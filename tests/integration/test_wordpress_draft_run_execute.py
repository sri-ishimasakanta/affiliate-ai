"""WordPressDraftRunService.execute: guards / duplicate preflight / lifecycle / atomicity。

WordPress へは一切通信しない (フェイクの WordPressClient を注入する)。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config.settings import Settings, get_settings
from app.exceptions import (
    EntityNotFoundError,
    ExternalProviderError,
    RenderedCandidateChangedError,
    WordPressAmbiguousOutcomeError,
    WordPressDraftRunStateError,
    WordPressExternalCreateLocalPersistFailedError,
)
from app.models import ArticleDraftPromotion, WordPressDraftRun
from app.models.enums import ArticleStatus
from app.models.wordpress_draft_run import WP_RUN_FAILED, WP_RUN_RUNNING, WP_RUN_SUCCEEDED
from app.services.wordpress_draft_run_service import WordPressDraftRunService
from app.services.wordpress_preview_service import WordPressPreviewService
from app.wordpress.client import WordPressCreatedPost
from tests.support.draft_promotion_fixture import article_of, promoted_scenario

_BASE_URL = "https://wp.example.test/blog"


class _FakeWordPressClient:
    """execute() が使う最小 interface のみを持つテスト用 double。"""

    def __init__(
        self,
        *,
        existing_slugs: dict[str, list[int]] | None = None,
        create_result: WordPressCreatedPost | BaseException | None = None,
    ) -> None:
        self._existing = existing_slugs or {}
        self._create_result = create_result
        self.find_calls = 0
        self.create_calls = 0

    def find_draft_posts_by_slug(self, slug: str) -> list[int]:
        self.find_calls += 1
        return list(self._existing.get(slug, []))

    def create_draft_post_exact(self, payload_json: str) -> WordPressCreatedPost:
        self.create_calls += 1
        if isinstance(self._create_result, BaseException):
            raise self._create_result
        assert self._create_result is not None
        return self._create_result


@pytest.fixture
def wp_env(monkeypatch):
    monkeypatch.setenv("WORDPRESS_BASE_URL", _BASE_URL)
    monkeypatch.setenv("WORDPRESS_USERNAME", "wp-user-secret")
    monkeypatch.setenv("WORDPRESS_APP_PASSWORD", "aaaa bbbb cccc dddd")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _promotion_id(session: Session, article_id: int) -> int:
    row = session.scalars(
        select(ArticleDraftPromotion).where(ArticleDraftPromotion.article_id == article_id)
    ).first()
    return row.id


def _prepared_run(session: Session) -> WordPressDraftRun:
    """promoted_scenario() から prepared な WordPressDraftRun を 1 件作る (WordPress 通信なし)。"""

    ps = promoted_scenario(session)
    dp = WordPressPreviewService(session).draft_request_preview(
        ps.article_id, expected_renderer_version="wordpress_html_v1"
    )
    pid = _promotion_id(session, ps.article_id)
    out = WordPressDraftRunService(session).prepare(
        ps.article_id,
        source_promotion_id=pid,
        expected_renderer_version=dp.renderer_version,
        expected_rendered_content_hash=dp.rendered_content_hash,
        expected_payload_hash=dp.payload_hash,
        expected_request_identity_hash=dp.request_identity_hash,
    )
    return session.get(WordPressDraftRun, out.run_id)


def _created(post_id: int = 42, slug: str = "unused") -> WordPressCreatedPost:
    return WordPressCreatedPost(
        id=post_id, status="draft", slug=slug, link=f"https://wp.example.test/?p={post_id}"
    )


# ==================== happy path (§24) ====================================
def test_execute_happy_path_succeeds(session: Session, wp_env) -> None:
    run = _prepared_run(session)
    fake = _FakeWordPressClient(create_result=_created(post_id=123))
    svc = WordPressDraftRunService(session, wordpress_client=fake)

    out = svc.execute(
        run.article_id, run.id,
        expected_target_request_identity_hash=run.target_request_identity_hash,
    )

    assert out.status == WP_RUN_SUCCEEDED
    assert out.wordpress_post_id == "123"
    assert out.wordpress_post_status == "draft"
    assert fake.create_calls == 1
    assert fake.find_calls == 1

    persisted = session.get(WordPressDraftRun, run.id)
    assert persisted.status == WP_RUN_SUCCEEDED
    assert persisted.wordpress_post_id == "123"
    assert persisted.wordpress_post_status == "draft"
    assert persisted.started_at is not None
    assert persisted.finished_at is not None
    assert persisted.error_message is None
    assert persisted.response_snapshot == {
        "id": 123, "status": "draft", "slug": "unused",
        "link": "https://wp.example.test/?p=123",
    }

    art = article_of(session, run.article_id)
    assert art.wordpress_post_id == 123
    assert art.status == ArticleStatus.REVIEW.value  # Article は review のまま
    assert art.published_url is None
    assert art.published_at is None


# ==================== duplicate preflight (§23) ============================
def test_execute_blocked_when_duplicate_draft_exists(session: Session, wp_env) -> None:
    run = _prepared_run(session)
    slug = json.loads(run.payload_json)["slug"]
    fake = _FakeWordPressClient(existing_slugs={slug: [555]})
    svc = WordPressDraftRunService(session, wordpress_client=fake)

    with pytest.raises(WordPressDraftRunStateError) as exc_info:
        svc.execute(
            run.article_id, run.id,
            expected_target_request_identity_hash=run.target_request_identity_hash,
        )
    assert "555" in str(exc_info.value)
    assert fake.create_calls == 0  # 絶対に POST しない

    persisted = session.get(WordPressDraftRun, run.id)
    assert persisted.status == "prepared"  # run は prepared のまま


def test_execute_blocked_when_multiple_duplicate_drafts_exist(session: Session, wp_env) -> None:
    run = _prepared_run(session)
    slug = json.loads(run.payload_json)["slug"]
    fake = _FakeWordPressClient(existing_slugs={slug: [1, 2, 3]})
    svc = WordPressDraftRunService(session, wordpress_client=fake)

    with pytest.raises(WordPressDraftRunStateError) as exc_info:
        svc.execute(
            run.article_id, run.id,
            expected_target_request_identity_hash=run.target_request_identity_hash,
        )
    for pid in ("1", "2", "3"):
        assert pid in str(exc_info.value)
    assert fake.create_calls == 0
    assert session.get(WordPressDraftRun, run.id).status == "prepared"


def test_execute_proceeds_when_no_duplicate_draft(session: Session, wp_env) -> None:
    run = _prepared_run(session)
    fake = _FakeWordPressClient(existing_slugs={}, create_result=_created())
    svc = WordPressDraftRunService(session, wordpress_client=fake)

    out = svc.execute(
        run.article_id, run.id,
        expected_target_request_identity_hash=run.target_request_identity_hash,
    )
    assert out.status == WP_RUN_SUCCEEDED
    assert fake.find_calls == 1
    assert fake.create_calls == 1


# ==================== state guards (§25) ===================================
def test_execute_rejects_wrong_article(session: Session, wp_env) -> None:
    run = _prepared_run(session)
    fake = _FakeWordPressClient(create_result=_created())
    svc = WordPressDraftRunService(session, wordpress_client=fake)
    with pytest.raises(EntityNotFoundError):
        svc.execute(
            run.article_id + 999, run.id,
            expected_target_request_identity_hash=run.target_request_identity_hash,
        )
    assert fake.create_calls == 0


def test_execute_rejects_already_running(session: Session, wp_env) -> None:
    run = _prepared_run(session)
    run.status = WP_RUN_RUNNING
    session.flush()
    fake = _FakeWordPressClient(create_result=_created())
    svc = WordPressDraftRunService(session, wordpress_client=fake)
    with pytest.raises(WordPressDraftRunStateError):
        svc.execute(
            run.article_id, run.id,
            expected_target_request_identity_hash=run.target_request_identity_hash,
        )
    assert fake.create_calls == 0


def test_execute_rejects_already_succeeded(session: Session, wp_env) -> None:
    run = _prepared_run(session)
    run.status = WP_RUN_SUCCEEDED
    session.flush()
    fake = _FakeWordPressClient(create_result=_created())
    svc = WordPressDraftRunService(session, wordpress_client=fake)
    with pytest.raises(WordPressDraftRunStateError):
        svc.execute(
            run.article_id, run.id,
            expected_target_request_identity_hash=run.target_request_identity_hash,
        )
    assert fake.create_calls == 0


def test_execute_rejects_article_not_review(session: Session, wp_env) -> None:
    run = _prepared_run(session)
    art = article_of(session, run.article_id)
    art.status = ArticleStatus.APPROVED.value
    session.flush()
    fake = _FakeWordPressClient(create_result=_created())
    svc = WordPressDraftRunService(session, wordpress_client=fake)
    with pytest.raises(WordPressDraftRunStateError):
        svc.execute(
            run.article_id, run.id,
            expected_target_request_identity_hash=run.target_request_identity_hash,
        )
    assert fake.create_calls == 0


def test_execute_rejects_existing_article_wordpress_post_id(session: Session, wp_env) -> None:
    run = _prepared_run(session)
    art = article_of(session, run.article_id)
    art.wordpress_post_id = 999
    session.flush()
    fake = _FakeWordPressClient(create_result=_created())
    svc = WordPressDraftRunService(session, wordpress_client=fake)
    with pytest.raises(WordPressDraftRunStateError):
        svc.execute(
            run.article_id, run.id,
            expected_target_request_identity_hash=run.target_request_identity_hash,
        )
    assert fake.create_calls == 0


def test_execute_rejects_target_identity_mismatch(session: Session, wp_env) -> None:
    run = _prepared_run(session)
    fake = _FakeWordPressClient(create_result=_created())
    svc = WordPressDraftRunService(session, wordpress_client=fake)
    with pytest.raises(WordPressDraftRunStateError):
        svc.execute(run.article_id, run.id, expected_target_request_identity_hash="0" * 64)
    assert fake.create_calls == 0


def test_execute_rejects_payload_hash_drift(session: Session, wp_env) -> None:
    run = _prepared_run(session)
    run.payload_hash = "0" * 64  # simulate stored corruption/drift
    session.flush()
    fake = _FakeWordPressClient(create_result=_created())
    svc = WordPressDraftRunService(session, wordpress_client=fake)
    with pytest.raises(WordPressDraftRunStateError):
        svc.execute(
            run.article_id, run.id,
            expected_target_request_identity_hash=run.target_request_identity_hash,
        )
    assert fake.create_calls == 0


def test_execute_rejects_canonical_body_hash_drift(session: Session, wp_env) -> None:
    run = _prepared_run(session)
    art = article_of(session, run.article_id)
    art.body = art.body + "\n\n追記された本文ドリフト。"
    session.flush()
    fake = _FakeWordPressClient(create_result=_created())
    svc = WordPressDraftRunService(session, wordpress_client=fake)
    # body の変更は renderer の rendered_content_hash も変える (両者は独立ではない)。
    # RenderedCandidateChangedError (renderer drift guard) が先に検出しても、
    # WordPressDraftRunStateError (execute 独自の canonical hash drift guard) が
    # 検出しても、どちらも「POST しない」という結果は同じ。
    with pytest.raises((WordPressDraftRunStateError, RenderedCandidateChangedError)):
        svc.execute(
            run.article_id, run.id,
            expected_target_request_identity_hash=run.target_request_identity_hash,
        )
    assert fake.create_calls == 0


def test_execute_rejects_status_not_draft(session: Session, wp_env) -> None:
    run = _prepared_run(session)
    corrupted = dict(json.loads(run.payload_json))
    corrupted["status"] = "publish"
    run.payload_json = json.dumps(corrupted, sort_keys=True, ensure_ascii=False,
                                   separators=(",", ":"))
    session.flush()
    fake = _FakeWordPressClient(create_result=_created())
    svc = WordPressDraftRunService(session, wordpress_client=fake)
    with pytest.raises(WordPressDraftRunStateError):
        svc.execute(
            run.article_id, run.id,
            expected_target_request_identity_hash=run.target_request_identity_hash,
        )
    assert fake.create_calls == 0


def test_execute_rejects_credentials_missing(session: Session, wp_env, monkeypatch) -> None:
    # 実行時点だけ wordpress_configured=False にする (prepare は wp_env で通す)。
    # delenv だけでは .env の実credentialにフォールバックしてしまうため、
    # 3C-5D-A.1a で確立した isolated-settings pattern と同じ手法で
    # execute() が参照するモジュールの get_settings を狭く差し替える。
    run = _prepared_run(session)
    # kwargs は env var / .env のどちらよりも優先されるため、wp_env が setenv した
    # WORDPRESS_USERNAME/APP_PASSWORD があっても確実に unconfigured にできる。
    unconfigured_settings = Settings(
        _env_file=None, wordpress_username=None, wordpress_app_password=None
    )
    monkeypatch.setattr(
        "app.services.wordpress_draft_run_service.get_settings",
        lambda: unconfigured_settings,
    )
    fake = _FakeWordPressClient(create_result=_created())
    svc = WordPressDraftRunService(session, wordpress_client=fake)
    with pytest.raises(WordPressDraftRunStateError):
        svc.execute(
            run.article_id, run.id,
            expected_target_request_identity_hash=run.target_request_identity_hash,
        )
    assert fake.create_calls == 0


# ==================== ambiguous / failure behavior (§26) ===================
def test_execute_401_marks_failed_no_retry_article_unchanged(session: Session, wp_env) -> None:
    run = _prepared_run(session)
    fake = _FakeWordPressClient(
        create_result=ExternalProviderError("wordpress", "authentication failed (401)")
    )
    svc = WordPressDraftRunService(session, wordpress_client=fake)

    with pytest.raises(ExternalProviderError):
        svc.execute(
            run.article_id, run.id,
            expected_target_request_identity_hash=run.target_request_identity_hash,
        )
    assert fake.create_calls == 1  # exactly one attempt, no retry

    persisted = session.get(WordPressDraftRun, run.id)
    assert persisted.status == WP_RUN_FAILED
    assert persisted.finished_at is not None
    assert persisted.wordpress_post_id is None

    art = article_of(session, run.article_id)
    assert art.wordpress_post_id is None
    assert art.status == ArticleStatus.REVIEW.value


def test_execute_ambiguous_timeout_marks_failed_lifecycle(session: Session, wp_env) -> None:
    run = _prepared_run(session)
    fake = _FakeWordPressClient(
        create_result=WordPressAmbiguousOutcomeError("request timed out; WordPress outcome unknown")
    )
    svc = WordPressDraftRunService(session, wordpress_client=fake)

    with pytest.raises(WordPressAmbiguousOutcomeError):
        svc.execute(
            run.article_id, run.id,
            expected_target_request_identity_hash=run.target_request_identity_hash,
        )
    assert fake.create_calls == 1

    persisted = session.get(WordPressDraftRun, run.id)
    assert persisted.status == WP_RUN_FAILED  # existing lifecycle used for ambiguous outcome too
    assert "ambiguous_wordpress_outcome" in persisted.error_message
    assert persisted.wordpress_post_id is None

    art = article_of(session, run.article_id)
    assert art.wordpress_post_id is None
    assert art.status == ArticleStatus.REVIEW.value


# ==================== DB failure after external success (§27) =============
def test_execute_local_persist_failure_after_external_success_no_second_post(
    session: Session, wp_env, monkeypatch
) -> None:
    run = _prepared_run(session)
    fake = _FakeWordPressClient(create_result=_created(post_id=777))
    svc = WordPressDraftRunService(session, wordpress_client=fake)

    real_commit = session.commit
    calls = {"n": 0}

    def flaky_commit():
        calls["n"] += 1
        if calls["n"] == 1:
            return real_commit()  # the prepared -> running commit (§14) succeeds
        raise RuntimeError("simulated local persist failure after WordPress success")

    monkeypatch.setattr(session, "commit", flaky_commit)

    with pytest.raises(WordPressExternalCreateLocalPersistFailedError) as exc_info:
        svc.execute(
            run.article_id, run.id,
            expected_target_request_identity_hash=run.target_request_identity_hash,
        )

    # documents: the external WordPress post (id=777) may already exist even though
    # the local success record failed to persist. Reported safely, no retry attempted.
    assert "777" in str(exc_info.value)
    assert fake.create_calls == 1  # NEVER a second POST

    monkeypatch.undo()
    persisted = session.get(WordPressDraftRun, run.id)
    # local success write was rolled back; run reflects the last real commit (running),
    # NOT falsely "succeeded" and NOT "failed" (WordPress side is not actually a failure).
    assert persisted.status == WP_RUN_RUNNING
    assert persisted.wordpress_post_id is None
