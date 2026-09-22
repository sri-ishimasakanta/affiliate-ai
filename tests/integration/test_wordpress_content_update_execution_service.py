"""WordPressContentUpdateExecutionService の統合テスト (D-D5D)。

WordPress へは一切実通信しない (fake client を注入)。``execute()`` は
CONTENT_NOOP/drift/local-gate 失敗のとき 0 DB write、UPDATE_REQUIRED のときだけ
Transaction A (running) -> 高々 1 回の POST -> mandatory read-back -> Transaction B
(succeeded/outcome_unknown) を行う。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.article.draft_promotion_canonical import compute_text_hash
from app.config.settings import get_settings
from app.exceptions import (
    WordPressContentUpdateRunError,
    WordPressContentUpdateTerminalPersistFailedError,
)
from app.models import (
    Article,
    ArticlePublicationArtifact,
    WordPressContentUpdateRun,
    WordPressDraftRun,
    WordPressPublicationRun,
)
from app.models.wordpress_content_update_run import (
    WP_CONTENT_UPDATE_OUTCOME_UNKNOWN,
    WP_CONTENT_UPDATE_RUNNING,
    WP_CONTENT_UPDATE_SUCCEEDED,
)
from app.models.wordpress_draft_run import WP_RUN_SUCCEEDED
from app.models.wordpress_publication_run import WP_PUBRUN_SUCCEEDED
from app.repositories.wordpress_content_update_run_repository import (
    WordPressContentUpdateRunRepository,
)
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
    OUTCOME_OUTCOME_UNKNOWN,
    OUTCOME_SUCCEEDED,
    WordPressContentUpdateExecutionService,
)
from app.services.wordpress_content_update_preflight_service import (
    CLASSIFICATION_CONTENT_NOOP,
    CLASSIFICATION_UPDATE_REQUIRED,
    CLASSIFICATION_WORDPRESS_CURRENT_CONTENT_DRIFT,
    REASON_ARTICLE_NOT_FOUND,
)
from app.wordpress.client import WordPressUpdatedPost
from app.wordpress.content_update_request import build_wordpress_content_update_request
from app.wordpress.target import canonicalize_wordpress_base_url

_HREF = "https://official.example.test/tool-a"
_WP_POST_ID = "25"
_TARGET_BASE_URL = "https://bizfluxlab.com"


@pytest.fixture
def wp_env(monkeypatch):
    monkeypatch.setenv("WORDPRESS_BASE_URL", _TARGET_BASE_URL)
    monkeypatch.setenv("WORDPRESS_USERNAME", "wp-user-secret")
    monkeypatch.setenv("WORDPRESS_APP_PASSWORD", "aaaa bbbb cccc dddd")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _FakeWordPressClient:
    """テスト専用 fake -- 実ネットワークへは一切到達しない。

    ``get_responses``/``get_excs`` は 1-based の呼び出し順で消費される
    (1 回目 = preflight GET, 2 回目 = post-write read-back GET)。
    """

    def __init__(
        self,
        *,
        get_responses: list[dict] | None = None,
        get_excs: dict[int, BaseException] | None = None,
        post_response: WordPressUpdatedPost | None = None,
        post_exc: BaseException | None = None,
        on_post=None,
    ):
        self._get_responses = list(get_responses) if get_responses else []
        self._get_excs = dict(get_excs) if get_excs else {}
        self._post_response = post_response
        self._post_exc = post_exc
        self._on_post = on_post
        self.get_calls = 0
        self.post_calls = 0

    def get_post(self, post_id: int) -> dict:
        self.get_calls += 1
        if self.get_calls in self._get_excs:
            raise self._get_excs[self.get_calls]
        idx = self.get_calls - 1
        if idx < len(self._get_responses):
            return self._get_responses[idx]
        raise AssertionError(f"unexpected extra get_post call #{self.get_calls}")

    def update_post_content_exact(self, post_id: int, payload_json: str) -> WordPressUpdatedPost:
        self.post_calls += 1
        if self._on_post is not None:
            self._on_post()
        if self._post_exc is not None:
            raise self._post_exc
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


def _seed_article_and_artifact(
    session: Session, *, slug: str = "p1", wordpress_post_id: int | None = 25
) -> tuple[Article, ArticlePublicationArtifact]:
    art = Article(
        title="t",
        slug=slug,
        keyword_id=None,
        body=f"[tool]({_HREF})\n",
        wordpress_post_id=wordpress_post_id,
    )
    session.add(art)
    session.commit()

    prepared = ArticlePublicationPreparationService(session, settings=get_settings()).prepare(
        art.id
    )
    artifact = ArticlePublicationArtifactPersistenceService(session).persist(prepared)
    ArticlePublicationArtifactService(session).approve_artifact(
        artifact.id, expected_artifact_hash=artifact.artifact_hash
    )
    session.refresh(artifact)
    return art, artifact


def _add_draft_run(
    session: Session,
    article: Article,
    *,
    rendered_content_hash: str,
    wordpress_post_id: str = _WP_POST_ID,
) -> WordPressDraftRun:
    run = WordPressDraftRun(
        article_id=article.id,
        source_promotion_id=1,
        status=WP_RUN_SUCCEEDED,
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
    wordpress_post_id: str = _WP_POST_ID,
) -> WordPressPublicationRun:
    run = WordPressPublicationRun(
        article_id=article.id,
        source_wordpress_draft_run_id=draft_run.id,
        status=WP_PUBRUN_SUCCEEDED,
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


_WP_STORED_RAW = "wordpress-stored-raw-content"


def _noop_scenario(session: Session, *, slug: str = "p1"):
    """candidate == last-submitted (CONTENT_NOOP path)."""

    art, artifact = _seed_article_and_artifact(session, slug=slug)
    submitted_hash = artifact.tracked_html_hash
    raw_hash = compute_text_hash(_WP_STORED_RAW)
    draft = _add_draft_run(session, art, rendered_content_hash=submitted_hash)
    pub = _add_publication_run(session, art, draft, wordpress_raw_content_hash=raw_hash)
    return art, artifact, draft, pub


def _update_required_scenario(session: Session, *, slug: str = "p1"):
    """candidate != last-submitted, but expected raw baseline is known (UPDATE_REQUIRED
    once the live preflight confirms expected == observed)."""

    art, artifact = _seed_article_and_artifact(session, slug=slug)
    old_submitted_hash = compute_text_hash("<p>OLD, never equal to the real artifact</p>")
    assert old_submitted_hash != artifact.tracked_html_hash
    raw_hash = compute_text_hash(_WP_STORED_RAW)
    draft = _add_draft_run(session, art, rendered_content_hash=old_submitted_hash)
    pub = _add_publication_run(session, art, draft, wordpress_raw_content_hash=raw_hash)
    return art, artifact, draft, pub


def _row_counts(session: Session):
    return (
        session.scalar(select(func.count()).select_from(Article)),
        session.scalar(select(func.count()).select_from(ArticlePublicationArtifact)),
        session.scalar(select(func.count()).select_from(WordPressPublicationRun)),
        session.scalar(select(func.count()).select_from(WordPressDraftRun)),
        session.scalar(select(func.count()).select_from(WordPressContentUpdateRun)),
    )


# ==================== §26: CONTENT_NOOP =========================================
def test_content_noop_makes_no_writes(session: Session, wp_env) -> None:
    art, artifact, draft, pub = _noop_scenario(session)
    before = _row_counts(session)

    fake = _FakeWordPressClient(get_responses=[_wp_response(raw=_WP_STORED_RAW)])
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    assert result.preflight_classification == CLASSIFICATION_CONTENT_NOOP
    assert result.executed is False
    assert result.run_id is None
    assert result.run_status is None
    assert fake.get_calls == 1
    assert fake.post_calls == 0
    assert _row_counts(session) == before


# ==================== §27: remote drift ==========================================
def test_remote_drift_makes_no_writes(session: Session, wp_env) -> None:
    art, artifact, draft, pub = _noop_scenario(session)
    before = _row_counts(session)

    fake = _FakeWordPressClient(get_responses=[_wp_response(raw="totally different")])
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    assert result.preflight_classification == CLASSIFICATION_WORDPRESS_CURRENT_CONTENT_DRIFT
    assert result.executed is False
    assert result.run_id is None
    assert fake.get_calls == 1
    assert fake.post_calls == 0
    assert _row_counts(session) == before


def test_local_gate_failure_makes_no_writes_and_no_network(session: Session, wp_env) -> None:
    fake = _FakeWordPressClient(get_responses=[_wp_response()])
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    result = svc.execute(article_id=999999, artifact_id=1, artifact_hash="a" * 64)

    assert result.preflight_classification is None
    assert result.reason_code == REASON_ARTICLE_NOT_FOUND
    assert result.executed is False
    assert fake.get_calls == 0
    assert fake.post_calls == 0


# ==================== §28: successful update =====================================
def test_successful_update_full_sequence(session: Session, wp_env) -> None:
    art, artifact, draft, pub = _update_required_scenario(session)

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
    assert run.request_content_hash == artifact.tracked_html_hash
    assert (
        run.expected_pre_update_wordpress_raw_content_hash
        == run.observed_pre_update_wordpress_raw_content_hash
    )
    assert run.response_content_raw_hash == compute_text_hash("post-write raw content")
    # no request-vs-raw equality assumption
    assert run.response_content_raw_hash != run.request_content_hash
    assert run.finished_at is not None
    assert run.started_at is not None


# ==================== §29: Transaction A durable BEFORE the POST =================
def test_running_row_durable_before_post_via_independent_session(
    session: Session, wp_env, engine
) -> None:
    art, artifact, draft, pub = _update_required_scenario(session)
    article_id, artifact_id, artifact_hash = art.id, artifact.id, artifact.artifact_hash

    independent_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    observed = {}

    def _on_post():
        indep = independent_factory()
        try:
            rows = (
                indep.execute(
                    select(WordPressContentUpdateRun).where(
                        WordPressContentUpdateRun.article_id == article_id
                    )
                )
                .scalars()
                .all()
            )
            observed["count"] = len(rows)
            observed["status"] = rows[0].status if rows else None
        finally:
            indep.close()

    fake = _FakeWordPressClient(
        get_responses=[
            _wp_response(raw=_WP_STORED_RAW),
            _wp_response(raw="post-write raw content"),
        ],
        post_response=WordPressUpdatedPost(
            id=25, status="publish", link="https://bizfluxlab.com/?p=25"
        ),
        on_post=_on_post,
    )
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    svc.execute(article_id=article_id, artifact_id=artifact_id, artifact_hash=artifact_hash)

    assert observed["count"] == 1
    assert observed["status"] == WP_CONTENT_UPDATE_RUNNING


# ==================== §30: Transaction A failure =================================
def test_transaction_a_failure_makes_zero_post(session: Session, wp_env, monkeypatch) -> None:
    art, artifact, draft, pub = _update_required_scenario(session)

    fake = _FakeWordPressClient(get_responses=[_wp_response(raw=_WP_STORED_RAW)])
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)

    def _boom(*_a, **_kw):
        raise RuntimeError("simulated add_running failure")

    monkeypatch.setattr(WordPressContentUpdateRunRepository, "add_running", _boom)

    with pytest.raises(RuntimeError, match="simulated add_running failure"):
        svc.execute(
            article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
        )

    assert fake.post_calls == 0
    assert session.scalar(select(func.count()).select_from(WordPressContentUpdateRun)) == 0


# ==================== §31: ambiguous POST =========================================
def test_ambiguous_transport_after_post_boundary_is_outcome_unknown(
    session: Session, wp_env
) -> None:
    from app.exceptions import WordPressAmbiguousOutcomeError

    art, artifact, draft, pub = _update_required_scenario(session)
    fake = _FakeWordPressClient(
        get_responses=[_wp_response(raw=_WP_STORED_RAW)],
        post_exc=WordPressAmbiguousOutcomeError("timeout after send"),
    )
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    assert result.run_status == WP_CONTENT_UPDATE_OUTCOME_UNKNOWN
    assert result.reason_code == OUTCOME_OUTCOME_UNKNOWN
    assert fake.post_calls == 1
    assert fake.get_calls == 1  # no read-back retry after an ambiguous POST


def test_generic_ambiguous_provider_failure_is_outcome_unknown(session: Session, wp_env) -> None:
    from app.exceptions import ExternalProviderError

    art, artifact, draft, pub = _update_required_scenario(session)
    fake = _FakeWordPressClient(
        get_responses=[_wp_response(raw=_WP_STORED_RAW)],
        post_exc=ExternalProviderError("wordpress", "unexpected response status 500"),
    )
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    assert result.run_status == WP_CONTENT_UPDATE_OUTCOME_UNKNOWN
    assert fake.post_calls == 1
    assert fake.get_calls == 1


def test_post_response_post_id_mismatch_is_outcome_unknown(session: Session, wp_env) -> None:
    art, artifact, draft, pub = _update_required_scenario(session)
    fake = _FakeWordPressClient(
        get_responses=[_wp_response(raw=_WP_STORED_RAW)],
        post_response=WordPressUpdatedPost(id=999, status="publish", link=None),
    )
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    assert result.run_status == WP_CONTENT_UPDATE_OUTCOME_UNKNOWN
    assert fake.post_calls == 1
    assert fake.get_calls == 1  # no read-back attempted after an unverifiable POST id


# ==================== §32: definitive failure =====================================
def test_no_definitive_failed_path_exists_in_current_client_contract(
    session: Session, wp_env
) -> None:
    """D-D5D §32: 既存 WordPressClient 契約は zero-effect を証明できる別の例外型を
    公開していない -- ExternalProviderError/WordPressAmbiguousOutcomeError はどちらも
    outcome_unknown に帰着する。failed への遷移経路は現時点で存在しないことを pin する。
    """

    from app.exceptions import ExternalProviderError

    art, artifact, draft, pub = _update_required_scenario(session)
    fake = _FakeWordPressClient(
        get_responses=[_wp_response(raw=_WP_STORED_RAW)],
        post_exc=ExternalProviderError("wordpress", "authentication failed (401)"),
    )
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    # even a 401-shaped ExternalProviderError -- the client contract does not expose
    # a structural way to distinguish it from a 500 -- becomes outcome_unknown, not failed.
    assert result.run_status == WP_CONTENT_UPDATE_OUTCOME_UNKNOWN
    assert fake.post_calls == 1


# ==================== §33: post-success / read-back-failure ======================
def test_post_success_then_readback_timeout_is_outcome_unknown(session: Session, wp_env) -> None:
    art, artifact, draft, pub = _update_required_scenario(session)
    fake = _FakeWordPressClient(
        get_responses=[_wp_response(raw=_WP_STORED_RAW)],
        get_excs={2: RuntimeError("readback timeout")},
        post_response=WordPressUpdatedPost(id=25, status="publish", link=None),
    )
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    assert result.run_status == WP_CONTENT_UPDATE_OUTCOME_UNKNOWN
    assert fake.post_calls == 1
    assert fake.get_calls == 2  # preflight + attempted read-back (no retry beyond that)


def test_post_success_then_malformed_readback_is_outcome_unknown(session: Session, wp_env) -> None:
    art, artifact, draft, pub = _update_required_scenario(session)
    malformed = _wp_response(raw=_WP_STORED_RAW)
    malformed["content"] = {"rendered": "only rendered, no raw"}
    fake = _FakeWordPressClient(
        get_responses=[_wp_response(raw=_WP_STORED_RAW), malformed],
        post_response=WordPressUpdatedPost(id=25, status="publish", link=None),
    )
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    assert result.run_status == WP_CONTENT_UPDATE_OUTCOME_UNKNOWN
    assert fake.post_calls == 1


def test_post_success_then_readback_status_mismatch_is_outcome_unknown(
    session: Session, wp_env
) -> None:
    art, artifact, draft, pub = _update_required_scenario(session)
    fake = _FakeWordPressClient(
        get_responses=[
            _wp_response(raw=_WP_STORED_RAW),
            _wp_response(raw="post-write raw content", status="draft"),
        ],
        post_response=WordPressUpdatedPost(id=25, status="publish", link=None),
    )
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    assert result.run_status == WP_CONTENT_UPDATE_OUTCOME_UNKNOWN
    assert fake.post_calls == 1
    assert fake.get_calls == 2


# ==================== §34: Transaction B commit failure ==========================
def test_transaction_b_commit_failure_raises_and_keeps_run_running(
    session: Session, wp_env, monkeypatch
) -> None:
    art, artifact, draft, pub = _update_required_scenario(session)
    fake = _FakeWordPressClient(
        get_responses=[
            _wp_response(raw=_WP_STORED_RAW),
            _wp_response(raw="post-write raw content"),
        ],
        post_response=WordPressUpdatedPost(id=25, status="publish", link=None),
    )
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)

    original_commit = session.commit
    calls = {"n": 0}

    def _commit_boom():
        calls["n"] += 1
        if calls["n"] == 2:  # 1st commit = Transaction A (must succeed); 2nd = Transaction B
            raise RuntimeError("simulated Transaction B commit failure")
        return original_commit()

    monkeypatch.setattr(session, "commit", _commit_boom)

    with pytest.raises(WordPressContentUpdateTerminalPersistFailedError):
        svc.execute(
            article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
        )

    assert fake.post_calls == 1  # no second POST

    monkeypatch.setattr(session, "commit", original_commit)
    run = session.scalars(
        select(WordPressContentUpdateRun).where(WordPressContentUpdateRun.article_id == art.id)
    ).first()
    assert run is not None
    assert run.status == WP_CONTENT_UPDATE_RUNNING  # Transaction A's durable state, unrewritten


# ==================== §35: blocking prior run =====================================
def test_prior_running_run_blocks_new_post(session: Session, wp_env) -> None:
    art, artifact, draft, pub = _update_required_scenario(session)
    repo = WordPressContentUpdateRunRepository(session)
    repo.add_running(
        article_id=art.id,
        wordpress_post_id=_WP_POST_ID,
        article_publication_artifact_id=artifact.id,
        artifact_hash=artifact.artifact_hash,
        method="POST",
        endpoint_path=f"/wp-json/wp/v2/posts/{_WP_POST_ID}",
        update_payload_json='{"content":"x"}',
        update_payload_hash=compute_text_hash("payload"),
        content_update_request_identity_hash=compute_text_hash("identity"),
        target_content_update_request_identity_hash=compute_text_hash("target-identity"),
        target_base_url=_TARGET_BASE_URL,
        request_content_hash=compute_text_hash("candidate"),
        expected_pre_update_wordpress_raw_content_hash="1" * 64,
        observed_pre_update_wordpress_raw_content_hash="1" * 64,
        started_at=datetime.now(UTC),
    )

    fake = _FakeWordPressClient(get_responses=[_wp_response(raw=_WP_STORED_RAW)])
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    assert result.executed is False
    assert fake.post_calls == 0


def test_prior_outcome_unknown_run_blocks_new_post(session: Session, wp_env) -> None:
    art, artifact, draft, pub = _update_required_scenario(session)
    repo = WordPressContentUpdateRunRepository(session)
    run = repo.add_running(
        article_id=art.id,
        wordpress_post_id=_WP_POST_ID,
        article_publication_artifact_id=artifact.id,
        artifact_hash=artifact.artifact_hash,
        method="POST",
        endpoint_path=f"/wp-json/wp/v2/posts/{_WP_POST_ID}",
        update_payload_json='{"content":"x"}',
        update_payload_hash=compute_text_hash("payload"),
        content_update_request_identity_hash=compute_text_hash("identity"),
        target_content_update_request_identity_hash=compute_text_hash("target-identity"),
        target_base_url=_TARGET_BASE_URL,
        request_content_hash=compute_text_hash("candidate"),
        expected_pre_update_wordpress_raw_content_hash="1" * 64,
        observed_pre_update_wordpress_raw_content_hash="1" * 64,
        started_at=datetime.now(UTC),
    )
    repo.mark_outcome_unknown(run, error_message="timeout", finished_at=datetime.now(UTC))

    fake = _FakeWordPressClient(get_responses=[_wp_response(raw=_WP_STORED_RAW)])
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    assert result.executed is False
    assert fake.post_calls == 0


# ==================== §36: idempotency ============================================
def test_idempotency_same_key_same_identity_reuses_existing_run(session: Session, wp_env) -> None:
    art, artifact, draft, pub = _update_required_scenario(session)
    fake = _FakeWordPressClient(
        get_responses=[
            _wp_response(raw=_WP_STORED_RAW),
            _wp_response(raw="post-write raw content"),
        ],
        post_response=WordPressUpdatedPost(id=25, status="publish", link=None),
    )
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    first = svc.execute(
        article_id=art.id,
        artifact_id=artifact.id,
        artifact_hash=artifact.artifact_hash,
        idempotency_key="key-1",
    )
    assert first.run_status == WP_CONTENT_UPDATE_SUCCEEDED
    assert fake.post_calls == 1

    # second call: since the content is now CONTENT_NOOP relative to the just-succeeded
    # run's own evidence, a fresh classify() should already report CONTENT_NOOP and never
    # reach the idempotency lookup. Prove no second POST regardless.
    second = svc.execute(
        article_id=art.id,
        artifact_id=artifact.id,
        artifact_hash=artifact.artifact_hash,
        idempotency_key="key-1",
    )
    assert second.executed is False
    assert fake.post_calls == 1


def test_idempotency_same_key_different_identity_conflicts(session: Session, wp_env) -> None:
    """既存 idempotency key が別の frozen request identity に紐づいている場合は
    conflict になる。この既存 run は *succeeded* かつ *この post に対する直近の
    succeeded run* として構成する -- outcome_unknown/running は D-D5A.1 の
    blocking-run gate 自体に先に捕捉されてしまい、idempotency-conflict 経路を
    検証できないため (blocking-run と idempotency-conflict は別々の gate)。
    succeeded な直近 run は D-D5C の baseline 解決にも使われるため、その
    evidence を意図的に「この artifact とは異なる過去の送信」に見えるよう設定し、
    fresh classify() が UPDATE_REQUIRED に到達できるようにする。"""

    art, artifact, draft, pub = _update_required_scenario(session)
    repo = WordPressContentUpdateRunRepository(session)
    prior_raw_hash = compute_text_hash(_WP_STORED_RAW)
    prior = repo.add_running(
        article_id=art.id,
        wordpress_post_id=_WP_POST_ID,
        article_publication_artifact_id=artifact.id,
        artifact_hash=artifact.artifact_hash,
        method="POST",
        endpoint_path=f"/wp-json/wp/v2/posts/{_WP_POST_ID}",
        update_payload_json='{"content":"different"}',
        update_payload_hash=compute_text_hash("different-payload"),
        content_update_request_identity_hash=compute_text_hash("a-totally-different-identity"),
        target_content_update_request_identity_hash=compute_text_hash("different-target-identity"),
        target_base_url=_TARGET_BASE_URL,
        request_content_hash=compute_text_hash("a-different-past-submission"),
        expected_pre_update_wordpress_raw_content_hash=prior_raw_hash,
        observed_pre_update_wordpress_raw_content_hash=prior_raw_hash,
        idempotency_key="shared-key",
        started_at=datetime.now(UTC),
    )
    repo.mark_succeeded(
        prior,
        http_status=200,
        response_content_raw_hash=prior_raw_hash,
        finished_at=datetime.now(UTC),
    )

    fake = _FakeWordPressClient(get_responses=[_wp_response(raw=_WP_STORED_RAW)])
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)

    with pytest.raises(WordPressContentUpdateRunError, match="idempotency_key"):
        svc.execute(
            article_id=art.id,
            artifact_id=artifact.id,
            artifact_hash=artifact.artifact_hash,
            idempotency_key="shared-key",
        )
    assert fake.post_calls == 0


def test_null_idempotency_key_never_conflicts(session: Session, wp_env) -> None:
    art, artifact, draft, pub = _noop_scenario(session)
    fake = _FakeWordPressClient(get_responses=[_wp_response(raw=_WP_STORED_RAW)])
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )
    assert result.executed is False  # CONTENT_NOOP; no idempotency lookup even reached


def test_no_automatic_retry_of_running_or_outcome_unknown(session: Session, wp_env) -> None:
    art, artifact, draft, pub = _update_required_scenario(session)
    repo = WordPressContentUpdateRunRepository(session)
    run = repo.add_running(
        article_id=art.id,
        wordpress_post_id=_WP_POST_ID,
        article_publication_artifact_id=artifact.id,
        artifact_hash=artifact.artifact_hash,
        method="POST",
        endpoint_path=f"/wp-json/wp/v2/posts/{_WP_POST_ID}",
        update_payload_json='{"content":"x"}',
        update_payload_hash=compute_text_hash("payload"),
        content_update_request_identity_hash=compute_text_hash("identity"),
        target_content_update_request_identity_hash=compute_text_hash("target-identity"),
        target_base_url=_TARGET_BASE_URL,
        request_content_hash=compute_text_hash("candidate"),
        expected_pre_update_wordpress_raw_content_hash="1" * 64,
        observed_pre_update_wordpress_raw_content_hash="1" * 64,
        idempotency_key="retry-key",
        started_at=datetime.now(UTC),
    )
    repo.mark_outcome_unknown(run, error_message="timeout", finished_at=datetime.now(UTC))

    fake = _FakeWordPressClient(get_responses=[_wp_response(raw=_WP_STORED_RAW)])
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    result = svc.execute(
        article_id=art.id,
        artifact_id=artifact.id,
        artifact_hash=artifact.artifact_hash,
        idempotency_key="retry-key",
    )
    assert result.executed is False
    assert fake.post_calls == 0


# ==================== §37: namespace regression ===================================
def test_successful_update_with_deliberately_different_namespaces(session: Session, wp_env) -> None:
    art, artifact, draft, pub = _update_required_scenario(session)
    fake = _FakeWordPressClient(
        get_responses=[
            _wp_response(raw=_WP_STORED_RAW),
            _wp_response(raw="a totally different post-write blob"),
        ],
        post_response=WordPressUpdatedPost(id=25, status="publish", link=None),
    )
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    assert result.run_status == WP_CONTENT_UPDATE_SUCCEEDED
    assert result.request_content_hash != result.expected_pre_update_wordpress_raw_content_hash
    assert result.response_content_raw_hash != result.request_content_hash
    assert result.response_content_raw_hash != result.expected_pre_update_wordpress_raw_content_hash


# ==================== §38: security/leak ==========================================
def test_execution_result_never_exposes_secrets_or_html(session: Session, wp_env) -> None:
    import dataclasses

    art, artifact, draft, pub = _update_required_scenario(session)
    fake = _FakeWordPressClient(
        get_responses=[
            _wp_response(raw=_WP_STORED_RAW),
            _wp_response(raw="post-write raw content"),
        ],
        post_response=WordPressUpdatedPost(id=25, status="publish", link=None),
    )
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    for value in dataclasses.astuple(result):
        if isinstance(value, str):
            assert artifact.tracked_html not in value
            assert "Authorization" not in value
            assert "wp-user-secret" not in value
            assert "aaaa bbbb cccc dddd" not in value


# ==================== §39: DB write boundaries ====================================
def test_only_update_required_causes_db_writes(session: Session, wp_env) -> None:
    noop_art, noop_artifact, *_ = _noop_scenario(session, slug="noop-slug")
    before = _row_counts(session)
    fake = _FakeWordPressClient(get_responses=[_wp_response(raw=_WP_STORED_RAW)])
    WordPressContentUpdateExecutionService(session, wordpress_client=fake).execute(
        article_id=noop_art.id,
        artifact_id=noop_artifact.id,
        artifact_hash=noop_artifact.artifact_hash,
    )
    assert _row_counts(session) == before

    upd_art, upd_artifact, upd_draft, upd_pub = _update_required_scenario(
        session, slug="update-slug"
    )
    before2 = _row_counts(session)
    fake2 = _FakeWordPressClient(
        get_responses=[
            _wp_response(raw=_WP_STORED_RAW),
            _wp_response(raw="post-write raw content"),
        ],
        post_response=WordPressUpdatedPost(id=25, status="publish", link=None),
    )
    WordPressContentUpdateExecutionService(session, wordpress_client=fake2).execute(
        article_id=upd_art.id, artifact_id=upd_artifact.id, artifact_hash=upd_artifact.artifact_hash
    )
    after2 = _row_counts(session)
    # exactly one new WordPressContentUpdateRun row; nothing else changed.
    assert after2[:4] == before2[:4]
    assert after2[4] == before2[4] + 1


# ==================== D-D5D.1: historical succeeded identity must NOT veto =========
# a legitimate re-execution when fresh D-D5C classification says UPDATE_REQUIRED.
# Historical success proves nothing about CURRENT WordPress state -- only the fresh
# preflight raw/content comparison is authoritative (D-D5D.1 §3).

_CONTENT_A_RAW = "raw content A -- the state being restored"
_CONTENT_B_RAW = "raw content B -- the state that superseded A"


def _ab_reversion_scenario(session: Session):
    """Article #1-shaped scenario where:

    - a historical succeeded ``WordPressContentUpdateRun`` (A) shares the EXACT
      target_content_update_request_identity_hash that a fresh execute() call
      against the current (single) approved artifact would compute today --
      i.e. the Human is trying to restore exactly this content again.
    - a LATER succeeded run (B), with a completely different frozen identity,
      is the current authoritative WordPress state (current raw == B's baseline,
      last-submitted content == B's request_content_hash).
    """

    art, artifact = _seed_article_and_artifact(session)
    draft = _add_draft_run(session, art, rendered_content_hash=compute_text_hash("irrelevant"))
    # publication run baseline is superseded by B below (latest succeeded content
    # update run always takes precedence per D-D5A.1 §3/§7 baseline resolution).
    _add_publication_run(
        session, art, draft, wordpress_raw_content_hash=compute_text_hash("irrelevant")
    )

    target_base_url = canonicalize_wordpress_base_url(_TARGET_BASE_URL)
    candidate_a = build_wordpress_content_update_request(
        wordpress_post_id=_WP_POST_ID,
        article_publication_artifact_id=artifact.id,
        artifact_hash=artifact.artifact_hash,
        tracked_html=artifact.tracked_html,
        target_base_url=target_base_url,
    )
    raw_a_hash = compute_text_hash(_CONTENT_A_RAW)
    raw_b_hash = compute_text_hash(_CONTENT_B_RAW)

    repo = WordPressContentUpdateRunRepository(session)

    # -- historical succeeded run A: EXACT same frozen identity as a fresh
    # execute() against the current artifact would produce today. --
    run_a = repo.add_running(
        article_id=art.id,
        wordpress_post_id=_WP_POST_ID,
        article_publication_artifact_id=artifact.id,
        artifact_hash=artifact.artifact_hash,
        method=candidate_a.method,
        endpoint_path=candidate_a.endpoint_path,
        update_payload_json=candidate_a.update_payload_json,
        update_payload_hash=candidate_a.update_payload_hash,
        content_update_request_identity_hash=candidate_a.content_update_request_identity_hash,
        target_content_update_request_identity_hash=(
            candidate_a.target_content_update_request_identity_hash
        ),
        target_base_url=target_base_url,
        request_content_hash=candidate_a.request_content_hash,
        expected_pre_update_wordpress_raw_content_hash=compute_text_hash("pre-A-raw"),
        observed_pre_update_wordpress_raw_content_hash=compute_text_hash("pre-A-raw"),
        started_at=datetime.now(UTC),
    )
    repo.mark_succeeded(
        run_a, http_status=200, response_content_raw_hash=raw_a_hash, finished_at=datetime.now(UTC)
    )

    # -- later succeeded run B: different frozen identity/content, now the
    # authoritative current WordPress state and submitted-content baseline. --
    run_b = repo.add_running(
        article_id=art.id,
        wordpress_post_id=_WP_POST_ID,
        article_publication_artifact_id=artifact.id,
        artifact_hash=artifact.artifact_hash,
        method="POST",
        endpoint_path=f"/wp-json/wp/v2/posts/{_WP_POST_ID}",
        update_payload_json='{"content":"content B"}',
        update_payload_hash=compute_text_hash("payload-b"),
        content_update_request_identity_hash=compute_text_hash("identity-b"),
        target_content_update_request_identity_hash=compute_text_hash("target-identity-b"),
        target_base_url=target_base_url,
        request_content_hash=compute_text_hash("content B (never equal to candidate A)"),
        expected_pre_update_wordpress_raw_content_hash=raw_a_hash,
        observed_pre_update_wordpress_raw_content_hash=raw_a_hash,
        started_at=datetime.now(UTC),
    )
    repo.mark_succeeded(
        run_b, http_status=200, response_content_raw_hash=raw_b_hash, finished_at=datetime.now(UTC)
    )

    return art, artifact, run_a, run_b, candidate_a


def test_ab_reversion_historical_a_does_not_suppress_fresh_update_required(
    session: Session, wp_env
) -> None:
    """D-D5D.1 §10 -- the critical regression test.

    Historical run A succeeded, was superseded by B, and the Human now wants to
    restore A. Fresh preflight must classify UPDATE_REQUIRED (current raw == B's
    baseline; candidate A != B's last-submitted hash) and execution must actually
    POST -- the mere existence of a historical succeeded run sharing A's identity
    must NOT suppress it.
    """

    art, artifact, run_a, run_b, candidate_a = _ab_reversion_scenario(session)

    fake = _FakeWordPressClient(
        get_responses=[
            _wp_response(raw=_CONTENT_B_RAW),  # preflight: current WP state == B
            _wp_response(raw=_CONTENT_A_RAW),  # post-write read-back: now == A again
        ],
        post_response=WordPressUpdatedPost(id=25, status="publish", link=None),
    )
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    assert result.preflight_classification == CLASSIFICATION_UPDATE_REQUIRED
    assert result.executed is True
    assert result.run_status == WP_CONTENT_UPDATE_SUCCEEDED
    assert fake.get_calls == 2  # exactly one preflight GET + one read-back GET
    assert fake.post_calls == 1  # exactly one POST for A

    # a NEW run was created -- the old succeeded A run was not reused.
    assert result.run_id != run_a.id
    assert result.run_id != run_b.id
    new_run = session.get(WordPressContentUpdateRun, result.run_id)
    assert new_run.request_content_hash == candidate_a.request_content_hash
    assert new_run.response_content_raw_hash == compute_text_hash(_CONTENT_A_RAW)

    # the old succeeded A run itself is untouched (still succeeded, still itself).
    old_a = session.get(WordPressContentUpdateRun, run_a.id)
    assert old_a.status == WP_CONTENT_UPDATE_SUCCEEDED
    assert old_a.id == run_a.id


def test_historical_a_latest_b_never_reports_already_succeeded(session: Session, wp_env) -> None:
    """D-D5D.1 §12 -- a historical succeeded run sharing the candidate's exact
    target identity must never, by itself, produce an "already succeeded"
    short-circuit. Only the fresh classification governs."""

    art, artifact, run_a, run_b, candidate_a = _ab_reversion_scenario(session)

    fake = _FakeWordPressClient(
        get_responses=[_wp_response(raw=_CONTENT_B_RAW), _wp_response(raw=_CONTENT_A_RAW)],
        post_response=WordPressUpdatedPost(id=25, status="publish", link=None),
    )
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    assert result.preflight_classification == CLASSIFICATION_UPDATE_REQUIRED
    assert result.reason_code != "ALREADY_SUCCEEDED"
    assert result.executed is True
    assert fake.post_calls == 1


def test_current_content_duplicate_is_still_content_noop_not_historical_lookup(
    session: Session, wp_env
) -> None:
    """D-D5D.1 §11 -- the normal duplicate-resend case (latest successful content ==
    candidate, current WordPress raw matches that baseline) is correctly caught by
    fresh D-D5C CONTENT_NOOP classification -- before any historical succeeded-run
    lookup would even matter. POST count = 0, new run count = 0."""

    art, artifact, draft, pub = _noop_scenario(session)
    before = _row_counts(session)

    fake = _FakeWordPressClient(get_responses=[_wp_response(raw=_WP_STORED_RAW)])
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    assert result.preflight_classification == CLASSIFICATION_CONTENT_NOOP
    assert result.executed is False
    assert result.run_id is None
    assert fake.post_calls == 0
    assert _row_counts(session) == before


# ==================== D-D5D.2: post-commit refresh elimination ===================
# session.commit() must be the LAST local DB operation at both the Transaction A
# and Transaction B boundaries. No refresh/query/flush of any kind afterward.


def _raise_if_refresh_called(*_a, **_kw):
    raise AssertionError(
        "session.refresh() must never be called by WordPressContentUpdateExecutionService "
        "(D-D5D.2) -- commit is the final local DB operation at every boundary"
    )


def test_transaction_a_never_refreshes_before_post(session: Session, wp_env, monkeypatch) -> None:
    """D-D5D.2 §9.A -- refresh() patched to raise immediately if called at all.
    A full successful UPDATE_REQUIRED execution must complete cleanly, proving no
    post-commit refresh sits between Transaction A's commit and the POST."""

    art, artifact, draft, pub = _update_required_scenario(session)
    fake = _FakeWordPressClient(
        get_responses=[
            _wp_response(raw=_WP_STORED_RAW),
            _wp_response(raw="post-write raw content"),
        ],
        post_response=WordPressUpdatedPost(id=25, status="publish", link=None),
    )
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    monkeypatch.setattr(session, "refresh", _raise_if_refresh_called)

    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    assert result.run_status == WP_CONTENT_UPDATE_SUCCEEDED
    assert fake.post_calls == 1
    assert fake.get_calls == 2  # preflight + read-back


def test_transaction_b_succeeded_never_refreshes(session: Session, wp_env, monkeypatch) -> None:
    """D-D5D.2 §9.B -- refresh() patched to raise. A successful update must still
    reach a durably-recorded succeeded terminal state, with no raw refresh
    exception surfacing and the result correctly reporting success."""

    art, artifact, draft, pub = _update_required_scenario(session)
    fake = _FakeWordPressClient(
        get_responses=[
            _wp_response(raw=_WP_STORED_RAW),
            _wp_response(raw="post-write raw content"),
        ],
        post_response=WordPressUpdatedPost(id=25, status="publish", link=None),
    )
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    monkeypatch.setattr(session, "refresh", _raise_if_refresh_called)

    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    assert result.executed is True
    assert result.run_status == WP_CONTENT_UPDATE_SUCCEEDED
    assert result.reason_code == OUTCOME_SUCCEEDED
    assert result.response_content_raw_hash == compute_text_hash("post-write raw content")


def test_transaction_b_outcome_unknown_never_refreshes(
    session: Session, wp_env, monkeypatch
) -> None:
    """D-D5D.2 §9.C -- refresh() patched to raise. An ambiguous POST outcome must
    still reach a durably-recorded outcome_unknown terminal state without any raw
    refresh exception."""

    from app.exceptions import WordPressAmbiguousOutcomeError

    art, artifact, draft, pub = _update_required_scenario(session)
    fake = _FakeWordPressClient(
        get_responses=[_wp_response(raw=_WP_STORED_RAW)],
        post_exc=WordPressAmbiguousOutcomeError("timeout after send"),
    )
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    monkeypatch.setattr(session, "refresh", _raise_if_refresh_called)

    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    assert result.run_status == WP_CONTENT_UPDATE_OUTCOME_UNKNOWN
    assert result.reason_code == OUTCOME_OUTCOME_UNKNOWN
    assert fake.post_calls == 1


def test_refresh_call_count_is_zero_for_full_execution(
    session: Session, wp_env, monkeypatch
) -> None:
    """D-D5D.2 §9.D -- exercise behavior (not source grep): count actual calls to
    session.refresh() across a full successful execute() and assert exactly 0."""

    # scenario/fixture setup happens BEFORE patching -- it legitimately calls
    # session.refresh() itself as unrelated test bookkeeping (see
    # _seed_article_and_artifact). Only calls made during execute() itself count.
    art, artifact, draft, pub = _update_required_scenario(session)
    fake = _FakeWordPressClient(
        get_responses=[
            _wp_response(raw=_WP_STORED_RAW),
            _wp_response(raw="post-write raw content"),
        ],
        post_response=WordPressUpdatedPost(id=25, status="publish", link=None),
    )
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)

    calls = {"n": 0}
    original_refresh = session.refresh

    def _counting_refresh(*a, **kw):
        calls["n"] += 1
        return original_refresh(*a, **kw)

    monkeypatch.setattr(session, "refresh", _counting_refresh)

    svc.execute(article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash)

    assert calls["n"] == 0


# ==================== D-D5D.2 §10: durable state via independent session =========
def test_succeeded_durable_via_independent_session_without_refresh(
    session: Session, wp_env, engine
) -> None:
    art, artifact, draft, pub = _update_required_scenario(session)
    fake = _FakeWordPressClient(
        get_responses=[
            _wp_response(raw=_WP_STORED_RAW),
            _wp_response(raw="post-write raw content"),
        ],
        post_response=WordPressUpdatedPost(id=25, status="publish", link=None),
    )
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )
    assert result.run_status == WP_CONTENT_UPDATE_SUCCEEDED

    independent_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    indep = independent_factory()
    try:
        row = indep.get(WordPressContentUpdateRun, result.run_id)
        assert row is not None
        assert row.status == WP_CONTENT_UPDATE_SUCCEEDED
        assert row.response_content_raw_hash == compute_text_hash("post-write raw content")
        assert row.finished_at is not None
    finally:
        indep.close()


