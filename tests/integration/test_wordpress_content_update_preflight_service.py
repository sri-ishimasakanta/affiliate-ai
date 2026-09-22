"""WordPressContentUpdatePreflightService の統合テスト (D-D5C)。

READ-ONLY 分類のみ -- 0 DB write、``classify()`` は最大 1 回だけ WordPress GET を
行う (fake client を注入。実ネットワークへは一切到達しない)。``plan()`` は
0 回の GET しか行わない (local のみ)。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.article.draft_promotion_canonical import compute_text_hash
from app.config.settings import get_settings
from app.models import (
    Article,
    ArticlePublicationArtifact,
    WordPressDraftRun,
    WordPressPublicationRun,
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
from app.services.wordpress_content_update_preflight_service import (
    CLASSIFICATION_CONTENT_NOOP,
    CLASSIFICATION_UPDATE_REQUIRED,
    CLASSIFICATION_WORDPRESS_CURRENT_CONTENT_DRIFT,
    PROVISIONAL_NOOP_CANDIDATE,
    PROVISIONAL_UPDATE_CANDIDATE,
    REASON_APPROVAL_HASH_MISMATCH,
    REASON_ARTICLE_NOT_FOUND,
    REASON_ARTIFACT_ARTICLE_MISMATCH,
    REASON_ARTIFACT_HASH_MISMATCH,
    REASON_ARTIFACT_INVALID,
    REASON_ARTIFACT_NOT_APPROVED,
    REASON_ARTIFACT_NOT_FOUND,
    REASON_CURRENT_CANONICAL_DRIFT,
    REASON_DRAFT_BASELINE_NOT_APPROVED,
    REASON_NO_SUBMITTED_CONTENT_BASELINE,
    REASON_NO_WORDPRESS_RAW_BASELINE,
    REASON_PRIOR_RUN_AMBIGUOUS,
    REASON_PRIOR_RUN_RUNNING,
    REASON_UNSUPPORTED_WORDPRESS_STATUS,
    REASON_WORDPRESS_DRAFT_NOT_FOUND,
    REASON_WORDPRESS_POST_ID_MISMATCH,
    REASON_WORDPRESS_PREFLIGHT_FAILED,
    REASON_WORDPRESS_PREFLIGHT_POST_ID_MISMATCH,
    REASON_WORDPRESS_PUBLICATION_NOT_FOUND,
    REASON_WORDPRESS_STATUS_MISMATCH,
    WordPressContentUpdatePreflightService,
)
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
    """テスト専用の ``get_post`` だけを持つ fake -- 実ネットワークへは一切到達しない。"""

    def __init__(self, *, response: dict | None = None, exc: BaseException | None = None):
        self._response = response
        self._exc = exc
        self.get_calls = 0

    def get_post(self, post_id: int) -> dict:
        self.get_calls += 1
        if self._exc is not None:
            raise self._exc
        assert self._response is not None
        return self._response


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
    session: Session,
    *,
    slug: str = "p1",
    body: str | None = None,
    approve: bool = True,
    approved_hash_override: str | None = None,
    wordpress_post_id: int | None = 25,
) -> tuple[Article, ArticlePublicationArtifact]:
    """実 D-D3/D-D4 pipeline (prepare -> persist -> approve) で genuinely valid な
    artifact を作る -- frozen-validity 再検証 (artifact_hash_valid など) が必ず
    True になることを保証する (hand-rolled fake hash では通らない)。"""

    art = Article(
        title="t",
        slug=slug,
        keyword_id=None,
        body=body or f"[tool]({_HREF})\n",
        wordpress_post_id=wordpress_post_id,
    )
    session.add(art)
    session.commit()

    prepared = ArticlePublicationPreparationService(session, settings=get_settings()).prepare(
        art.id
    )
    artifact = ArticlePublicationArtifactPersistenceService(session).persist(prepared)

    if approve:
        ArticlePublicationArtifactService(session).approve_artifact(
            artifact.id, expected_artifact_hash=artifact.artifact_hash
        )
        session.refresh(artifact)
        if approved_hash_override is not None:
            # D-D1 の set-once 承認は常に artifact_hash と一致する値でしか承認できない
            # -- APPROVAL_HASH_MISMATCH を pin するには承認後に直接破壊するしかない
            # (defense-in-depth 経路の pin。通常操作では発生し得ない)。
            artifact.approved_artifact_hash = approved_hash_override
            session.commit()

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
        source_promotion_id=1,  # SQLite の既定 (FK 強制なし) を利用する不変の参照値
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


def _golden_scenario(session: Session, *, slug: str = "p1"):
    """expected raw == observed raw かつ candidate == last submitted (PROVISIONAL_
    NOOP_CANDIDATE / CONTENT_NOOP の golden path)。Article #1 の実際の証跡パターンを
    忠実に再現する。"""

    art, artifact = _seed_article_and_artifact(session, slug=slug)
    # candidate.request_content_hash == artifact.tracked_html_hash (compute_tracked_html_hash
    # は compute_text_hash のエイリアス) なので、これを last-submitted としてそのまま使えば
    # 必ず一致する (NOOP)。
    submitted_hash = artifact.tracked_html_hash
    raw_hash = compute_text_hash("wordpress-stored-raw-content")

    draft = _add_draft_run(session, art, rendered_content_hash=submitted_hash)
    pub = _add_publication_run(session, art, draft, wordpress_raw_content_hash=raw_hash)
    return art, artifact, draft, pub


# ==================== §26: local gates (0 network calls) ========================
def test_article_not_found(session: Session) -> None:
    svc = WordPressContentUpdatePreflightService(session)
    result = svc.plan(article_id=999999, artifact_id=1, artifact_hash="a" * 64)
    assert result.reason_code == REASON_ARTICLE_NOT_FOUND
    assert result.classification is None
    assert result.would_execute is False


def test_artifact_not_found(session: Session) -> None:
    art, _artifact = _seed_article_and_artifact(session)
    svc = WordPressContentUpdatePreflightService(session)
    result = svc.plan(article_id=art.id, artifact_id=999999, artifact_hash="a" * 64)
    assert result.reason_code == REASON_ARTIFACT_NOT_FOUND


def test_artifact_article_mismatch(session: Session) -> None:
    _art1, artifact1 = _seed_article_and_artifact(session, slug="p1")
    art2, _artifact2 = _seed_article_and_artifact(session, slug="p2")
    svc = WordPressContentUpdatePreflightService(session)
    result = svc.plan(
        article_id=art2.id, artifact_id=artifact1.id, artifact_hash=artifact1.artifact_hash
    )
    assert result.reason_code == REASON_ARTIFACT_ARTICLE_MISMATCH


def test_supplied_artifact_hash_mismatch(session: Session) -> None:
    art, artifact = _seed_article_and_artifact(session)
    svc = WordPressContentUpdatePreflightService(session)
    result = svc.plan(article_id=art.id, artifact_id=artifact.id, artifact_hash="f" * 64)
    assert result.reason_code == REASON_ARTIFACT_HASH_MISMATCH


def test_unapproved_artifact(session: Session) -> None:
    art, artifact = _seed_article_and_artifact(session, approve=False)
    svc = WordPressContentUpdatePreflightService(session)
    result = svc.plan(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )
    assert result.reason_code == REASON_ARTIFACT_NOT_APPROVED


def test_approval_hash_mismatch(session: Session) -> None:
    art, artifact = _seed_article_and_artifact(session, approved_hash_override="e" * 64)
    svc = WordPressContentUpdatePreflightService(session)
    result = svc.plan(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )
    assert result.reason_code == REASON_APPROVAL_HASH_MISMATCH


def test_frozen_artifact_invalid(session: Session) -> None:
    art, artifact = _seed_article_and_artifact(session)
    # tracked_html_hash を凍結後に直接壊す -- D-D4 の frozen-validity 再検証が拾う。
    artifact.tracked_html_hash = "0" * 64
    session.commit()

    svc = WordPressContentUpdatePreflightService(session)
    result = svc.plan(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )
    assert result.reason_code == REASON_ARTIFACT_INVALID


def test_current_canonical_drift(session: Session) -> None:
    art, artifact = _seed_article_and_artifact(session)
    art.body = "completely different body now"
    session.commit()

    svc = WordPressContentUpdatePreflightService(session)
    result = svc.plan(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )
    assert result.reason_code == REASON_CURRENT_CANONICAL_DRIFT


def test_publication_evidence_missing(session: Session) -> None:
    art, artifact = _seed_article_and_artifact(session)  # no draft/pub run at all
    svc = WordPressContentUpdatePreflightService(session)
    result = svc.plan(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )
    assert result.reason_code == REASON_WORDPRESS_PUBLICATION_NOT_FOUND


def test_post_id_mismatch(session: Session) -> None:
    art, artifact = _seed_article_and_artifact(session, wordpress_post_id=999)
    draft = _add_draft_run(session, art, rendered_content_hash=compute_text_hash("x"))
    _add_publication_run(session, art, draft, wordpress_raw_content_hash=compute_text_hash("y"))

    svc = WordPressContentUpdatePreflightService(session)
    result = svc.plan(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )
    assert result.reason_code == REASON_WORDPRESS_POST_ID_MISMATCH


def test_article_wordpress_post_id_none_is_post_id_mismatch(session: Session) -> None:
    art, artifact = _seed_article_and_artifact(session, wordpress_post_id=None)
    draft = _add_draft_run(session, art, rendered_content_hash=compute_text_hash("x"))
    _add_publication_run(session, art, draft, wordpress_raw_content_hash=compute_text_hash("y"))

    svc = WordPressContentUpdatePreflightService(session)
    result = svc.plan(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )
    assert result.reason_code == REASON_WORDPRESS_POST_ID_MISMATCH


def test_raw_baseline_missing(session: Session) -> None:
    art, artifact = _seed_article_and_artifact(session)
    draft = _add_draft_run(session, art, rendered_content_hash=compute_text_hash("x"))
    pub = _add_publication_run(
        session, art, draft, wordpress_raw_content_hash=compute_text_hash("y")
    )
    # 直接壊して baseline を空にする (defense-in-depth 経路の pin)。
    pub.wordpress_raw_content_hash = ""
    session.commit()

    svc = WordPressContentUpdatePreflightService(session)
    result = svc.plan(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )
    assert result.reason_code == REASON_NO_WORDPRESS_RAW_BASELINE


def test_submitted_content_baseline_missing(session: Session) -> None:
    art, artifact = _seed_article_and_artifact(session)
    draft = _add_draft_run(session, art, rendered_content_hash=compute_text_hash("x"))
    _add_publication_run(session, art, draft, wordpress_raw_content_hash=compute_text_hash("y"))
    draft.rendered_content_hash = ""
    session.commit()

    svc = WordPressContentUpdatePreflightService(session)
    result = svc.plan(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )
    assert result.reason_code == REASON_NO_SUBMITTED_CONTENT_BASELINE


def test_blocking_running_update(session: Session, wp_env) -> None:
    art, artifact, draft, pub = _golden_scenario(session)
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

    svc = WordPressContentUpdatePreflightService(session)
    result = svc.plan(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )
    assert result.reason_code == REASON_PRIOR_RUN_RUNNING


def test_blocking_outcome_unknown_update(session: Session, wp_env) -> None:
    art, artifact, draft, pub = _golden_scenario(session)
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

    svc = WordPressContentUpdatePreflightService(session)
    result = svc.plan(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )
    assert result.reason_code == REASON_PRIOR_RUN_AMBIGUOUS


@pytest.mark.parametrize(
    "seed",
    [
        lambda session: (None, None),  # article not found path (article_id=999999)
    ],
)
def test_local_gate_failures_make_zero_network_calls(session: Session, seed) -> None:
    fake = _FakeWordPressClient(response=_wp_response())
    svc = WordPressContentUpdatePreflightService(session, wordpress_client=fake)
    result = svc.classify(article_id=999999, artifact_id=1, artifact_hash="a" * 64)
    assert result.reason_code == REASON_ARTICLE_NOT_FOUND
    assert fake.get_calls == 0


# ==================== §27: live classification ===================================
def test_expected_equals_observed_and_candidate_equals_prior_is_content_noop(
    session: Session, wp_env
) -> None:
    art, artifact, draft, pub = _golden_scenario(session)
    fake = _FakeWordPressClient(
        response=_wp_response(post_id=25, status="publish", raw="wordpress-stored-raw-content")
    )
    svc = WordPressContentUpdatePreflightService(session, wordpress_client=fake)
    result = svc.classify(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    assert result.classification == CLASSIFICATION_CONTENT_NOOP
    assert result.reason_code == CLASSIFICATION_CONTENT_NOOP
    assert result.would_execute is False
    assert fake.get_calls == 1


def test_expected_equals_observed_and_candidate_differs_is_update_required(
    session: Session, wp_env
) -> None:
    art, artifact = _seed_article_and_artifact(session)
    # 過去に送信済みの内容は artifact の tracked_html と異なる (UPDATE_REQUIRED になる)。
    old_submitted_hash = compute_text_hash("<p>OLD tracked, never equal to the real artifact</p>")
    assert old_submitted_hash != artifact.tracked_html_hash
    draft = _add_draft_run(session, art, rendered_content_hash=old_submitted_hash)
    raw_hash = compute_text_hash("wordpress-stored-raw-content")
    _add_publication_run(session, art, draft, wordpress_raw_content_hash=raw_hash)

    fake = _FakeWordPressClient(
        response=_wp_response(post_id=25, status="publish", raw="wordpress-stored-raw-content")
    )
    svc = WordPressContentUpdatePreflightService(session, wordpress_client=fake)
    result = svc.classify(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    assert result.classification == CLASSIFICATION_UPDATE_REQUIRED
    assert result.reason_code == CLASSIFICATION_UPDATE_REQUIRED
    assert result.would_execute is True
    assert fake.get_calls == 1


def test_expected_not_equal_observed_is_wordpress_current_content_drift(
    session: Session, wp_env
) -> None:
    art, artifact, draft, pub = _golden_scenario(session)
    # live raw が expected baseline と一致しない -- remote drift。
    fake = _FakeWordPressClient(
        response=_wp_response(post_id=25, status="publish", raw="SOMETHING ELSE ENTIRELY")
    )
    svc = WordPressContentUpdatePreflightService(session, wordpress_client=fake)
    result = svc.classify(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    assert result.classification == CLASSIFICATION_WORDPRESS_CURRENT_CONTENT_DRIFT
    assert result.reason_code == CLASSIFICATION_WORDPRESS_CURRENT_CONTENT_DRIFT
    assert result.would_execute is False
    assert fake.get_calls == 1


def test_remote_drift_short_circuits_content_classification(session: Session, wp_env) -> None:
    """expected != observed のとき、candidate/last-submitted の比較結果に一切関わらず
    常に WORDPRESS_CURRENT_CONTENT_DRIFT になる (short circuit の証明)。"""

    art, artifact = _seed_article_and_artifact(session)
    submitted_hash = artifact.tracked_html_hash  # candidate と一致 (本来なら NOOP)
    draft = _add_draft_run(session, art, rendered_content_hash=submitted_hash)
    _add_publication_run(
        session, art, draft, wordpress_raw_content_hash=compute_text_hash("expected-raw")
    )

    fake = _FakeWordPressClient(
        response=_wp_response(post_id=25, status="publish", raw="different-from-expected")
    )
    svc = WordPressContentUpdatePreflightService(session, wordpress_client=fake)
    result = svc.classify(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    assert result.classification == CLASSIFICATION_WORDPRESS_CURRENT_CONTENT_DRIFT
    assert result.candidate_request_content_hash == result.last_successful_request_content_hash


# ==================== §20: WordPress status handling ==============================
def test_live_status_not_publish_is_status_mismatch(session: Session, wp_env) -> None:
    art, artifact, draft, pub = _golden_scenario(session)
    fake = _FakeWordPressClient(
        response=_wp_response(post_id=25, status="draft", raw="wordpress-stored-raw-content")
    )
    svc = WordPressContentUpdatePreflightService(session, wordpress_client=fake)
    result = svc.classify(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )
    assert result.reason_code == REASON_WORDPRESS_STATUS_MISMATCH
    assert result.classification is None
    assert result.would_execute is False


# ==================== §19: network failure handling ================================
def test_get_post_exception_is_preflight_failed(session: Session, wp_env) -> None:
    art, artifact, draft, pub = _golden_scenario(session)
    fake = _FakeWordPressClient(exc=RuntimeError("boom"))
    svc = WordPressContentUpdatePreflightService(session, wordpress_client=fake)
    result = svc.classify(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )
    assert result.reason_code == REASON_WORDPRESS_PREFLIGHT_FAILED
    assert result.classification is None
    assert fake.get_calls == 1


def test_get_post_missing_content_raw_is_preflight_failed(session: Session, wp_env) -> None:
    art, artifact, draft, pub = _golden_scenario(session)
    response = _wp_response(post_id=25, status="publish")
    response["content"] = {"rendered": "only rendered, no raw"}
    fake = _FakeWordPressClient(response=response)
    svc = WordPressContentUpdatePreflightService(session, wordpress_client=fake)
    result = svc.classify(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )
    assert result.reason_code == REASON_WORDPRESS_PREFLIGHT_FAILED


def test_get_post_returns_wrong_post_id(session: Session, wp_env) -> None:
    art, artifact, draft, pub = _golden_scenario(session)
    fake = _FakeWordPressClient(response=_wp_response(post_id=999, status="publish"))
    svc = WordPressContentUpdatePreflightService(session, wordpress_client=fake)
    result = svc.classify(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )
    assert result.reason_code == REASON_WORDPRESS_PREFLIGHT_POST_ID_MISMATCH


# ==================== §29: at most one GET ==========================================
def test_successful_classification_calls_get_exactly_once(session: Session, wp_env) -> None:
    art, artifact, draft, pub = _golden_scenario(session)
    fake = _FakeWordPressClient(
        response=_wp_response(post_id=25, status="publish", raw="wordpress-stored-raw-content")
    )
    svc = WordPressContentUpdatePreflightService(session, wordpress_client=fake)
    svc.classify(article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash)
    assert fake.get_calls == 1


def test_drift_classification_calls_get_exactly_once(session: Session, wp_env) -> None:
    art, artifact, draft, pub = _golden_scenario(session)
    fake = _FakeWordPressClient(response=_wp_response(post_id=25, status="publish", raw="drift"))
    svc = WordPressContentUpdatePreflightService(session, wordpress_client=fake)
    svc.classify(article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash)
    assert fake.get_calls == 1


def test_get_failure_calls_get_exactly_once_no_retry(session: Session, wp_env) -> None:
    art, artifact, draft, pub = _golden_scenario(session)
    fake = _FakeWordPressClient(exc=RuntimeError("boom"))
    svc = WordPressContentUpdatePreflightService(session, wordpress_client=fake)
    svc.classify(article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash)
    assert fake.get_calls == 1


def test_plan_never_calls_get(session: Session, wp_env) -> None:
    art, artifact, draft, pub = _golden_scenario(session)
    fake = _FakeWordPressClient(response=_wp_response())
    svc = WordPressContentUpdatePreflightService(session, wordpress_client=fake)
    svc.plan(article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash)
    assert fake.get_calls == 0


# ==================== §24/§25: local-only provisional (Article #1 pattern) ========
def test_plan_provisional_noop_candidate_matches_article_1_pattern(
    session: Session, wp_env
) -> None:
    art, artifact, draft, pub = _golden_scenario(session)
    svc = WordPressContentUpdatePreflightService(session)
    result = svc.plan(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    assert result.classification == PROVISIONAL_NOOP_CANDIDATE
    assert result.reason_code == PROVISIONAL_NOOP_CANDIDATE
    assert result.would_execute is False
    assert result.observed_pre_update_wordpress_raw_content_hash is None
    assert result.candidate_request_content_hash == result.last_successful_request_content_hash
    assert result.expected_pre_update_wordpress_raw_content_hash == pub.wordpress_raw_content_hash


def test_plan_provisional_update_candidate(session: Session, wp_env) -> None:
    art, artifact = _seed_article_and_artifact(session)
    old_submitted_hash = compute_text_hash("<p>OLD, never equal to the real artifact</p>")
    assert old_submitted_hash != artifact.tracked_html_hash
    draft = _add_draft_run(session, art, rendered_content_hash=old_submitted_hash)
    _add_publication_run(session, art, draft, wordpress_raw_content_hash=compute_text_hash("raw"))

    svc = WordPressContentUpdatePreflightService(session)
    result = svc.plan(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )
    assert result.classification == PROVISIONAL_UPDATE_CANDIDATE
    assert result.would_execute is False  # D-D5C never executes, even for UPDATE candidates


def test_plan_result_never_claims_final_classification_codes(session: Session, wp_env) -> None:
    art, artifact, draft, pub = _golden_scenario(session)
    svc = WordPressContentUpdatePreflightService(session)
    result = svc.plan(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )
    assert result.classification not in (
        CLASSIFICATION_CONTENT_NOOP,
        CLASSIFICATION_UPDATE_REQUIRED,
        CLASSIFICATION_WORDPRESS_CURRENT_CONTENT_DRIFT,
    )


def test_target_base_url_is_canonical_and_credential_free(session: Session, wp_env) -> None:
    art, artifact, draft, pub = _golden_scenario(session)
    svc = WordPressContentUpdatePreflightService(session)
    result = svc.plan(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )
    assert result.target_base_url == canonicalize_wordpress_base_url(_TARGET_BASE_URL)
    assert "@" not in result.target_base_url
    assert "wp-user-secret" not in result.target_base_url


# ==================== §28: namespace regression ====================================
def test_content_noop_still_works_with_deliberately_different_namespaces(
    session: Session, wp_env
) -> None:
    """request_content_hash 系と raw hash 系が全く異なる値でも、各 namespace 内の
    正しい比較だけで CONTENT_NOOP に到達できることを証明する。"""

    art, artifact = _seed_article_and_artifact(session)
    submitted_hash = artifact.tracked_html_hash
    draft = _add_draft_run(session, art, rendered_content_hash=submitted_hash)
    raw_value = "totally unrelated wordpress raw html blob"
    raw_hash = compute_text_hash(raw_value)
    _add_publication_run(session, art, draft, wordpress_raw_content_hash=raw_hash)

    assert raw_hash != submitted_hash  # namespaces really are unrelated values

    fake = _FakeWordPressClient(response=_wp_response(post_id=25, status="publish", raw=raw_value))
    svc = WordPressContentUpdatePreflightService(session, wordpress_client=fake)
    result = svc.classify(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )
    assert result.classification == CLASSIFICATION_CONTENT_NOOP


def test_update_required_still_works_with_deliberately_different_namespaces(
    session: Session, wp_env
) -> None:
    art, artifact = _seed_article_and_artifact(session)
    old_submitted_hash = compute_text_hash("<p>old, never equal to the real artifact</p>")
    assert old_submitted_hash != artifact.tracked_html_hash
    draft = _add_draft_run(session, art, rendered_content_hash=old_submitted_hash)
    raw_value = "totally unrelated wordpress raw html blob"
    raw_hash = compute_text_hash(raw_value)
    _add_publication_run(session, art, draft, wordpress_raw_content_hash=raw_hash)

    fake = _FakeWordPressClient(response=_wp_response(post_id=25, status="publish", raw=raw_value))
    svc = WordPressContentUpdatePreflightService(session, wordpress_client=fake)
    result = svc.classify(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )
    assert result.classification == CLASSIFICATION_UPDATE_REQUIRED


def test_no_comparison_ever_equates_request_hash_and_raw_hash(session: Session, wp_env) -> None:
    art, artifact, draft, pub = _golden_scenario(session)
    fake = _FakeWordPressClient(
        response=_wp_response(post_id=25, status="publish", raw="wordpress-stored-raw-content")
    )
    svc = WordPressContentUpdatePreflightService(session, wordpress_client=fake)
    result = svc.classify(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )
    # request/tracked/payload namespace と raw namespace は独立した値であり続ける。
    assert (
        result.candidate_request_content_hash
        != result.expected_pre_update_wordpress_raw_content_hash
    )
    assert (
        result.candidate_request_content_hash
        != result.observed_pre_update_wordpress_raw_content_hash
    )


# ==================== §30: read-only DB behavior ===================================
def test_classify_makes_zero_db_mutations(session: Session, wp_env) -> None:
    from sqlalchemy import func, select

    from app.models import WordPressContentUpdateRun

    art, artifact, draft, pub = _golden_scenario(session)

    def _counts():
        return (
            session.scalar(select(func.count()).select_from(Article)),
            session.scalar(select(func.count()).select_from(ArticlePublicationArtifact)),
            session.scalar(select(func.count()).select_from(WordPressPublicationRun)),
            session.scalar(select(func.count()).select_from(WordPressDraftRun)),
            session.scalar(select(func.count()).select_from(WordPressContentUpdateRun)),
        )

    before = _counts()
    fake = _FakeWordPressClient(
        response=_wp_response(post_id=25, status="publish", raw="wordpress-stored-raw-content")
    )
    svc = WordPressContentUpdatePreflightService(session, wordpress_client=fake)
    svc.classify(article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash)
    after = _counts()

    assert before == after
    assert artifact.approved_at is not None  # unchanged, not re-mutated
    assert art.body == art.body  # sanity: object still attached/unchanged


def test_plan_makes_zero_db_mutations(session: Session, wp_env) -> None:
    from sqlalchemy import func, select

    from app.models import WordPressContentUpdateRun

    art, artifact, draft, pub = _golden_scenario(session)

    def _counts():
        return (
            session.scalar(select(func.count()).select_from(Article)),
            session.scalar(select(func.count()).select_from(ArticlePublicationArtifact)),
            session.scalar(select(func.count()).select_from(WordPressPublicationRun)),
            session.scalar(select(func.count()).select_from(WordPressDraftRun)),
            session.scalar(select(func.count()).select_from(WordPressContentUpdateRun)),
        )

    before = _counts()
    svc = WordPressContentUpdatePreflightService(session)
    svc.plan(article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash)
    after = _counts()
    assert before == after


# ==================== §31: security/leak =============================================
def test_result_never_contains_tracked_html_or_manifest(session: Session, wp_env) -> None:
    art, artifact, draft, pub = _golden_scenario(session)
    fake = _FakeWordPressClient(
        response=_wp_response(post_id=25, status="publish", raw="wordpress-stored-raw-content")
    )
    svc = WordPressContentUpdatePreflightService(session, wordpress_client=fake)
    result = svc.classify(
        article_id=art.id, artifact_id=artifact.id, artifact_hash=artifact.artifact_hash
    )

    import dataclasses

    for value in dataclasses.astuple(result):
        if isinstance(value, str):
            assert artifact.tracked_html not in value
            assert "Authorization" not in value
            assert "wp-user-secret" not in value
            assert "aaaa bbbb cccc dddd" not in value


# ==================== C4.8: draft content update ==============================
def _draft_only_scenario(session: Session, *, slug: str):
    """公開実績が無く、succeeded な draft run だけを持つ記事。"""
    art, artifact = _seed_article_and_artifact(session, slug=slug)
    draft = _add_draft_run(session, art, rendered_content_hash="0" * 64)
    return art, artifact, draft


def test_draft_mode_updates_an_unpublished_draft(session: Session, wp_env) -> None:
    """公開されていない draft の content を managed path で更新できる。"""
    art, artifact, _draft = _draft_only_scenario(session, slug="draft-mode-ok")
    raw = "current-draft-raw"
    client = _FakeWordPressClient(response=_wp_response(
        post_id=art.wordpress_post_id, status="draft", raw=raw))
    svc = WordPressContentUpdatePreflightService(session, wordpress_client=client)

    result = svc.classify(
        article_id=art.id, artifact_id=artifact.id,
        artifact_hash=artifact.artifact_hash,
        expected_wordpress_status="draft",
        expected_pre_update_raw_content_hash=compute_text_hash(raw),
    )
    assert result.classification == CLASSIFICATION_UPDATE_REQUIRED
    assert result.would_execute is True
    assert result.wordpress_status == "draft"


def test_draft_mode_requires_an_approved_baseline(session: Session, wp_env) -> None:
    """初回の draft 更新は、呼び出し側が現在の raw hash を承認して渡す。"""
    art, artifact, _draft = _draft_only_scenario(session, slug="draft-mode-baseline")
    svc = WordPressContentUpdatePreflightService(session)

    result = svc.plan(
        article_id=art.id, artifact_id=artifact.id,
        artifact_hash=artifact.artifact_hash,
        expected_wordpress_status="draft",
    )
    assert result.reason_code == REASON_DRAFT_BASELINE_NOT_APPROVED
    assert result.would_execute is False


def test_draft_mode_refuses_an_already_published_article(
    session: Session, wp_env
) -> None:
    """公開済みの記事を draft モードで更新させない。"""
    art, artifact, _d, _p = _golden_scenario(session, slug="draft-mode-published")
    svc = WordPressContentUpdatePreflightService(session)

    result = svc.plan(
        article_id=art.id, artifact_id=artifact.id,
        artifact_hash=artifact.artifact_hash,
        expected_wordpress_status="draft",
        expected_pre_update_raw_content_hash="a" * 64,
    )
    assert result.reason_code == REASON_WORDPRESS_STATUS_MISMATCH
    assert result.would_execute is False


def test_draft_mode_refuses_when_no_draft_run_exists(session: Session, wp_env) -> None:
    art, artifact = _seed_article_and_artifact(session, slug="draft-mode-no-run")
    svc = WordPressContentUpdatePreflightService(session)

    result = svc.plan(
        article_id=art.id, artifact_id=artifact.id,
        artifact_hash=artifact.artifact_hash,
        expected_wordpress_status="draft",
        expected_pre_update_raw_content_hash="a" * 64,
    )
    assert result.reason_code == REASON_WORDPRESS_DRAFT_NOT_FOUND


def test_draft_mode_detects_external_drift(session: Session, wp_env) -> None:
    """WordPress 側が別物になっていたら更新しない。"""
    art, artifact, _draft = _draft_only_scenario(session, slug="draft-mode-drift")
    client = _FakeWordPressClient(response=_wp_response(
        post_id=art.wordpress_post_id, status="draft", raw="someone-else-edited"))
    svc = WordPressContentUpdatePreflightService(session, wordpress_client=client)

    result = svc.classify(
        article_id=art.id, artifact_id=artifact.id,
        artifact_hash=artifact.artifact_hash,
        expected_wordpress_status="draft",
        expected_pre_update_raw_content_hash=compute_text_hash("what-we-expected"),
    )
    assert result.classification == CLASSIFICATION_WORDPRESS_CURRENT_CONTENT_DRIFT
    assert result.would_execute is False


def test_draft_mode_refuses_a_post_that_is_actually_published(
    session: Session, wp_env
) -> None:
    """live status が draft でなければ更新しない (publish へ切り替えない)。"""
    art, artifact, _draft = _draft_only_scenario(session, slug="draft-mode-live-pub")
    raw = "current-raw"
    client = _FakeWordPressClient(response=_wp_response(
        post_id=art.wordpress_post_id, status="publish", raw=raw))
    svc = WordPressContentUpdatePreflightService(session, wordpress_client=client)

    result = svc.classify(
        article_id=art.id, artifact_id=artifact.id,
        artifact_hash=artifact.artifact_hash,
        expected_wordpress_status="draft",
        expected_pre_update_raw_content_hash=compute_text_hash(raw),
    )
    assert result.reason_code == REASON_WORDPRESS_STATUS_MISMATCH
    assert result.would_execute is False


def test_unsupported_status_mode_is_refused(session: Session, wp_env) -> None:
    art, artifact, _draft = _draft_only_scenario(session, slug="draft-mode-bad")
    svc = WordPressContentUpdatePreflightService(session)
    result = svc.plan(
        article_id=art.id, artifact_id=artifact.id,
        artifact_hash=artifact.artifact_hash,
        expected_wordpress_status="future",
    )
    assert result.reason_code == REASON_UNSUPPORTED_WORDPRESS_STATUS


def test_publish_mode_is_unchanged_by_the_draft_capability(
    session: Session, wp_env
) -> None:
    """既定モードの挙動は一切変わらない。"""
    art, artifact, _d, _p = _golden_scenario(session, slug="publish-mode-intact")
    client = _FakeWordPressClient(response=_wp_response(
        post_id=art.wordpress_post_id, status="publish",
        raw="wordpress-stored-raw-content"))
    svc = WordPressContentUpdatePreflightService(session, wordpress_client=client)
    result = svc.classify(
        article_id=art.id, artifact_id=artifact.id,
        artifact_hash=artifact.artifact_hash)
    assert result.classification == CLASSIFICATION_CONTENT_NOOP
