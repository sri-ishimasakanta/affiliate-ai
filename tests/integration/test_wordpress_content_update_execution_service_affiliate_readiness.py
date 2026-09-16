"""WordPressContentUpdateExecutionService の affiliate publication readiness gate
(D-E2)。

WordPress へは一切実通信しない (fake client を注入)。projection push は既存の
実装 (``resolve_latest_acknowledgement``/``is_target_eligible``) をそのまま
local DB fixture で駆動する -- モックしない。

substitution_count > 0 の artifact だけが対象。substitution_count == 0 の
既存経路は ``test_wordpress_content_update_execution_service.py`` の既存
32 テストがそのまま無変更で通ることで証明されている (このファイルは追加しない)。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.affiliate.projection import (
    PROJECTION_STATUS_ACTIVE,
    PROJECTION_VERSION_ACTIVE,
    projection_from_target,
)
from app.affiliate.projection_push_acknowledgement import (
    serialize_manifest,
    token_fingerprint,
)
from app.article.draft_promotion_canonical import compute_text_hash
from app.config.settings import get_settings
from app.exceptions import WordPressContentUpdateRunError
from app.models import (
    AffiliateLinkTarget,
    AffiliateProgram,
    Article,
    ArticlePublicationArtifact,
    WordPressContentUpdateRun,
    WordPressDraftRun,
    WordPressPublicationRun,
)
from app.models.wordpress_content_update_run import WP_CONTENT_UPDATE_SUCCEEDED
from app.repositories.affiliate_target_projection_push_run_repository import (
    AffiliateTargetProjectionPushRunRepository,
)
from app.repositories.article_publication_artifact_repository import (
    ArticlePublicationArtifactRepository,
)
from app.services.affiliate_publication_readiness import (
    REASON_CURRENT_EVIDENCE_UNRESOLVABLE,
    REASON_HOST_NOT_APPROVED,
    REASON_MAPPING_NOT_ACTIVE,
    REASON_PROGRAM_NOT_ACTIVE,
    REASON_PROJECTION_NOT_ELIGIBLE,
    REASON_PROJECTION_VERSION_MISMATCH,
    REASON_TARGET_NOT_ACTIVE,
)
from app.services.article_link_occurrence_preview_service import (
    ArticleLinkOccurrencePreviewService,
)
from app.services.article_link_substitution_service import ArticleLinkSubstitutionService
from app.services.article_publication_artifact_persistence_service import (
    ArticlePublicationArtifactPersistenceService,
)
from app.services.article_publication_artifact_service import (
    ArticlePublicationArtifactService,
)
from app.services.article_publication_preparation_service import (
    ArticlePublicationPreparationService,
)
from app.services.wordpress_content_update_execution_service import (
    OUTCOME_SUCCEEDED,
    WordPressContentUpdateExecutionService,
)
from app.services.wordpress_content_update_preflight_service import (
    CLASSIFICATION_CONTENT_NOOP,
    CLASSIFICATION_UPDATE_REQUIRED,
)
from app.wordpress.client import WordPressUpdatedPost
from app.wordpress.publication_artifact import compute_artifact_hash
from app.wordpress.publication_artifact import serialize_manifest as serialize_publication_manifest

_HREF = "https://official.example.test/tool-a"
_TARGET_BASE_URL = "https://bizfluxlab.com"
_WP_STORED_RAW = "wordpress-stored-raw-content"
_APPROVED_HOST = "aff.example.test"
_APPROVED_PROVIDER = "test-asp"


@pytest.fixture
def wp_env(monkeypatch):
    monkeypatch.setenv("WORDPRESS_BASE_URL", _TARGET_BASE_URL)
    monkeypatch.setenv("WORDPRESS_USERNAME", "wp-user-secret")
    monkeypatch.setenv("WORDPRESS_APP_PASSWORD", "aaaa bbbb cccc dddd")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def approved_host_policy(monkeypatch):
    """D-E2 §27: is_host_approved() が読む module-level constant を、このテスト
    だけ差し替える (production の DEFAULT_DESTINATION_HOST_POLICY は不変のまま
    -- monkeypatch は自動的に revert される)。"""

    monkeypatch.setattr(
        "app.affiliate.destination_policy.DEFAULT_DESTINATION_HOST_POLICY",
        {_APPROVED_PROVIDER: frozenset({_APPROVED_HOST})},
    )


class _FakeWordPressClient:
    """テスト専用 fake -- 実ネットワークへは一切到達しない。

    ``get_responses``/``get_excs`` は 1-based の呼び出し順で消費される
    (1 回目 = preflight GET, 2 回目 = post-write read-back GET)。
    """

    def __init__(
        self,
        *,
        get_responses: list[dict] | None = None,
        post_response: WordPressUpdatedPost | None = None,
    ):
        self._get_responses = list(get_responses) if get_responses else []
        self._post_response = post_response
        self.get_calls = 0
        self.post_calls = 0

    def get_post(self, post_id: int) -> dict:
        self.get_calls += 1
        idx = self.get_calls - 1
        if idx < len(self._get_responses):
            return self._get_responses[idx]
        raise AssertionError(f"unexpected extra get_post call #{self.get_calls}")

    def update_post_content_exact(self, post_id: int, payload_json: str) -> WordPressUpdatedPost:
        self.post_calls += 1
        assert self._post_response is not None
        return self._post_response


def _wp_response(
    *,
    post_id: int = 25,
    status: str = "publish",
    raw: str = "raw-content",
    modified_gmt="2026-09-16T00:00:00",
) -> dict:
    return {
        "id": post_id,
        "status": status,
        "content": {"raw": raw, "rendered": f"<rendered>{raw}</rendered>"},
        "modified_gmt": modified_gmt,
        "link": f"https://bizfluxlab.com/?p={post_id}",
    }


def _add_draft_run(
    session: Session, article: Article, *, rendered_content_hash: str, wordpress_post_id: str
) -> WordPressDraftRun:
    run = WordPressDraftRun(
        article_id=article.id,
        source_promotion_id=1,
        status="succeeded",
        target_base_url=_TARGET_BASE_URL,
        method="POST",
        endpoint_path="/wp-json/wp/v2/posts",
        payload_json="{}",
        payload_hash=compute_text_hash("payload"),
        request_identity_hash=compute_text_hash("identity"),
        target_request_identity_hash=compute_text_hash("target-identity"),
        canonical_body_hash=compute_text_hash(article.body or ""),
        canonical_meta_hash=compute_text_hash(""),
        renderer_version="wordpress_html_v1",
        rendered_content_hash=rendered_content_hash,
        wordpress_post_id=wordpress_post_id,
        wordpress_post_status="draft",
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    session.add(run)
    session.commit()
    return run


def _add_publication_run(
    session: Session,
    article: Article,
    draft_run: WordPressDraftRun,
    *,
    wordpress_raw_content_hash: str,
    wordpress_post_id: str,
) -> WordPressPublicationRun:
    run = WordPressPublicationRun(
        article_id=article.id,
        source_wordpress_draft_run_id=draft_run.id,
        status="succeeded",
        target_base_url=_TARGET_BASE_URL,
        wordpress_post_id=wordpress_post_id,
        method="POST",
        endpoint_path=f"/wp-json/wp/v2/posts/{wordpress_post_id}",
        publish_payload_json='{"status":"publish"}',
        publish_payload_hash=compute_text_hash("publish-payload"),
        publication_request_identity_hash=compute_text_hash("pub-identity"),
        target_publication_request_identity_hash=compute_text_hash("target-pub-identity"),
        canonical_body_hash=draft_run.canonical_body_hash,
        canonical_meta_hash=draft_run.canonical_meta_hash,
        wordpress_raw_content_hash=wordpress_raw_content_hash,
        expected_pre_publish_status="draft",
        wordpress_post_status="publish",
        wordpress_post_url=f"https://bizfluxlab.com/?p={wordpress_post_id}",
        published_at_source="wordpress_date_gmt",
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    session.add(run)
    session.commit()
    return run


def _row_counts(session: Session):
    return (
        session.scalar(select(func.count()).select_from(ArticlePublicationArtifact)),
        session.scalar(select(func.count()).select_from(WordPressContentUpdateRun)),
    )


def _seed_program(session: Session, *, status: str = "active") -> AffiliateProgram:
    program = AffiliateProgram(name="Test ASP", provider=_APPROVED_PROVIDER, status=status)
    session.add(program)
    session.commit()
    return program


def _matching_manifest_entry(target: AffiliateLinkTarget) -> dict:
    proj = projection_from_target(target)
    return {
        "affiliate_link_target_id": target.id,
        "token_fingerprint": token_fingerprint(target.token),
        "link_identity_hash": target.link_identity_hash,
        "status": PROJECTION_STATUS_ACTIVE,
        "projection_version": PROJECTION_VERSION_ACTIVE,
        "entry_hash": proj.projection_entry_hash,
    }


def _succeed_run(session: Session, *, manifest: list[dict]) -> None:
    repo = AffiliateTargetProjectionPushRunRepository(session)
    run = repo.add_running(
        snapshot_scope="full",
        runtime_origin=_TARGET_BASE_URL,
        requested_snapshot_hash="a" * 64,
        requested_target_count=len(manifest),
        request_manifest_json=serialize_manifest(manifest),
        started_at=datetime.now(UTC),
    )
    repo.mark_succeeded(
        run,
        http_status=200,
        response_projection_snapshot_hash="a" * 64,
        received_count=len(manifest),
        inserted_count=len(manifest),
        updated_count=0,
        unchanged_count=0,
        finished_at=datetime.now(UTC),
    )
    session.commit()


def _update_required_scenario_with_substitution(
    session: Session,
    *,
    slug: str = "p1",
    program_status: str = "active",
    target_status: str = "active",
    mapping_status: str | None = None,
    wordpress_post_id: str = "25",
):
    """substitution_count == 1 な、UPDATE_REQUIRED に到達する artifact を作る
    (``_update_required_scenario`` の D-E2 版: mapping/target/program/
    projection push を追加する)。

    ``mapping_status``/``target_status``/``program_status`` は、正しい eligible
    な状態で artifact を組み立てた **後で** 変更する余地を呼び出し側に残すため、
    ここでは常に active な状態で構築し、返り値の ``target``/``mapping``/
    ``program`` を使って呼び出し側が個別に状態遷移させること (Human approval
    後に運用状態が悪化する D-E2 §22 のシナリオを正しく再現するため)。
    """

    art = Article(
        title="t",
        slug=slug,
        keyword_id=None,
        body=f"[tool]({_HREF})\n",
        wordpress_post_id=int(wordpress_post_id),
    )
    session.add(art)
    session.commit()

    program = _seed_program(session, status=program_status)
    target = AffiliateLinkTarget(
        token="REALAFFILIATETOKEN001AA",
        article_id=art.id,
        affiliate_program_id=program.id,
        destination_url=f"https://{_APPROVED_HOST}/x",
        destination_host=_APPROVED_HOST,
        status=target_status,
        link_identity_hash="1" * 64,
    )
    session.add(target)
    session.commit()

    preview = ArticleLinkOccurrencePreviewService(session, settings=get_settings()).preview(art.id)
    occ0 = preview.occurrences[0]
    mapping = ArticleLinkSubstitutionService(session).create_mapping(
        article_id=art.id,
        occurrence_identity_hash=occ0.occurrence_identity_hash,
        original_href=occ0.original_href,
        affiliate_link_target_id=target.id,
    )

    _succeed_run(session, manifest=[_matching_manifest_entry(target)])

    prepared = ArticlePublicationPreparationService(session, settings=get_settings()).prepare(
        art.id
    )
    assert prepared.substitution_count == 1  # このシナリオの前提
    artifact = ArticlePublicationArtifactPersistenceService(session).persist(prepared)
    ArticlePublicationArtifactService(session).approve_artifact(
        artifact.id, expected_artifact_hash=artifact.artifact_hash
    )
    session.refresh(artifact)

    if mapping_status is not None:
        from app.repositories.article_link_substitution_mapping_repository import (
            ArticleLinkSubstitutionMappingRepository,
        )

        mrepo = ArticleLinkSubstitutionMappingRepository(session)
        m = mrepo.get_by_id(mapping.id)
        if mapping_status == "revoked":
            mrepo.revoke(m)
        session.commit()

    old_submitted_hash = compute_text_hash("<p>OLD, never equal to the real artifact</p>")
    assert old_submitted_hash != artifact.tracked_html_hash
    raw_hash = compute_text_hash(_WP_STORED_RAW)
    draft = _add_draft_run(
        session, art, rendered_content_hash=old_submitted_hash, wordpress_post_id=wordpress_post_id
    )
    pub = _add_publication_run(
        session,
        art,
        draft,
        wordpress_raw_content_hash=raw_hash,
        wordpress_post_id=wordpress_post_id,
    )
    return art, artifact, target, mapping, program, draft, pub


def _forged_scenario_with_manifest_override(
    session: Session, *, slug: str, manifest_overrides: dict, wordpress_post_id: str = "25"
):
    """``_update_required_scenario_with_substitution`` と同じ土台を使うが、
    manifest の 1 field だけを改竄し、``artifact_hash`` をその改竄後の manifest
    から **正しく再計算** して forge する (D-E1.1 の forged-row 技法の応用)。

    単純に永続化済み artifact の substitution_manifest_json だけを書き換えると
    artifact_hash が manifest と食い違い、D-D5C の既存 local gate
    (REASON_ARTIFACT_INVALID) が classify() の時点で先に fail closed にしてしまい、
    UPDATE_REQUIRED に到達できない (D-E2 §26 で判明した事実)。ここでは
    manifest 全体を書き換えた **上で** artifact_hash も一致させることで、
    frozen artifact 自体は自己整合的 (artifact_hash_valid=True) なまま、
    CURRENT evidence の binding だけが壊れているシナリオを作る -- D-E2 の
    readiness gate に実際に到達させるための、意図的な最小限の改竄。"""

    art = Article(
        title="t", slug=slug, keyword_id=None, body=f"[tool]({_HREF})\n",
        wordpress_post_id=int(wordpress_post_id),
    )
    session.add(art)
    session.commit()

    program = _seed_program(session)
    target = AffiliateLinkTarget(
        token="REALAFFILIATETOKEN005AA", article_id=art.id, affiliate_program_id=program.id,
        destination_url=f"https://{_APPROVED_HOST}/x", destination_host=_APPROVED_HOST,
        status="active", link_identity_hash="5" * 64,
    )
    session.add(target)
    session.commit()

    preview = ArticleLinkOccurrencePreviewService(session, settings=get_settings()).preview(art.id)
    occ0 = preview.occurrences[0]
    mapping = ArticleLinkSubstitutionService(session).create_mapping(
        article_id=art.id, occurrence_identity_hash=occ0.occurrence_identity_hash,
        original_href=occ0.original_href, affiliate_link_target_id=target.id,
    )
    _succeed_run(session, manifest=[_matching_manifest_entry(target)])
    prepared = ArticlePublicationPreparationService(
        session, settings=get_settings()
    ).prepare(art.id)
    assert prepared.substitution_count == 1

    forged_manifest = [dict(e) for e in prepared.substitution_manifest]
    forged_manifest[0].update(manifest_overrides)
    forged_artifact_hash = compute_artifact_hash(
        artifact_schema_version=prepared.artifact_schema_version,
        article_id=prepared.article_id,
        canonical_body_hash=prepared.canonical_body_hash,
        renderer_version=prepared.renderer_version,
        manifest=forged_manifest,
    )
    forged = ArticlePublicationArtifactRepository(session).add(
        article_id=prepared.article_id,
        canonical_body_hash=prepared.canonical_body_hash,
        renderer_version=prepared.renderer_version,
        artifact_schema_version=prepared.artifact_schema_version,
        substitution_manifest_json=serialize_publication_manifest(forged_manifest),
        artifact_hash=forged_artifact_hash,
        tracked_html=prepared.tracked_html,
        tracked_html_hash=prepared.tracked_html_hash,
        substitution_count=prepared.substitution_count,
        approved_at=None,
        approved_artifact_hash=None,
        generated_at=datetime.now(UTC),
    )
    session.commit()
    ArticlePublicationArtifactService(session).approve_artifact(
        forged.id, expected_artifact_hash=forged.artifact_hash
    )
    session.refresh(forged)

    old_submitted_hash = compute_text_hash("<p>OLD, never equal to the real artifact</p>")
    raw_hash = compute_text_hash(_WP_STORED_RAW)
    draft = _add_draft_run(
        session, art, rendered_content_hash=old_submitted_hash, wordpress_post_id=wordpress_post_id
    )
    _add_publication_run(
        session, art, draft, wordpress_raw_content_hash=raw_hash,
        wordpress_post_id=wordpress_post_id,
    )
    return art, forged, target, mapping, program


def _fake_for_update() -> _FakeWordPressClient:
    return _FakeWordPressClient(
        get_responses=[_wp_response(raw=_WP_STORED_RAW)],  # preflight GET only
    )


# ==================== fully-ready positive path (§30/§31) =======================
def test_fully_ready_substituted_artifact_executes_successfully(
    session: Session, wp_env, approved_host_policy
) -> None:
    art, artifact, target, mapping, program, draft, pub = (
        _update_required_scenario_with_substitution(session)
    )

    fake = _FakeWordPressClient(
        get_responses=[
            _wp_response(raw=_WP_STORED_RAW),  # preflight
            _wp_response(raw="post-write raw content"),  # read-back
        ],
        post_response=WordPressUpdatedPost(
            id=25, status="publish", link="https://bizfluxlab.com/?p=25"
        ),
    )
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    assert result.preflight_classification == CLASSIFICATION_UPDATE_REQUIRED
    assert result.executed is True
    assert result.run_status == WP_CONTENT_UPDATE_SUCCEEDED
    assert result.reason_code == OUTCOME_SUCCEEDED
    assert fake.get_calls == 2
    assert fake.post_calls == 1

    run = session.get(WordPressContentUpdateRun, result.run_id)
    assert run.status == WP_CONTENT_UPDATE_SUCCEEDED
    # D-E2 §31: readiness は payload/identity を一切変えない -- 既存の
    # request_content_hash/target 情報は D-D5D のときと同一の値のまま。
    assert run.request_content_hash == artifact.tracked_html_hash
    assert run.wordpress_post_id == "25"
    assert (
        run.expected_pre_update_wordpress_raw_content_hash
        == run.observed_pre_update_wordpress_raw_content_hash
    )
    assert run.response_content_raw_hash == compute_text_hash("post-write raw content")


# ==================== artifact-level integrity gates (§26) ======================
# -- artifact-level integrity/approval gates -------------------------------------
# D-E2 §26 note: WordPressContentUpdatePreflightService.classify() (D-D5C, pre-
# existing, unmodified by D-E2) ALREADY performs its own local gate covering
# not-approved / approval-hash-mismatch / artifact-hash-invalid / tracked-html-
# hash-invalid / manifest-invalid / canonical-drift *before* it can ever return
# UPDATE_REQUIRED (see REASON_ARTIFACT_NOT_APPROVED / REASON_APPROVAL_HASH_MISMATCH
# / REASON_ARTIFACT_INVALID / REASON_CURRENT_CANONICAL_DRIFT in that module).
# Consequently these specific corruptions can never reach D-E2's own
# evaluate_affiliate_publication_readiness() at the full-integration level --
# classify() already returns a non-UPDATE_REQUIRED result first, so D-E2's own
# (deliberately redundant, defense-in-depth) artifact-level checks are exercised
# exhaustively instead in tests/unit/test_affiliate_publication_readiness.py via
# directly-constructed ArtifactInspection objects (D-E2 §26's explicit escape
# hatch: "service-level readiness tests may reuse a controlled inspection result
# rather than duplicating all cryptographic fixture construction"). The one test
# below proves the actually-reachable, full end-to-end system property required
# by §26 ("at least one full integration path must prove artifact integrity
# failure blocks execution before Transaction A") -- via the pre-existing D-D5C
# gate, which D-E2 does not weaken or bypass.
def test_not_approved_artifact_blocks_before_transaction_a_via_preflight(
    session: Session, wp_env, approved_host_policy
) -> None:
    art = Article(title="t", slug="p-notapproved", keyword_id=None, body=f"[tool]({_HREF})\n")
    session.add(art)
    session.commit()
    program = _seed_program(session)
    target = AffiliateLinkTarget(
        token="REALAFFILIATETOKEN002AA",
        article_id=art.id,
        affiliate_program_id=program.id,
        destination_url=f"https://{_APPROVED_HOST}/x",
        destination_host=_APPROVED_HOST,
        status="active",
        link_identity_hash="2" * 64,
    )
    session.add(target)
    session.commit()
    preview = ArticleLinkOccurrencePreviewService(session, settings=get_settings()).preview(art.id)
    occ0 = preview.occurrences[0]
    ArticleLinkSubstitutionService(session).create_mapping(
        article_id=art.id,
        occurrence_identity_hash=occ0.occurrence_identity_hash,
        original_href=occ0.original_href,
        affiliate_link_target_id=target.id,
    )
    _succeed_run(session, manifest=[_matching_manifest_entry(target)])
    prepared = ArticlePublicationPreparationService(session, settings=get_settings()).prepare(
        art.id
    )
    artifact = ArticlePublicationArtifactPersistenceService(session).persist(prepared)
    # 意図的に approve_artifact() を呼ばない。
    old_submitted_hash = compute_text_hash("<p>OLD</p>")
    raw_hash = compute_text_hash(_WP_STORED_RAW)
    draft = _add_draft_run(
        session, art, rendered_content_hash=old_submitted_hash, wordpress_post_id="25"
    )
    _add_publication_run(
        session, art, draft, wordpress_raw_content_hash=raw_hash, wordpress_post_id="25"
    )
    art.wordpress_post_id = 25
    session.commit()

    before = _row_counts(session)
    fake = _fake_for_update()
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    # blocked by D-D5C's own pre-existing local gate -- never reaches UPDATE_REQUIRED,
    # so D-E2's readiness gate never runs and no exception is raised either.
    assert result.preflight_classification is None
    assert result.reason_code == "ARTIFACT_NOT_APPROVED"
    assert result.executed is False
    assert result.run_id is None
    assert fake.post_calls == 0
    assert _row_counts(session) == before


# ==================== CURRENT evidence resolvability (§23/§24/§25) ==============
def test_mapping_missing_blocks_before_transaction_a(
    session: Session, wp_env, approved_host_policy
) -> None:
    art, artifact, target, mapping, program = _forged_scenario_with_manifest_override(
        session, slug="p-mapmissing", manifest_overrides={"mapping_id": 999999}
    )

    before = _row_counts(session)
    fake = _fake_for_update()
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    with pytest.raises(WordPressContentUpdateRunError, match=REASON_CURRENT_EVIDENCE_UNRESOLVABLE):
        svc.execute(
            article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
        )

    assert fake.post_calls == 0
    assert _row_counts(session) == before


def test_mapping_revoked_blocks_before_transaction_a(
    session: Session, wp_env, approved_host_policy
) -> None:
    art, artifact, target, mapping, program, draft, pub = (
        _update_required_scenario_with_substitution(
            session, slug="p-maprevoked", mapping_status="revoked"
        )
    )

    before = _row_counts(session)
    fake = _fake_for_update()
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    with pytest.raises(WordPressContentUpdateRunError, match=REASON_MAPPING_NOT_ACTIVE):
        svc.execute(
            article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
        )

    assert fake.post_calls == 0
    assert _row_counts(session) == before


def test_target_disabled_blocks_before_transaction_a(
    session: Session, wp_env, approved_host_policy
) -> None:
    art, artifact, target, mapping, program, draft, pub = (
        _update_required_scenario_with_substitution(session, slug="p-targetdisabled")
    )
    target.status = "disabled"
    session.commit()

    before = _row_counts(session)
    fake = _fake_for_update()
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    with pytest.raises(WordPressContentUpdateRunError, match=REASON_TARGET_NOT_ACTIVE):
        svc.execute(
            article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
        )

    assert fake.post_calls == 0
    assert _row_counts(session) == before


# Note: unlike mapping_id/target_projection_version (pure metadata not reflected
# in the rendered HTML), the manifest's `token` is cryptographically baked into
# the actual tracked_html content itself (the /go/{token} href). Forging a
# manifest.token mismatch while keeping the frozen artifact_hash internally
# consistent (as _forged_scenario_with_manifest_override does for the other
# fields) necessarily also breaks strict_html_validation_valid (the token in
# tracked_html no longer matches the token the manifest now claims) -- which
# D-D5C's preflight already treats as ARTIFACT_INVALID before UPDATE_REQUIRED is
# ever reached, empirically confirmed. So "target_identity_mismatch via token"
# cannot be constructed as an isolated full-integration scenario -- this is a
# genuine security property (the token cannot silently diverge from the served
# content), not a test gap. The behavior of evaluate_affiliate_publication_
# readiness() itself given an unresolvable entry (regardless of which specific
# fail_reason produced it) is covered directly in
# tests/unit/test_affiliate_publication_readiness.py, and the inspection
# service's own token-mismatch -> target_identity_mismatch detection is proven
# in tests/integration/test_article_publication_artifact_inspection_service.py::
# test_current_evidence_target_token_mismatch_fails_closed (D-E1.1).


def test_program_missing_blocks_before_transaction_a(
    session: Session, wp_env, approved_host_policy
) -> None:
    art = Article(title="t", slug="p-progmissing", keyword_id=None, body=f"[tool]({_HREF})\n")
    session.add(art)
    session.commit()
    missing_program_id = 777777
    target = AffiliateLinkTarget(
        token="REALAFFILIATETOKEN003AA",
        article_id=art.id,
        affiliate_program_id=missing_program_id,
        destination_url=f"https://{_APPROVED_HOST}/x",
        destination_host=_APPROVED_HOST,
        status="active",
        link_identity_hash="3" * 64,
    )
    session.add(target)
    session.commit()
    preview = ArticleLinkOccurrencePreviewService(session, settings=get_settings()).preview(art.id)
    occ0 = preview.occurrences[0]
    ArticleLinkSubstitutionService(session).create_mapping(
        article_id=art.id,
        occurrence_identity_hash=occ0.occurrence_identity_hash,
        original_href=occ0.original_href,
        affiliate_link_target_id=target.id,
    )
    _succeed_run(session, manifest=[_matching_manifest_entry(target)])
    prepared = ArticlePublicationPreparationService(session, settings=get_settings()).prepare(
        art.id
    )
    artifact = ArticlePublicationArtifactPersistenceService(session).persist(prepared)
    ArticlePublicationArtifactService(session).approve_artifact(
        artifact.id, expected_artifact_hash=artifact.artifact_hash
    )
    session.refresh(artifact)
    old_submitted_hash = compute_text_hash("<p>OLD</p>")
    raw_hash = compute_text_hash(_WP_STORED_RAW)
    draft = _add_draft_run(
        session, art, rendered_content_hash=old_submitted_hash, wordpress_post_id="25"
    )
    _add_publication_run(
        session, art, draft, wordpress_raw_content_hash=raw_hash, wordpress_post_id="25"
    )
    art.wordpress_post_id = 25
    session.commit()

    before = _row_counts(session)
    fake = _fake_for_update()
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    with pytest.raises(WordPressContentUpdateRunError, match=REASON_CURRENT_EVIDENCE_UNRESOLVABLE):
        svc.execute(
            article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
        )

    assert fake.post_calls == 0
    assert _row_counts(session) == before


def test_program_paused_blocks_before_transaction_a(
    session: Session, wp_env, approved_host_policy
) -> None:
    art, artifact, target, mapping, program, draft, pub = (
        _update_required_scenario_with_substitution(session, slug="p-progpaused")
    )
    program.status = "paused"
    session.commit()

    before = _row_counts(session)
    fake = _fake_for_update()
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    with pytest.raises(WordPressContentUpdateRunError, match=REASON_PROGRAM_NOT_ACTIVE):
        svc.execute(
            article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
        )

    assert fake.post_calls == 0
    assert _row_counts(session) == before


# ==================== host-policy gate (§27) =====================================
def test_host_not_in_test_policy_blocks_before_transaction_a(session: Session, wp_env) -> None:
    """approved_host_policy fixture を **使わない** -- 本番同様、空の
    DEFAULT_DESTINATION_HOST_POLICY のもとでは実在する substituted artifact は
    常にこのゲートで fail closed になる (意図された挙動、D-E2 §10)。"""

    art, artifact, target, mapping, program, draft, pub = (
        _update_required_scenario_with_substitution(session, slug="p-nohostpolicy")
    )

    before = _row_counts(session)
    fake = _fake_for_update()
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    with pytest.raises(WordPressContentUpdateRunError, match=REASON_HOST_NOT_APPROVED):
        svc.execute(
            article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
        )

    assert fake.post_calls == 0
    assert _row_counts(session) == before


def test_lookalike_host_not_approved_even_with_test_policy(
    session: Session, wp_env, monkeypatch
) -> None:
    """exact-match host policy: 承認された host の **subdomain** は別 host
    として拒否されること (destination_policy.is_host_approved は完全一致のみ)。"""

    monkeypatch.setattr(
        "app.affiliate.destination_policy.DEFAULT_DESTINATION_HOST_POLICY",
        {_APPROVED_PROVIDER: frozenset({"other-approved.example.test"})},
    )
    art, artifact, target, mapping, program, draft, pub = (
        _update_required_scenario_with_substitution(session, slug="p-lookalike")
    )
    # target.destination_host == _APPROVED_HOST ("aff.example.test") はこの
    # policy に含まれない -- 承認集合には別の host しか無い。

    before = _row_counts(session)
    fake = _fake_for_update()
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    with pytest.raises(WordPressContentUpdateRunError, match=REASON_HOST_NOT_APPROVED):
        svc.execute(
            article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
        )

    assert fake.post_calls == 0
    assert _row_counts(session) == before


# ==================== projection acknowledgement gate (§28/§29) =================
# Note: a "zero acknowledgement history ever" scenario for an otherwise fully
# valid substituted artifact is architecturally unreachable through the normal
# D-D3 pipeline -- ArticleLinkOccurrencePreviewService.resolve_eligibility()
# already requires a matching acknowledgement to exist before the occurrence is
# even included in the manifest in the first place (proven in D-E1's own
# test_zero_substitution_when_no_push_run_makes_occurrence_ineligible_upstream).
# The two tests below cover the two scenarios that genuinely happen *after* a
# valid artifact has already been built and approved, per D-E2 §28.
def test_newer_acknowledgement_omitting_target_blocks_before_transaction_a(
    session: Session, wp_env, approved_host_policy
) -> None:
    """D-E2 §28: 最新の succeeded acknowledgement が target を含まなくなった
    (別の target だけの新しい snapshot に置き換わった) シナリオ -- Human 承認後に
    real acknowledgement 状態が変化した現実的なケース。"""

    art, artifact, target, mapping, program, draft, pub = (
        _update_required_scenario_with_substitution(session, slug="p-ackomits")
    )

    other_program = _seed_program(session)
    other_target = AffiliateLinkTarget(
        token="OTHERREADYTOKEN00001AA",
        article_id=art.id,
        affiliate_program_id=other_program.id,
        destination_url="https://other.example.test/z",
        destination_host="other.example.test",
        status="active",
        link_identity_hash="9" * 64,
    )
    session.add(other_target)
    session.commit()
    _succeed_run(session, manifest=[_matching_manifest_entry(other_target)])

    before = _row_counts(session)
    fake = _fake_for_update()
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    with pytest.raises(WordPressContentUpdateRunError, match=REASON_PROJECTION_NOT_ELIGIBLE):
        svc.execute(
            article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
        )

    assert fake.post_calls == 0
    assert _row_counts(session) == before


def test_running_latest_acknowledgement_blocks_fail_closed(
    session: Session, wp_env, approved_host_policy
) -> None:
    """最新 push run が running (未確定) の場合、既存の
    resolve_latest_acknowledgement() semantics どおり fail closed -- 古い
    succeeded run があっても遡らない。"""

    art, artifact, target, mapping, program, draft, pub = (
        _update_required_scenario_with_substitution(session, slug="p-ackrunning")
    )
    repo = AffiliateTargetProjectionPushRunRepository(session)
    repo.add_running(
        snapshot_scope="full",
        runtime_origin=_TARGET_BASE_URL,
        requested_snapshot_hash="b" * 64,
        requested_target_count=0,
        request_manifest_json=serialize_manifest([]),
        started_at=datetime.now(UTC),
    )
    session.commit()

    before = _row_counts(session)
    fake = _fake_for_update()
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    with pytest.raises(WordPressContentUpdateRunError, match=REASON_PROJECTION_NOT_ELIGIBLE):
        svc.execute(
            article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
        )

    assert fake.post_calls == 0
    assert _row_counts(session) == before


def test_projection_version_mismatch_blocks_even_though_currently_active(
    session: Session, wp_env, approved_host_policy
) -> None:
    """D-E2 §12: manifest の frozen target_projection_version と、現在の
    target.status から計算される current projection version が食い違う
    シナリオを明示的に作る (defense-in-depth の比較そのものを exercise する)。"""

    art, artifact, target, mapping, program = _forged_scenario_with_manifest_override(
        session,
        slug="p-versionmismatch",
        # target は実際には active (version 1) のまま -- frozen manifest だけが
        # disabled 用の version 2 を主張する食い違いを作る。
        manifest_overrides={"target_projection_version": 2},
    )
    assert target.status == "active"

    before = _row_counts(session)
    fake = _fake_for_update()
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    with pytest.raises(WordPressContentUpdateRunError, match=REASON_PROJECTION_VERSION_MISMATCH):
        svc.execute(
            article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
        )

    assert fake.post_calls == 0
    assert _row_counts(session) == before


# ==================== CONTENT_NOOP / non-UPDATE_REQUIRED bypass (§32) ===========
def test_content_noop_bypasses_readiness_even_when_state_is_not_ready(
    session: Session, wp_env
) -> None:
    """substituted artifact だが、fresh classify() が CONTENT_NOOP を返す場合
    (candidate == last-submitted) -- readiness は一切評価されない。host policy
    を承認しない (fixture を使わない) 状態でも、CONTENT_NOOP には無関係。"""

    art = Article(title="t", slug="p-noop-sub", keyword_id=None, body=f"[tool]({_HREF})\n")
    session.add(art)
    session.commit()
    program = _seed_program(session)
    target = AffiliateLinkTarget(
        token="REALAFFILIATETOKEN004AA",
        article_id=art.id,
        affiliate_program_id=program.id,
        destination_url=f"https://{_APPROVED_HOST}/x",
        destination_host=_APPROVED_HOST,
        status="active",
        link_identity_hash="4" * 64,
    )
    session.add(target)
    session.commit()
    preview = ArticleLinkOccurrencePreviewService(session, settings=get_settings()).preview(art.id)
    occ0 = preview.occurrences[0]
    ArticleLinkSubstitutionService(session).create_mapping(
        article_id=art.id,
        occurrence_identity_hash=occ0.occurrence_identity_hash,
        original_href=occ0.original_href,
        affiliate_link_target_id=target.id,
    )
    _succeed_run(session, manifest=[_matching_manifest_entry(target)])
    prepared = ArticlePublicationPreparationService(session, settings=get_settings()).prepare(
        art.id
    )
    assert prepared.substitution_count == 1
    artifact = ArticlePublicationArtifactPersistenceService(session).persist(prepared)
    ArticlePublicationArtifactService(session).approve_artifact(
        artifact.id, expected_artifact_hash=artifact.artifact_hash
    )
    session.refresh(artifact)

    # candidate (= artifact.tracked_html_hash) と last-submitted を一致させる
    # -> CONTENT_NOOP。
    submitted_hash = artifact.tracked_html_hash
    raw_hash = compute_text_hash(_WP_STORED_RAW)
    draft = _add_draft_run(
        session, art, rendered_content_hash=submitted_hash, wordpress_post_id="25"
    )
    _add_publication_run(
        session, art, draft, wordpress_raw_content_hash=raw_hash, wordpress_post_id="25"
    )
    art.wordpress_post_id = 25
    session.commit()

    # 現在の状態を non-ready にしておく (target disabled) -- それでも
    # CONTENT_NOOP なら readiness は一切走らないはず。
    target.status = "disabled"
    session.commit()

    before = _row_counts(session)
    fake = _FakeWordPressClient(get_responses=[_wp_response(raw=_WP_STORED_RAW)])
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    assert result.preflight_classification == CLASSIFICATION_CONTENT_NOOP
    assert result.executed is False
    assert result.run_id is None
    assert result.reason_code == CLASSIFICATION_CONTENT_NOOP
    assert fake.get_calls == 1
    assert fake.post_calls == 0
    assert _row_counts(session) == before


# ==================== zero-substitution UPDATE_REQUIRED regression (§33/§21) ====
def test_zero_substitution_update_required_bypasses_affiliate_readiness(
    session: Session, wp_env
) -> None:
    """substitution_count == 0 な artifact は、host policy が空でも
    affiliate readiness に一切関与されず、既存どおり実行できる -- D-E2 が
    ``substitution_count > 0`` だけを対象にしていることの直接証明。"""

    art = Article(
        title="t",
        slug="p-zero-sub",
        keyword_id=None,
        body="no external links here\n",
        wordpress_post_id=25,
    )
    session.add(art)
    session.commit()
    prepared = ArticlePublicationPreparationService(session, settings=get_settings()).prepare(
        art.id
    )
    assert prepared.substitution_count == 0
    artifact = ArticlePublicationArtifactPersistenceService(session).persist(prepared)
    ArticlePublicationArtifactService(session).approve_artifact(
        artifact.id, expected_artifact_hash=artifact.artifact_hash
    )
    session.refresh(artifact)

    old_submitted_hash = compute_text_hash("<p>OLD, never equal</p>")
    raw_hash = compute_text_hash(_WP_STORED_RAW)
    draft = _add_draft_run(
        session, art, rendered_content_hash=old_submitted_hash, wordpress_post_id="25"
    )
    _add_publication_run(
        session, art, draft, wordpress_raw_content_hash=raw_hash, wordpress_post_id="25"
    )

    fake = _FakeWordPressClient(
        get_responses=[
            _wp_response(raw=_WP_STORED_RAW),
            _wp_response(raw="post-write raw content"),
        ],
        post_response=WordPressUpdatedPost(
            id=25, status="publish", link="https://bizfluxlab.com/?p=25"
        ),
    )
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    assert result.preflight_classification == CLASSIFICATION_UPDATE_REQUIRED
    assert result.executed is True
    assert result.reason_code == OUTCOME_SUCCEEDED
    assert fake.post_calls == 1


# ==================== readiness performs DB reads only (§34) ====================
def test_readiness_evaluation_performs_no_additional_writes_beyond_transaction_a(
    session: Session, wp_env, approved_host_policy
) -> None:
    art, artifact, target, mapping, program, draft, pub = (
        _update_required_scenario_with_substitution(session, slug="p-readonly")
    )

    fake = _FakeWordPressClient(
        get_responses=[_wp_response(raw=_WP_STORED_RAW), _wp_response(raw="post-write raw")],
        post_response=WordPressUpdatedPost(
            id=25, status="publish", link="https://bizfluxlab.com/?p=25"
        ),
    )
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    assert result.executed is True
    # readiness 自体が新しい行を作ったなら run 以外の何かが増えているはずだが、
    # 増えるのは WordPressContentUpdateRun の 1 行だけであること。
    counts_after = _row_counts(session)
    assert counts_after[1] == 1  # WordPressContentUpdateRun ちょうど 1 行