def test_outcome_unknown_durable_via_independent_session_without_refresh(
    session: Session, wp_env, engine
) -> None:
    from app.exceptions import WordPressAmbiguousOutcomeError

    art, artifact, draft, pub = _update_required_scenario(session)
    fake = _FakeWordPressClient(
        get_responses=[_wp_response(raw=_WP_STORED_RAW)],
        post_exc=WordPressAmbiguousOutcomeError("timeout after send"),
    )
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )
    assert result.run_status == WP_CONTENT_UPDATE_OUTCOME_UNKNOWN

    independent_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    indep = independent_factory()
    try:
        row = indep.get(WordPressContentUpdateRun, result.run_id)
        assert row is not None
        assert row.status == WP_CONTENT_UPDATE_OUTCOME_UNKNOWN
        assert row.finished_at is not None
    finally:
        indep.close()


# ==================== C4.8: draft content update ==============================
def _draft_update_scenario(session: Session, *, slug: str):
    """公開実績が無く succeeded な draft run だけを持つ記事 (UPDATE_REQUIRED)。"""
    art, artifact = _seed_article_and_artifact(session, slug=slug)
    old = compute_text_hash("<p>OLD</p>")
    assert old != artifact.tracked_html_hash
    draft = _add_draft_run(session, art, rendered_content_hash=old)
    return art, artifact, draft


