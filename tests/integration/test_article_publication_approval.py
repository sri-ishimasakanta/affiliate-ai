"""ArticlePublicationApprovalService.approve: guards / lifecycle / atomicity。

WordPress へは一切通信しない。WordPressDraftRun#succeeded は直接 DB 上で
simulate する (execute() の外部 HTTP 経路自体は別のテストファイルで検証済み)。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.exceptions import ArticlePublicationApprovalError, EntityNotFoundError
from app.models import ArticleDraftPromotion, WordPressDraftRun
from app.models.enums import ArticleStatus
from app.models.wordpress_draft_run import WP_RUN_FAILED, WP_RUN_SUCCEEDED
from app.services.article_publication_approval_service import (
    ArticlePublicationApprovalService,
)
from app.services.wordpress_draft_run_service import WordPressDraftRunService
from app.services.wordpress_preview_service import WordPressPreviewService
from tests.support.draft_promotion_fixture import article_of, promoted_scenario

_WP_POST_ID = 999


def _promotion_id(session: Session, article_id: int) -> int:
    row = session.scalars(
        select(ArticleDraftPromotion).where(ArticleDraftPromotion.article_id == article_id)
    ).first()
    return row.id


def _approved_ready(session: Session):
    """review 状態の Article + succeeded/draft な WordPressDraftRun を用意する。"""

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
    run = session.get(WordPressDraftRun, out.run_id)
    # execute() が到達させる succeeded/draft 状態を直接 simulate する (WordPress 通信なし)。
    run.status = WP_RUN_SUCCEEDED
    run.wordpress_post_id = str(_WP_POST_ID)
    run.wordpress_post_status = "draft"
    run.started_at = datetime.now(UTC)
    run.finished_at = datetime.now(UTC)
    session.flush()

    art = article_of(session, ps.article_id)
    art.wordpress_post_id = _WP_POST_ID
    session.flush()

    return art, run


def _svc(session: Session) -> ArticlePublicationApprovalService:
    return ArticlePublicationApprovalService(session)


# ==================== happy path ====================================
def test_approve_happy_path(session: Session) -> None:
    art, run = _approved_ready(session)
    body_before, meta_before, slug_before, title_before = (
        art.body, art.meta_description, art.slug, art.title,
    )

    out = _svc(session).approve(
        art.id,
        expected_wordpress_post_id=_WP_POST_ID,
        expected_target_request_identity_hash=run.target_request_identity_hash,
    )
    assert out.status == ArticleStatus.APPROVED

    persisted = article_of(session, art.id)
    assert persisted.status == ArticleStatus.APPROVED.value
    assert persisted.wordpress_post_id == _WP_POST_ID
    assert persisted.published_url is None
    assert persisted.published_at is None
    # content unchanged
    assert persisted.body == body_before
    assert persisted.meta_description == meta_before
    assert persisted.slug == slug_before
    assert persisted.title == title_before

    # no run mutation
    persisted_run = session.get(WordPressDraftRun, run.id)
    assert persisted_run.status == WP_RUN_SUCCEEDED
    assert persisted_run.wordpress_post_id == str(_WP_POST_ID)
    assert persisted_run.wordpress_post_status == "draft"


def test_repeat_approval_rejected(session: Session) -> None:
    art, run = _approved_ready(session)
    _svc(session).approve(
        art.id,
        expected_wordpress_post_id=_WP_POST_ID,
        expected_target_request_identity_hash=run.target_request_identity_hash,
    )
    with pytest.raises(ArticlePublicationApprovalError):
        _svc(session).approve(
            art.id,
            expected_wordpress_post_id=_WP_POST_ID,
            expected_target_request_identity_hash=run.target_request_identity_hash,
        )
    assert article_of(session, art.id).status == ArticleStatus.APPROVED.value


# ==================== state guards ====================================
def test_wrong_article_id(session: Session) -> None:
    art, run = _approved_ready(session)
    with pytest.raises(EntityNotFoundError):
        _svc(session).approve(
            art.id + 999,
            expected_wordpress_post_id=_WP_POST_ID,
            expected_target_request_identity_hash=run.target_request_identity_hash,
        )


def test_article_not_review(session: Session) -> None:
    art, run = _approved_ready(session)
    art.status = ArticleStatus.REWRITE.value
    session.flush()
    with pytest.raises(ArticlePublicationApprovalError):
        _svc(session).approve(
            art.id,
            expected_wordpress_post_id=_WP_POST_ID,
            expected_target_request_identity_hash=run.target_request_identity_hash,
        )
    assert article_of(session, art.id).status == ArticleStatus.REWRITE.value


def test_missing_wordpress_post_id(session: Session) -> None:
    art, run = _approved_ready(session)
    art.wordpress_post_id = None
    session.flush()
    with pytest.raises(ArticlePublicationApprovalError):
        _svc(session).approve(
            art.id,
            expected_wordpress_post_id=_WP_POST_ID,
            expected_target_request_identity_hash=run.target_request_identity_hash,
        )
    assert article_of(session, art.id).status == ArticleStatus.REVIEW.value


def test_wordpress_post_id_mismatch(session: Session) -> None:
    art, run = _approved_ready(session)
    with pytest.raises(ArticlePublicationApprovalError):
        _svc(session).approve(
            art.id,
            expected_wordpress_post_id=_WP_POST_ID + 1,
            expected_target_request_identity_hash=run.target_request_identity_hash,
        )
    assert article_of(session, art.id).status == ArticleStatus.REVIEW.value


def test_published_url_already_set(session: Session) -> None:
    art, run = _approved_ready(session)
    art.published_url = "https://example.com/already-published"
    session.flush()
    with pytest.raises(ArticlePublicationApprovalError):
        _svc(session).approve(
            art.id,
            expected_wordpress_post_id=_WP_POST_ID,
            expected_target_request_identity_hash=run.target_request_identity_hash,
        )
    assert article_of(session, art.id).status == ArticleStatus.REVIEW.value


def test_published_at_already_set(session: Session) -> None:
    art, run = _approved_ready(session)
    art.published_at = datetime.now(UTC)
    session.flush()
    with pytest.raises(ArticlePublicationApprovalError):
        _svc(session).approve(
            art.id,
            expected_wordpress_post_id=_WP_POST_ID,
            expected_target_request_identity_hash=run.target_request_identity_hash,
        )
    assert article_of(session, art.id).status == ArticleStatus.REVIEW.value


def test_body_hash_drift(session: Session) -> None:
    art, run = _approved_ready(session)
    art.body = art.body + "\n\n追記されたドリフト。"
    session.flush()
    with pytest.raises(ArticlePublicationApprovalError):
        _svc(session).approve(
            art.id,
            expected_wordpress_post_id=_WP_POST_ID,
            expected_target_request_identity_hash=run.target_request_identity_hash,
        )
    assert article_of(session, art.id).status == ArticleStatus.REVIEW.value


def test_meta_hash_drift(session: Session) -> None:
    art, run = _approved_ready(session)
    art.meta_description = art.meta_description + "追記"
    session.flush()
    with pytest.raises(ArticlePublicationApprovalError):
        _svc(session).approve(
            art.id,
            expected_wordpress_post_id=_WP_POST_ID,
            expected_target_request_identity_hash=run.target_request_identity_hash,
        )
    assert article_of(session, art.id).status == ArticleStatus.REVIEW.value


def test_missing_wordpress_draft_run(session: Session) -> None:
    art, run = _approved_ready(session)
    session.delete(run)
    session.flush()
    with pytest.raises(ArticlePublicationApprovalError):
        _svc(session).approve(
            art.id,
            expected_wordpress_post_id=_WP_POST_ID,
            expected_target_request_identity_hash="0" * 64,
        )
    assert article_of(session, art.id).status == ArticleStatus.REVIEW.value


def test_run_not_succeeded(session: Session) -> None:
    art, run = _approved_ready(session)
    run.status = WP_RUN_FAILED
    session.flush()
    with pytest.raises(ArticlePublicationApprovalError):
        _svc(session).approve(
            art.id,
            expected_wordpress_post_id=_WP_POST_ID,
            expected_target_request_identity_hash=run.target_request_identity_hash,
        )
    assert article_of(session, art.id).status == ArticleStatus.REVIEW.value


def test_run_wordpress_post_id_mismatch(session: Session) -> None:
    art, run = _approved_ready(session)
    run.wordpress_post_id = str(_WP_POST_ID + 1)
    session.flush()
    # no run now matches Article.wordpress_post_id -> treated as "missing"
    with pytest.raises(ArticlePublicationApprovalError):
        _svc(session).approve(
            art.id,
            expected_wordpress_post_id=_WP_POST_ID,
            expected_target_request_identity_hash="0" * 64,
        )
    assert article_of(session, art.id).status == ArticleStatus.REVIEW.value


def test_run_wordpress_post_status_not_draft(session: Session) -> None:
    art, run = _approved_ready(session)
    run.wordpress_post_status = "publish"
    session.flush()
    with pytest.raises(ArticlePublicationApprovalError):
        _svc(session).approve(
            art.id,
            expected_wordpress_post_id=_WP_POST_ID,
            expected_target_request_identity_hash=run.target_request_identity_hash,
        )
    assert article_of(session, art.id).status == ArticleStatus.REVIEW.value


def test_request_guard_mismatch(session: Session) -> None:
    art, run = _approved_ready(session)
    with pytest.raises(ArticlePublicationApprovalError):
        _svc(session).approve(
            art.id,
            expected_wordpress_post_id=_WP_POST_ID,
            expected_target_request_identity_hash="0" * 64,
        )
    assert article_of(session, art.id).status == ArticleStatus.REVIEW.value


# ==================== API-level: extra="forbid" ============================
def test_api_approve_rejects_unexpected_fields(api_client, session: Session) -> None:
    art, run = _approved_ready(session)
    resp = api_client.post(
        f"/api/v1/articles/{art.id}/approve",
        json={
            "expected_wordpress_post_id": _WP_POST_ID,
            "expected_target_request_identity_hash": run.target_request_identity_hash,
            "status": "approved",
        },
    )
    assert resp.status_code == 422


def test_api_approve_happy_path(api_client, session: Session) -> None:
    art, run = _approved_ready(session)
    resp = api_client.post(
        f"/api/v1/articles/{art.id}/approve",
        json={
            "expected_wordpress_post_id": _WP_POST_ID,
            "expected_target_request_identity_hash": run.target_request_identity_hash,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "approved"
    assert data["wordpress_id"] == _WP_POST_ID
    assert data["published_url"] is None
