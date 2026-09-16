"""ArticlePublicationArtifactInspectionService — D-D4 READ-ONLY Human inspection:
independent re-verification + token/secret masking + historical independence.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
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
from app.config.settings import Settings
from app.exceptions import EntityNotFoundError
from app.models import AffiliateLinkTarget, AffiliateProgram, Article
from app.repositories.affiliate_target_projection_push_run_repository import (
    AffiliateTargetProjectionPushRunRepository,
)
from app.repositories.article_publication_artifact_repository import (
    ArticlePublicationArtifactRepository,
)
from app.services.article_link_occurrence_preview_service import (
    ArticleLinkOccurrencePreviewService,
)
from app.services.article_link_substitution_service import (
    ArticleLinkSubstitutionService,
)
from app.services.article_publication_artifact_inspection_service import (
    CURRENT_CANONICAL_ARTICLE_NOT_FOUND,
    CURRENT_CANONICAL_DRIFT,
    CURRENT_CANONICAL_MATCH,
    ArticlePublicationArtifactInspectionService,
)
from app.services.article_publication_artifact_persistence_service import (
    ArticlePublicationArtifactPersistenceService,
)
from app.services.article_publication_preparation_service import (
    ArticlePublicationPreparationService,
)
from app.wordpress.publication_artifact import expected_replacement_href

_HREF = "https://official.example.test/tool-a"
_REAL_TOKEN = "REALSECRETTOKENFORTEST"


def _settings() -> Settings:
    return Settings(wordpress_base_url="https://bizfluxlab.com")


def _seed_article(session: Session, *, slug: str = "p1") -> Article:
    art = Article(title="t", slug=slug, keyword_id=None, body=f"[tool]({_HREF})\n")
    session.add(art)
    session.commit()
    return art


def _seed_program(session: Session) -> AffiliateProgram:
    program = AffiliateProgram(name="Test ASP", provider="test-asp", status="active")
    session.add(program)
    session.commit()
    return program


def _seed_target(
    session: Session, *, article_id: int, token: str = _REAL_TOKEN
) -> AffiliateLinkTarget:
    program = _seed_program(session)
    target = AffiliateLinkTarget(
        token=token, article_id=article_id, affiliate_program_id=program.id,
        destination_url="https://aff.example.test/x", destination_host="aff.example.test",
        status="active", link_identity_hash="1" * 64,
    )
    session.add(target)
    session.commit()
    return target


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
        snapshot_scope="full", runtime_origin="https://bizfluxlab.com",
        requested_snapshot_hash="a" * 64, requested_target_count=len(manifest),
        request_manifest_json=serialize_manifest(manifest), started_at=datetime.now(UTC),
    )
    repo.mark_succeeded(
        run, http_status=200, response_projection_snapshot_hash="a" * 64,
        received_count=len(manifest), inserted_count=len(manifest), updated_count=0,
        unchanged_count=0, finished_at=datetime.now(UTC),
    )
    session.commit()


def _persisted_artifact_with_substitution(session: Session):
    art = _seed_article(session)
    target = _seed_target(session, article_id=art.id)
    preview = ArticleLinkOccurrencePreviewService(session, settings=_settings()).preview(art.id)
    occ0 = preview.occurrences[0]
    ArticleLinkSubstitutionService(session).create_mapping(
        article_id=art.id, occurrence_identity_hash=occ0.occurrence_identity_hash,
        original_href=occ0.original_href, affiliate_link_target_id=target.id,
    )
    _succeed_run(session, manifest=[_matching_manifest_entry(target)])
    prepared = ArticlePublicationPreparationService(session, settings=_settings()).prepare(art.id)
    artifact = ArticlePublicationArtifactPersistenceService(session).persist(prepared)
    return art, target, artifact


def _inspect(session: Session, artifact_id: int):
    return ArticlePublicationArtifactInspectionService(session).inspect(artifact_id)


# ==================== §33: inspection security =================================
def test_inspection_never_exposes_full_token(session: Session) -> None:
    _art, target, artifact = _persisted_artifact_with_substitution(session)
    insp = _inspect(session, artifact.id)

    assert len(insp.manifest_summary) == 1
    entry = insp.manifest_summary[0]
    assert _REAL_TOKEN not in entry.replacement_href_masked
    assert entry.replacement_href_masked == "https://bizfluxlab.com/go/<masked>"
    assert entry.token_fingerprint == token_fingerprint(target.token)
    assert _REAL_TOKEN not in entry.token_fingerprint

    # dataclass repr も含め、どこにも full token が出てこないこと。
    full_repr = repr(insp)
    assert _REAL_TOKEN not in full_repr
    assert "AFFILIATE_RUNTIME_SHARED_SECRET" not in full_repr
    # D-E1: destination_host は CURRENT evidence として意図的に表示される
    # (original_host とは別概念 -- D-E0.2/D-E0.3 の訂正)。full destination_url
    # (query string 含む) は引き続き一切出さない (次のテストで検証)。
    assert entry.current.resolvable is True
    assert entry.current.destination_host == "aff.example.test"


def test_inspection_does_not_expose_destination_url_or_secret(session: Session) -> None:
    _art, _target, artifact = _persisted_artifact_with_substitution(session)
    insp = _inspect(session, artifact.id)
    full_repr = repr(insp)
    assert "https://aff.example.test/x" not in full_repr
    assert "shared_secret" not in full_repr.lower()
    assert "hmac" not in full_repr.lower()


# ==================== §34: inspection integrity =================================
def test_inspection_valid_artifact_all_checks_true(session: Session) -> None:
    art = _seed_article(session)
    prepared = ArticlePublicationPreparationService(session, settings=_settings()).prepare(art.id)
    artifact = ArticlePublicationArtifactPersistenceService(session).persist(prepared)

    insp = _inspect(session, artifact.id)
    assert insp.artifact_hash_valid is True
    assert insp.tracked_html_hash_valid is True
    assert insp.manifest_valid is True
    assert insp.strict_html_validation_valid is True
    assert insp.current_canonical_status == CURRENT_CANONICAL_MATCH


def test_inspection_detects_tracked_html_hash_mismatch(session: Session) -> None:
    art = _seed_article(session)
    prepared = ArticlePublicationPreparationService(session, settings=_settings()).prepare(art.id)
    artifact = ArticlePublicationArtifactPersistenceService(session).persist(prepared)

    artifact.tracked_html_hash = "f" * 64
    session.commit()

    insp = _inspect(session, artifact.id)
    assert insp.tracked_html_hash_valid is False


def test_inspection_detects_artifact_hash_mismatch(session: Session) -> None:
    art = _seed_article(session)
    prepared = ArticlePublicationPreparationService(session, settings=_settings()).prepare(art.id)
    artifact = ArticlePublicationArtifactPersistenceService(session).persist(prepared)

    # artifact_hash 自体を書き換える (unique constraint に注意して別値にする)。
    artifact.artifact_hash = "e" * 64
    session.commit()

    insp = _inspect(session, artifact.id)
    assert insp.artifact_hash_valid is False
    # 他のチェックは独立している -- manifest/tracked_html 自体は壊れていない。
    assert insp.tracked_html_hash_valid is True
    assert insp.manifest_valid is True


def test_inspection_detects_manifest_corruption(session: Session) -> None:
    _art, _target, artifact = _persisted_artifact_with_substitution(session)

    artifact.substitution_manifest_json = "not-valid-json{{{"
    session.commit()

    insp = _inspect(session, artifact.id)
    assert insp.manifest_valid is False
    assert insp.artifact_hash_valid is False  # manifest 不正なので再計算もできない
    assert insp.strict_html_validation_valid is False
    assert insp.manifest_summary == []


def test_inspection_detects_strict_reverse_validation_failure(session: Session) -> None:
    _art, _target, artifact = _persisted_artifact_with_substitution(session)

    # tracked_html から target="_blank" を剥がして、manifest との整合を壊す
    # (D-D3 の reverse_tracked_html が矛盾を検出して例外を出すはず)。
    manifest = json.loads(artifact.substitution_manifest_json)
    replacement_href = manifest[0]["replacement_href"]
    tampered_html = artifact.tracked_html.replace(
        f'href="{replacement_href}" target="_blank"', f'href="{replacement_href}"'
    )
    assert tampered_html != artifact.tracked_html
    artifact.tracked_html = tampered_html
    session.commit()

    insp = _inspect(session, artifact.id)
    assert insp.strict_html_validation_valid is False


def test_inspection_current_canonical_match_vs_drift(session: Session) -> None:
    art = _seed_article(session)
    prepared = ArticlePublicationPreparationService(session, settings=_settings()).prepare(art.id)
    artifact = ArticlePublicationArtifactPersistenceService(session).persist(prepared)

    insp_match = _inspect(session, artifact.id)
    assert insp_match.current_canonical_status == CURRENT_CANONICAL_MATCH
    assert insp_match.artifact_hash_valid is True  # drift とは独立

    art.body = "totally different body now\n"
    session.commit()

    insp_drift = _inspect(session, artifact.id)
    assert insp_drift.current_canonical_status == CURRENT_CANONICAL_DRIFT
    # artifact 自体の凍結整合性は drift の影響を受けない (§20)。
    assert insp_drift.artifact_hash_valid is True
    assert insp_drift.tracked_html_hash_valid is True
    assert insp_drift.manifest_valid is True
    assert insp_drift.strict_html_validation_valid is True


def test_inspection_missing_artifact_raises(session: Session) -> None:
    with pytest.raises(EntityNotFoundError):
        _inspect(session, 999999)


# ==================== §19: historical independence (no current-state calls) =
def test_inspection_does_not_consult_current_mapping_or_target_state(
    session: Session,
) -> None:
    art, target, artifact = _persisted_artifact_with_substitution(session)

    # mapping/target の現在の lifecycle を変化させる。
    from app.repositories.affiliate_link_target_repository import (
        AffiliateLinkTargetRepository,
    )

    AffiliateLinkTargetRepository(session).mark_disabled(target, disabled_at=datetime.now(UTC))
    session.commit()

    insp = _inspect(session, artifact.id)
    # historical artifact 自体の整合性検証は現在の target 状態に一切影響されない。
    assert insp.artifact_hash_valid is True
    assert insp.tracked_html_hash_valid is True
    assert insp.manifest_valid is True
    assert insp.strict_html_validation_valid is True
    assert insp.current_canonical_status == CURRENT_CANONICAL_MATCH


def test_inspection_reports_article_not_found_status(session: Session) -> None:
    art = _seed_article(session)
    prepared = ArticlePublicationPreparationService(session, settings=_settings()).prepare(art.id)

    # Article は D-D4 でも削除しないが、frozen artifact が article_id を
    # 参照できない状況を安全にシミュレートするために repository を直接使う。
    repo = ArticlePublicationArtifactRepository(session)
    forged = repo.add(
        article_id=999999,
        canonical_body_hash=prepared.canonical_body_hash,
        renderer_version=prepared.renderer_version,
        artifact_schema_version=prepared.artifact_schema_version,
        substitution_manifest_json=prepared.substitution_manifest_json,
        artifact_hash="d" * 64,
        tracked_html=prepared.tracked_html,
        tracked_html_hash=prepared.tracked_html_hash,
        substitution_count=prepared.substitution_count,
        approved_at=None,
        approved_artifact_hash=None,
        generated_at=datetime.now(UTC),
    )
    session.commit()

    insp = _inspect(session, forged.id)
    assert insp.current_canonical_status == CURRENT_CANONICAL_ARTICLE_NOT_FOUND
    assert insp.article_title == ""


# ==================== D-E1: FROZEN vs CURRENT evidence ==========================
def _corrupt_manifest_field(session: Session, artifact, *, field: str, value) -> None:
    manifest = json.loads(artifact.substitution_manifest_json)
    manifest[0][field] = value
    artifact.substitution_manifest_json = json.dumps(manifest)
    session.commit()


def _corrupt_manifest_fields(session: Session, artifact, **fields) -> None:
    manifest = json.loads(artifact.substitution_manifest_json)
    manifest[0].update(fields)
    artifact.substitution_manifest_json = json.dumps(manifest)
    session.commit()


def test_original_host_is_never_labeled_destination_host(session: Session) -> None:
    """D-E0.2/D-E0.3 の訂正: original_href の host と AffiliateLinkTarget の
    destination_host は別概念。deliberately 異なる host を使い、両方が正しく
    区別されて出ることを確認する。"""

    _art, _target, artifact = _persisted_artifact_with_substitution(session)
    insp = _inspect(session, artifact.id)
    entry = insp.manifest_summary[0]

    assert entry.original_host == "official.example.test"  # _HREF の host
    assert entry.current.destination_host == "aff.example.test"  # target の host
    assert entry.original_host != entry.current.destination_host


def test_safe_destination_display_hides_full_url_and_query(session: Session) -> None:
    """destination_url に affiliate 識別子/query parameter を含めても、
    出力には正規化された destination_host のみが現れ、full destination_url /
    query 値 / full token は一切現れないこと。"""

    art = _seed_article(session)
    program = _seed_program(session)
    target = AffiliateLinkTarget(
        token=_REAL_TOKEN, article_id=art.id, affiliate_program_id=program.id,
        destination_url="https://aff.example.test/x?aff_id=SECRET123&sub=abc",
        destination_host="aff.example.test", status="active", link_identity_hash="2" * 64,
    )
    session.add(target)
    session.commit()
    preview = ArticleLinkOccurrencePreviewService(session, settings=_settings()).preview(art.id)
    occ0 = preview.occurrences[0]
    ArticleLinkSubstitutionService(session).create_mapping(
        article_id=art.id, occurrence_identity_hash=occ0.occurrence_identity_hash,
        original_href=occ0.original_href, affiliate_link_target_id=target.id,
    )
    _succeed_run(session, manifest=[_matching_manifest_entry(target)])
    prepared = ArticlePublicationPreparationService(session, settings=_settings()).prepare(art.id)
    artifact = ArticlePublicationArtifactPersistenceService(session).persist(prepared)

    insp = _inspect(session, artifact.id)
    full_repr = repr(insp)
    assert insp.manifest_summary[0].current.destination_host == "aff.example.test"
    assert "SECRET123" not in full_repr
    assert "aff_id=" not in full_repr
    assert "?aff_id=SECRET123&sub=abc" not in full_repr
    assert _REAL_TOKEN not in full_repr


# -- mapping current state ---------------------------------------------------
def test_current_evidence_mapping_active(session: Session) -> None:
    _art, _target, artifact = _persisted_artifact_with_substitution(session)
    insp = _inspect(session, artifact.id)
    c = insp.manifest_summary[0].current
    assert c.resolvable is True
    assert c.mapping_status == "active"


def test_current_evidence_mapping_revoked_shown_not_blocked(session: Session) -> None:
    art, target, artifact = _persisted_artifact_with_substitution(session)
    mapping_id = insp_before = None
    insp_before = _inspect(session, artifact.id)
    mapping_id = insp_before.manifest_summary[0].mapping_id

    from app.repositories.article_link_substitution_mapping_repository import (
        ArticleLinkSubstitutionMappingRepository,
    )

    repo = ArticleLinkSubstitutionMappingRepository(session)
    mapping = repo.get_by_id(mapping_id)
    repo.revoke(mapping)
    session.commit()

    insp = _inspect(session, artifact.id)
    c = insp.manifest_summary[0].current
    assert c.resolvable is True  # revoked はまだ同じ行 -- identity は一致する
    assert c.mapping_status == "revoked"
    assert insp.all_current_evidence_resolvable is True  # D-E1: 承認はブロックしない


def test_current_evidence_mapping_missing_fails_closed(session: Session) -> None:
    _art, _target, artifact = _persisted_artifact_with_substitution(session)
    _corrupt_manifest_field(session, artifact, field="mapping_id", value=999999)

    insp = _inspect(session, artifact.id)
    c = insp.manifest_summary[0].current
    assert c.resolvable is False
    assert c.fail_reason == "mapping_missing"
    assert c.mapping_status is None and c.destination_host is None
    assert insp.all_current_evidence_resolvable is False


def test_current_evidence_mapping_identity_mismatch_fails_closed(session: Session) -> None:
    _art, _target, artifact = _persisted_artifact_with_substitution(session)
    _corrupt_manifest_field(
        session, artifact, field="original_href", value="https://tampered.example.test/x"
    )

    insp = _inspect(session, artifact.id)
    c = insp.manifest_summary[0].current
    assert c.resolvable is False
    assert c.fail_reason == "mapping_identity_mismatch"
    assert insp.all_current_evidence_resolvable is False


# -- target current state -----------------------------------------------------
def test_current_evidence_target_active(session: Session) -> None:
    _art, _target, artifact = _persisted_artifact_with_substitution(session)
    insp = _inspect(session, artifact.id)
    assert insp.manifest_summary[0].current.target_status == "active"


def test_current_evidence_target_disabled_shown_not_blocked(session: Session) -> None:
    art, target, artifact = _persisted_artifact_with_substitution(session)
    target.status = "disabled"
    session.commit()

    insp = _inspect(session, artifact.id)
    c = insp.manifest_summary[0].current
    assert c.resolvable is True
    assert c.target_status == "disabled"
    assert insp.all_current_evidence_resolvable is True


def test_current_evidence_target_superseded_shown_not_blocked(session: Session) -> None:
    art, target, artifact = _persisted_artifact_with_substitution(session)
    target.status = "superseded"
    session.commit()

    insp = _inspect(session, artifact.id)
    assert insp.manifest_summary[0].current.target_status == "superseded"


def test_current_evidence_target_missing_fails_closed(session: Session) -> None:
    """D-E1.1 (Defect A の是正): 単に manifest の affiliate_link_target_id だけを
    書き換えると mapping.affiliate_link_target_id との突き合わせで
    mapping_identity_mismatch が先に発生してしまい FAIL_TARGET_MISSING には
    絶対に到達しない (D-E1 FINAL DIFF REVIEW で指摘された false coverage)。

    ここでは mapping 自体を repository 経由で直接、実在しない target_id を
    指すように作る (``ArticleLinkSubstitutionService.create_mapping`` の target
    存在チェックを経由しないことで、mapping.affiliate_link_target_id と
    manifest.affiliate_link_target_id を **完全に一致** させたまま、その
    target_id を持つ行そのものを一度も作らない)。SQLite テストは FK を強制
    しないため (production FK は無傷のまま)、これは既存の
    ``test_inspection_reports_article_not_found_status`` と同じ「forged row」
    手法の応用。mapping binding は完全に通過し、target lookup だけが None に
    なることを保証する。"""

    from app.repositories.article_link_substitution_mapping_repository import (
        ArticleLinkSubstitutionMappingRepository,
    )

    art = _seed_article(session, slug="p-target-missing")
    missing_target_id = 999999
    occurrence_identity_hash = "3" * 64
    fake_token = "GHOSTTARGETTOKEN0001AA"

    mapping = ArticleLinkSubstitutionMappingRepository(session).add_active(
        article_id=art.id,
        occurrence_identity_hash=occurrence_identity_hash,
        original_href=_HREF,
        affiliate_link_target_id=missing_target_id,
        approved_at=datetime.now(UTC),
    )
    session.commit()

    entry = {
        "occurrence_ordinal": 0,
        "occurrence_identity_hash": occurrence_identity_hash,
        "mapping_id": mapping.id,
        "affiliate_link_target_id": missing_target_id,
        "token": fake_token,
        "target_projection_version": PROJECTION_VERSION_ACTIVE,
        "original_href": _HREF,
        "replacement_href": expected_replacement_href(fake_token),
        "rel_before": "",
        "rel_after": "sponsored nofollow noopener noreferrer",
    }
    prepared = ArticlePublicationPreparationService(session, settings=_settings()).prepare(art.id)
    forged = ArticlePublicationArtifactRepository(session).add(
        article_id=art.id,
        canonical_body_hash=prepared.canonical_body_hash,
        renderer_version=prepared.renderer_version,
        artifact_schema_version=prepared.artifact_schema_version,
        substitution_manifest_json=json.dumps([entry]),
        artifact_hash="e" * 64,
        tracked_html=prepared.tracked_html,
        tracked_html_hash=prepared.tracked_html_hash,
        substitution_count=1,
        approved_at=None,
        approved_artifact_hash=None,
        generated_at=datetime.now(UTC),
    )
    session.commit()

    insp = _inspect(session, forged.id)
    assert insp.manifest_valid is True  # 形は正しい -- 検証したいのは current evidence 側
    assert len(insp.manifest_summary) == 1
    c = insp.manifest_summary[0].current
    assert c.resolvable is False
    assert c.fail_reason == "target_missing"
    # 何も捏造しない -- unresolvable のとき他の current フィールドは全て None。
    assert c.mapping_status is None
    assert c.target_status is None
    assert c.program_name is None
    assert c.destination_host is None
    assert c.projection_eligible is None
    assert c.host_policy_eligible is None
    assert insp.all_current_evidence_resolvable is False


def test_current_evidence_target_token_mismatch_fails_closed(session: Session) -> None:
    _art, _target, artifact = _persisted_artifact_with_substitution(session)
    fake_token = "WRONGTOKENVALUE12345AA"
    # replacement_href も一致させて manifest 自体の形/自己整合性は保つ -- 検証
    # したいのは「real target.token と manifest.token の突き合わせ」だけ。
    _corrupt_manifest_fields(
        session, artifact,
        token=fake_token, replacement_href=expected_replacement_href(fake_token),
    )

    insp = _inspect(session, artifact.id)
    c = insp.manifest_summary[0].current
    assert c.resolvable is False
    assert c.fail_reason == "target_identity_mismatch"


# -- program current state -----------------------------------------------------
def test_current_evidence_program_active(session: Session) -> None:
    _art, _target, artifact = _persisted_artifact_with_substitution(session)
    insp = _inspect(session, artifact.id)
    c = insp.manifest_summary[0].current
    assert c.program_status == "active"
    assert c.program_name == "Test ASP"
    assert c.program_provider == "test-asp"


def test_current_evidence_program_paused_shown_not_blocked(session: Session) -> None:
    art, target, artifact = _persisted_artifact_with_substitution(session)
    program = session.get(AffiliateProgram, target.affiliate_program_id)
    program.status = "paused"
    session.commit()

    insp = _inspect(session, artifact.id)
    c = insp.manifest_summary[0].current
    assert c.resolvable is True
    assert c.program_status == "paused"
    assert insp.all_current_evidence_resolvable is True


def test_current_evidence_program_missing_fails_closed(session: Session) -> None:
    """D-E1.1 (Defect B の是正): FAIL_PROGRAM_MISSING に実際に到達させる。
    mapping/target の binding は完全に一致させたまま (target.article_id /
    target.token とも一致)、target.affiliate_program_id だけが実在しない
    program を指すようにする。D-D3 の ``ArticleLinkOccurrencePreviewService.
    resolve_eligibility`` は program を一切参照しないため、この target は
    通常どおり substitution 対象になり artifact に載る -- D-E1 の inspection
    だけがこの欠落に気づく、という現実的なシナリオ。"""

    art = _seed_article(session, slug="p-program-missing")
    missing_program_id = 888888
    target = AffiliateLinkTarget(
        token="GHOSTPROGRAMTOKEN001AA", article_id=art.id,
        affiliate_program_id=missing_program_id,
        destination_url="https://aff.example.test/y", destination_host="aff.example.test",
        status="active", link_identity_hash="4" * 64,
    )
    session.add(target)
    session.commit()

    preview = ArticleLinkOccurrencePreviewService(session, settings=_settings()).preview(art.id)
    occ0 = preview.occurrences[0]
    ArticleLinkSubstitutionService(session).create_mapping(
        article_id=art.id, occurrence_identity_hash=occ0.occurrence_identity_hash,
        original_href=occ0.original_href, affiliate_link_target_id=target.id,
    )
    _succeed_run(session, manifest=[_matching_manifest_entry(target)])
    prepared = ArticlePublicationPreparationService(session, settings=_settings()).prepare(art.id)
    artifact = ArticlePublicationArtifactPersistenceService(session).persist(prepared)

    insp = _inspect(session, artifact.id)
    assert len(insp.manifest_summary) == 1
    c = insp.manifest_summary[0].current
    assert c.resolvable is False
    assert c.fail_reason == "program_missing"
    # target/program の current 値は何も捏造しない。
    assert c.mapping_status is None
    assert c.target_status is None
    assert c.program_name is None
    assert c.destination_host is None
    assert insp.all_current_evidence_resolvable is False


# -- projection / host-policy eligibility --------------------------------------
def test_current_evidence_projection_eligible_true_with_matching_succeeded_run(
    session: Session,
) -> None:
    _art, _target, artifact = _persisted_artifact_with_substitution(session)
    insp = _inspect(session, artifact.id)
    assert insp.manifest_summary[0].current.projection_eligible is True
    assert insp.manifest_summary[0].current.current_projection_version == PROJECTION_VERSION_ACTIVE


def test_zero_substitution_when_no_push_run_makes_occurrence_ineligible_upstream(
    session: Session,
) -> None:
    """D-E1.1 での命名訂正 (旧 test_current_evidence_projection_eligible_false_
    without_any_push_run): これは CurrentOccurrenceEvidence.projection_eligible
    の検証ではない -- push run が無いと D-D3 の
    ``ArticleLinkOccurrencePreviewService.resolve_eligibility`` 自体がこの
    occurrence を substitution 対象から除外するため、manifest に一切
    エントリが現れず (resolvable/projection_eligible を持つ行が存在しない)、
    substitution_count=0 になるという **upstream の eligibility gate** の
    確認に過ぎない。resolvable=True かつ projection_eligible=False という
    実際の CURRENT 警告シナリオは、次の
    ``test_current_evidence_projection_eligible_false_when_latest_push_omits_target``
    で別途検証する。"""

    art = _seed_article(session, slug="p-no-push-run")
    target = _seed_target(session, article_id=art.id)
    preview = ArticleLinkOccurrencePreviewService(session, settings=_settings()).preview(art.id)
    occ0 = preview.occurrences[0]
    ArticleLinkSubstitutionService(session).create_mapping(
        article_id=art.id, occurrence_identity_hash=occ0.occurrence_identity_hash,
        original_href=occ0.original_href, affiliate_link_target_id=target.id,
    )
    # 意図的に push run を一切作らない -- preparation が UPDATE_REQUIRED/eligible
    # 経路に乗らないため、manifest が空になる可能性がある。substitution は
    # eligibility に依存するので、この場合 substitution_count は 0 になる。
    prepared = ArticlePublicationPreparationService(session, settings=_settings()).prepare(art.id)
    artifact = ArticlePublicationArtifactPersistenceService(session).persist(prepared)

    insp = _inspect(session, artifact.id)
    # push run が無いので、そもそも substitution 対象にならない (eligible=False
    # だったため manifest に一切現れない) -- D-E1 は「対象にならなかった」ことを
    # 正しく substitution_count=0 として表現する。
    assert insp.substitution_count == 0
    assert insp.manifest_summary == []


def test_current_evidence_projection_eligible_false_when_latest_push_omits_target(
    session: Session,
) -> None:
    """D-E1.1 (Defect C の是正): resolvable=True の実在する substituted occurrence
    に対して projection_eligible だけが False になる、現実的な CURRENT 警告
    シナリオ。artifact は target が eligible な時点で正しく構築される
    (``_persisted_artifact_with_substitution`` が既に一致する succeeded push を
    作る)。その後、**別の (無関係な) target だけを含む、より新しい succeeded
    push run** を追加する -- ``resolve_latest_acknowledgement`` は最新の
    succeeded run の manifest だけを authoritative とするため、元の target は
    最新の acknowledgement から抜け落ち、``is_target_eligible`` が False を返す。
    mapping/target/program の identity binding は一切崩していないので
    resolvable は True のまま -- 純粋な運用上の警告であることを証明する。"""

    art, target, artifact = _persisted_artifact_with_substitution(session)

    # 別 program を使う -- 同一 (article_id, affiliate_program_id) には active な
    # target を 2 つ同時に持てない (partial unique index)。
    other_program = _seed_program(session)
    other_target = AffiliateLinkTarget(
        token="OTHERTARGETTOKEN0001AA", article_id=art.id,
        affiliate_program_id=other_program.id,
        destination_url="https://other.example.test/z", destination_host="other.example.test",
        status="active", link_identity_hash="5" * 64,
    )
    session.add(other_target)
    session.commit()
    _succeed_run(session, manifest=[_matching_manifest_entry(other_target)])

    insp = _inspect(session, artifact.id)
    assert len(insp.manifest_summary) == 1
    c = insp.manifest_summary[0].current
    assert c.resolvable is True  # identity binding は崩れていない
    assert c.projection_eligible is False  # 最新の acknowledgement から抜け落ちた
    # 他の current evidence は普通に解決される -- unresolvable ではないことの証明。
    assert c.mapping_status == "active"
    assert c.target_status == "active"
    assert c.program_provider == "test-asp"
    assert c.destination_host == "aff.example.test"


def test_current_evidence_host_policy_eligible_false_under_real_default_policy(
    session: Session,
) -> None:
    """D-E1 §12: 現在の DEFAULT_DESTINATION_HOST_POLICY は空 (fail closed) のため、
    fake/test シナリオが host_policy_eligible=False を示すのは正しい挙動。
    D-E1 ではこのデフォルトを本番コードで書き換えない。"""

    _art, _target, artifact = _persisted_artifact_with_substitution(session)
    insp = _inspect(session, artifact.id)
    assert insp.manifest_summary[0].current.host_policy_eligible is False


# -- no DB mutation from inspection --------------------------------------------
def test_inspection_and_current_evidence_resolution_cause_zero_db_writes(
    session: Session,
) -> None:
    _art, _target, artifact = _persisted_artifact_with_substitution(session)
    session.commit()  # baseline: nothing pending after fixture setup

    _inspect(session, artifact.id)
    _inspect(session, artifact.id)  # 2 回呼んでも副作用が無いことも確認

    assert len(session.new) == 0
    assert len(session.dirty) == 0
    assert len(session.deleted) == 0