def test_draft_update_succeeds_and_verifies_against_draft_status(
    session: Session, wp_env
) -> None:
    """draft 更新の read-back は draft を期待する。

    publish 固定のままだと、書き込みが成功しても必ず outcome_unknown になる。
    """
    art, artifact, _draft = _draft_update_scenario(session, slug="exec-draft-ok")
    raw = "current draft raw"
    fake = _FakeWordPressClient(
        get_responses=[
            _wp_response(post_id=art.wordpress_post_id, status="draft", raw=raw),
            _wp_response(post_id=art.wordpress_post_id, status="draft",
                         raw="post-write draft raw"),
        ],
        post_response=WordPressUpdatedPost(
            id=art.wordpress_post_id, status="draft", link=None),
    )
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id,
        artifact_hash=artifact.artifact_hash,
        expected_wordpress_status="draft",
        expected_pre_update_raw_content_hash=compute_text_hash(raw),
    )

    assert result.preflight_classification == CLASSIFICATION_UPDATE_REQUIRED
    assert result.executed is True
    assert result.run_status == WP_CONTENT_UPDATE_SUCCEEDED
    assert fake.post_calls == 1
    run = session.get(WordPressContentUpdateRun, result.run_id)
    assert run.response_content_raw_hash is not None


def test_draft_update_reports_outcome_unknown_if_it_became_published(
    session: Session, wp_env
) -> None:
    """read-back の status が期待と違えば検証不能として扱う (成功と断定しない)。"""
    art, artifact, _draft = _draft_update_scenario(session, slug="exec-draft-flip")
    raw = "current draft raw"
    fake = _FakeWordPressClient(
        get_responses=[
            _wp_response(post_id=art.wordpress_post_id, status="draft", raw=raw),
            _wp_response(post_id=art.wordpress_post_id, status="publish",
                         raw="post-write raw"),
        ],
        post_response=WordPressUpdatedPost(
            id=art.wordpress_post_id, status="draft", link=None),
    )
    svc = WordPressContentUpdateExecutionService(session, wordpress_client=fake)
    result = svc.execute(
        article_id=art.id, artifact_id=artifact.id,
        artifact_hash=artifact.artifact_hash,
        expected_wordpress_status="draft",
        expected_pre_update_raw_content_hash=compute_text_hash(raw),
    )
    assert result.run_status == WP_CONTENT_UPDATE_OUTCOME_UNKNOWN
