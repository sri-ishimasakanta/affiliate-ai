"""WordPressPublicationRunService.prepare: guards / identity freeze / idempotency / conflict。

WordPress へは一切通信しない (prepare は WordPressClient を使わない)。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.exceptions import (
    EntityNotFoundError,
    WordPressPublicationRunConflictError,
    WordPressPublicationRunPreparationError,
)
from app.models import (
    ArticleDraftPromotion,
    WordPressDraftRun,
    WordPressPublicationRun,
)
from app.models.enums import ArticleStatus
from app.models.wordpress_draft_run import WP_RUN_SUCCEEDED
from app.services.wordpress_draft_run_service import WordPressDraftRunService
from app.services.wordpress_preview_service import WordPressPreviewService
from app.services.wordpress_publication_run_service import (
    WordPressPublicationRunService,
)
from app.wordpress.publish_request import compute_publish_payload_hash
from tests.support.draft_promotion_fixture import article_of, promoted_scenario

_WP_POST_ID = 25
_RAW_HASH = "ee300c1dd3728a2a5e247ad0fdc610653e53f1612c1e4029633db8483b2f8f33"
_BASE_URL = "https://wp.example.test/blog"


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


def _count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(WordPressPublicationRun))


def _ready(session: Session, *, suffix: str = ""):
    """approved Article + succeeded/draft WordPressDraftRun を用意する (通信なし)。"""

    ps = promoted_scenario(session, suffix=suffix)
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
    run = session.get(WordPressDraftRun, out.run_id)
    run.status = WP_RUN_SUCCEEDED
    run.wordpress_post_id = str(_WP_POST_ID)
    run.wordpress_post_status = "draft"
    run.started_at = datetime.now(UTC)
    run.finished_at = datetime.now(UTC)
    session.flush()

    art = article_of(session, ps.article_id)
    art.status = ArticleStatus.APPROVED.value
    art.wordpress_post_id = _WP_POST_ID
    session.flush()
    return art, run


def _svc(session: Session) -> WordPressPublicationRunService:
    return WordPressPublicationRunService(session)


def _prepare(session, art, run, **over):
    kwargs = dict(
        source_wordpress_draft_run_id=run.id,
        expected_wordpress_post_id=_WP_POST_ID,
        expected_target_request_identity_hash=run.target_request_identity_hash,
        wordpress_raw_content_hash=_RAW_HASH,
    )
    kwargs.update(over)
    return _svc(session).prepare(art.id, **kwargs)


# ==================== happy path (§23) ==================================
def test_prepare_happy_path(session: Session, wp_env) -> None:
    art, run = _ready(session)
    body_before, meta_before, status_before = art.body, art.meta_description, art.status

    out = _prepare(session, art, run, idempotency_key="pub-k1")

    assert out.status == "prepared"
    assert out.already_prepared is False
    assert _count(session) == 1

    pr = session.get(WordPressPublicationRun, out.run_id)
    assert pr.article_id == art.id
    assert pr.source_wordpress_draft_run_id == run.id
    assert pr.wordpress_post_id == "25"
    assert pr.method == "POST"
    assert pr.endpoint_path == "/wp-json/wp/v2/posts/25"
    assert pr.publish_payload_json == '{"status":"publish"}'
    assert pr.publish_payload_hash == compute_publish_payload_hash('{"status":"publish"}')
    assert pr.expected_pre_publish_status == "draft"
    assert pr.wordpress_raw_content_hash == _RAW_HASH
    assert pr.canonical_body_hash == run.canonical_body_hash
    assert pr.canonical_meta_hash == run.canonical_meta_hash
    assert pr.target_base_url == run.target_base_url
    assert len(pr.target_publication_request_identity_hash) == 64
    # execution/result fields null
    assert pr.wordpress_post_status is None
    assert pr.wordpress_post_url is None
    assert pr.published_at_source is None
    assert pr.response_snapshot is None
    assert pr.error_message is None
    assert pr.started_at is None and pr.finished_at is None

    # Article + source draft run unchanged
    art_after = article_of(session, art.id)
    assert art_after.status == status_before == ArticleStatus.APPROVED.value
    assert art_after.body == body_before and art_after.meta_description == meta_before
    assert art_after.wordpress_post_id == _WP_POST_ID
    assert art_after.published_url is None and art_after.published_at is None
    src = session.get(WordPressDraftRun, run.id)
    assert src.status == WP_RUN_SUCCEEDED and src.wordpress_post_status == "draft"


def test_prepare_idempotent_same_key_returns_existing(session: Session, wp_env) -> None:
    art, run = _ready(session)
    a = _prepare(session, art, run, idempotency_key="pub-k1")
    b = _prepare(session, art, run, idempotency_key="pub-k1")
    assert a.run_id == b.run_id
    assert b.already_prepared is True
    assert _count(session) == 1


def test_prepare_same_key_different_identity_conflict(session: Session, wp_env) -> None:
    art, run = _ready(session)
    _prepare(session, art, run, idempotency_key="pub-k1")
    with pytest.raises(WordPressPublicationRunConflictError):
        _prepare(
            session, art, run, idempotency_key="pub-k1",
            wordpress_raw_content_hash="b" * 64,
        )
    assert _count(session) == 1


def test_prepare_duplicate_active_identity_returns_existing(session: Session, wp_env) -> None:
    art, run = _ready(session)
    a = _prepare(session, art, run)  # no idempotency key
    b = _prepare(session, art, run)  # same identity, still prepared -> returns existing
    assert a.run_id == b.run_id
    assert b.already_prepared is True
    assert _count(session) == 1


def test_prepare_rejected_when_succeeded_run_exists(session: Session, wp_env) -> None:
    art, run = _ready(session)
    a = _prepare(session, art, run)
    pr = session.get(WordPressPublicationRun, a.run_id)
    pr.status = "succeeded"
    session.flush()
    with pytest.raises(WordPressPublicationRunConflictError):
        _prepare(session, art, run, idempotency_key="pub-k2")
    assert _count(session) == 1


# ==================== guards (§24) =====================================
def test_prepare_rejects_unknown_source_draft_run(session: Session, wp_env) -> None:
    art, run = _ready(session)
    with pytest.raises(EntityNotFoundError):
        _prepare(session, art, run, source_wordpress_draft_run_id=999999)
    assert _count(session) == 0


def test_prepare_rejects_unknown_article(session: Session, wp_env) -> None:
    art, run = _ready(session)
    with pytest.raises(EntityNotFoundError):
        _svc(session).prepare(
            art.id + 99999,
            source_wordpress_draft_run_id=run.id,
            expected_wordpress_post_id=_WP_POST_ID,
            expected_target_request_identity_hash=run.target_request_identity_hash,
            wordpress_raw_content_hash=_RAW_HASH,
        )
    assert _count(session) == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda art, run, s: setattr(art, "status", ArticleStatus.REVIEW.value),
        lambda art, run, s: setattr(art, "wordpress_post_id", None),
        lambda art, run, s: setattr(art, "published_url", "https://example.com/x"),
        lambda art, run, s: setattr(art, "published_at", datetime.now(UTC)),
        lambda art, run, s: setattr(art, "body", art.body + "\n\nドリフト"),
        lambda art, run, s: setattr(art, "meta_description", art.meta_description + "x"),
        lambda art, run, s: setattr(run, "status", "failed"),
        lambda art, run, s: setattr(run, "wordpress_post_status", "publish"),
        lambda art, run, s: setattr(run, "wordpress_post_id", "26"),
        lambda art, run, s: setattr(run, "error_message", "boom"),
    ],
)
def test_prepare_guard_rejections(session: Session, wp_env, mutate) -> None:
    art, run = _ready(session)
    mutate(art, run, session)
    session.flush()
    with pytest.raises(WordPressPublicationRunPreparationError):
        _prepare(session, art, run)
    assert _count(session) == 0
    # Article + source run not further mutated by the failed prepare
    assert session.get(WordPressPublicationRun, 1) is None


def test_prepare_rejects_wrong_expected_post_id(session: Session, wp_env) -> None:
    art, run = _ready(session)
    with pytest.raises(WordPressPublicationRunPreparationError):
        _prepare(session, art, run, expected_wordpress_post_id=_WP_POST_ID + 1)
    assert _count(session) == 0


def test_prepare_rejects_wrong_expected_target_identity(session: Session, wp_env) -> None:
    art, run = _ready(session)
    with pytest.raises(WordPressPublicationRunPreparationError):
        _prepare(session, art, run, expected_target_request_identity_hash="0" * 64)
    assert _count(session) == 0


def test_prepare_rejects_bad_raw_content_hash_format(session: Session, wp_env) -> None:
    art, run = _ready(session)
    with pytest.raises(WordPressPublicationRunPreparationError):
        _prepare(session, art, run, wordpress_raw_content_hash="NOT-HEX")
    assert _count(session) == 0


def test_prepare_rejects_source_run_wrong_article(session: Session, wp_env) -> None:
    art, run = _ready(session)
    # a second scenario's draft run belongs to another Article
    _art2, run2 = _ready(session, suffix="b")
    with pytest.raises(WordPressPublicationRunPreparationError):
        _prepare(session, art, run2)
    assert _count(session) == 0


def test_prepare_rejects_target_mismatch(session: Session, wp_env, monkeypatch) -> None:
    art, run = _ready(session)
    monkeypatch.setenv("WORDPRESS_BASE_URL", "https://different.example")
    get_settings.cache_clear()
    try:
        with pytest.raises(WordPressPublicationRunPreparationError):
            _prepare(session, art, run)
    finally:
        get_settings.cache_clear()
    assert _count(session) == 0


# ==================== API (§16 extra="forbid") ==========================
def test_api_prepare_and_reads(api_client, session: Session, wp_env) -> None:
    art, run = _ready(session)
    resp = api_client.post(
        f"/api/v1/articles/{art.id}/wordpress-publication-runs",
        json={
            "source_wordpress_draft_run_id": run.id,
            "expected_wordpress_post_id": _WP_POST_ID,
            "expected_target_request_identity_hash": run.target_request_identity_hash,
            "wordpress_raw_content_hash": _RAW_HASH,
            "idempotency_key": "api-pub-k1",
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "prepared"
    assert data["endpoint_path"] == "/wp-json/wp/v2/posts/25"
    assert data["publish_payload_json"] == '{"status":"publish"}'
    tpri = data["target_publication_request_identity_hash"]
    assert len(tpri) == 64

    lst = api_client.get(f"/api/v1/articles/{art.id}/wordpress-publication-runs")
    assert lst.status_code == 200 and len(lst.json()) == 1

    detail = api_client.get(
        f"/api/v1/articles/{art.id}/wordpress-publication-runs/{data['run_id']}"
    )
    assert detail.status_code == 200
    assert detail.json()["publish_payload_json"] == '{"status":"publish"}'


def test_api_prepare_rejects_unexpected_fields(api_client, session: Session, wp_env) -> None:
    art, run = _ready(session)
    resp = api_client.post(
        f"/api/v1/articles/{art.id}/wordpress-publication-runs",
        json={
            "source_wordpress_draft_run_id": run.id,
            "expected_wordpress_post_id": _WP_POST_ID,
            "expected_target_request_identity_hash": run.target_request_identity_hash,
            "wordpress_raw_content_hash": _RAW_HASH,
            "target_base_url": "https://evil.example",
        },
    )
    assert resp.status_code == 422


def test_api_prepare_not_approved_returns_409(api_client, session: Session, wp_env) -> None:
    art, run = _ready(session)
    art.status = ArticleStatus.REVIEW.value
    session.flush()
    resp = api_client.post(
        f"/api/v1/articles/{art.id}/wordpress-publication-runs",
        json={
            "source_wordpress_draft_run_id": run.id,
            "expected_wordpress_post_id": _WP_POST_ID,
            "expected_target_request_identity_hash": run.target_request_identity_hash,
            "wordpress_raw_content_hash": _RAW_HASH,
        },
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "wordpress_publication_run_preparation_error"
