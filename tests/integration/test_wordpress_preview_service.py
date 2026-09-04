"""WordPressPreviewService: read-only / no-network / canonical-immutable。"""

from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.article.draft_promotion_canonical import compute_text_hash
from app.models import ArticleDraftPromotion, DraftGenerationRun
from app.services.wordpress_preview_service import WordPressPreviewService
from tests.support.draft_promotion_fixture import (
    article_of,
    promotable_scenario,
    promoted_scenario,
)


def _svc(session: Session) -> WordPressPreviewService:
    return WordPressPreviewService(session)


def test_preview_read_only_and_publishable(session: Session) -> None:
    ps = promoted_scenario(session)
    promotions_before = session.scalar(
        select(func.count()).select_from(ArticleDraftPromotion)
    )
    art_before = article_of(session, ps.article_id)
    body_before, meta_before, status_before = (
        art_before.body,
        art_before.meta_description,
        art_before.status,
    )

    out = _svc(session).preview(ps.article_id)

    assert out.publishable is True
    assert out.validation_report["overall"] == "pass"
    assert out.target_post_status == "draft"
    assert out.renderer_version == "wordpress_html_v1"
    assert out.canonical_body_hash == compute_text_hash(ps.body_markdown)
    assert out.canonical_meta_hash == compute_text_hash(ps.meta_description)
    assert out.rendered_h1_count == 0
    assert out.rendered_h2_count == 7 and out.rendered_h3_count == 7
    assert out.rendered_table_count == 1
    assert out.affiliate_substitutions == []
    assert out.internal_link_substitutions == []
    assert out.seo_meta_integration_supported is False
    assert out.wordpress_configured is False
    assert out.wp_excerpt == ps.meta_description
    assert out.featured_image is None
    assert out.inline_image_count == 0
    assert out.image_blocker is False

    # read-only: nothing changed
    assert session.scalar(
        select(func.count()).select_from(ArticleDraftPromotion)
    ) == promotions_before
    art_after = article_of(session, ps.article_id)
    assert art_after.body == body_before
    assert art_after.meta_description == meta_before
    assert art_after.status == status_before
    assert art_after.published_url is None
    assert art_after.wordpress_post_id is None
    assert art_after.published_at is None


def test_preview_deterministic(session: Session) -> None:
    ps = promoted_scenario(session)
    a = _svc(session).preview(ps.article_id)
    b = _svc(session).preview(ps.article_id)
    assert a.rendered_content_hash == b.rendered_content_hash
    assert a.canonical_body_hash == b.canonical_body_hash


def test_preview_no_secrets_in_response(session: Session) -> None:
    ps = promoted_scenario(session)
    out = _svc(session).preview(ps.article_id)
    blob = json.dumps(out.model_dump(), ensure_ascii=False).lower()
    for tok in ("app_password", "authorization", "bearer ", "wordpress_app_password"):
        assert tok not in blob


def test_preview_before_promotion_is_not_publishable(session: Session) -> None:
    # promotable but not promoted -> Article still 'drafting', no promotion row
    ps = promotable_scenario(session)
    out = _svc(session).preview(ps.article_id)
    ids = {c["id"] for c in out.validation_report["checks"] if c["level"] == "fail"}
    assert "promotion_exists" in ids
    assert "article_status_review" in ids
    assert out.publishable is False
    assert session.scalar(
        select(func.count()).select_from(ArticleDraftPromotion)
    ) == 0


def test_preview_via_api_does_no_writes(api_client, session: Session) -> None:
    ps = promoted_scenario(session)
    runs_before = session.scalar(select(func.count()).select_from(DraftGenerationRun))

    resp = api_client.post(f"/api/v1/articles/{ps.article_id}/wordpress-preview")
    assert resp.status_code == 200
    data = resp.json()
    assert data["target_post_status"] == "draft"
    assert data["publishable"] is True
    assert data["wordpress_configured"] is False

    assert session.scalar(
        select(func.count()).select_from(DraftGenerationRun)
    ) == runs_before
    art = article_of(session, ps.article_id)
    assert art.status == "review"
    assert art.published_url is None and art.wordpress_post_id is None


def test_preview_missing_article_404(api_client) -> None:
    resp = api_client.post("/api/v1/articles/999999/wordpress-preview")
    assert resp.status_code == 404
