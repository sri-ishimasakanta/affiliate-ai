"""WordPress draft-request preview: read-only / gates / drift guards / no secrets。"""

from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.article.draft_promotion_canonical import compute_text_hash
from app.exceptions import RenderedCandidateChangedError
from app.models import ArticleDraftPromotion, DraftGenerationRun
from app.services.wordpress_preview_service import WordPressPreviewService
from tests.support.draft_promotion_fixture import (
    article_of,
    promotable_scenario,
    promoted_scenario,
)


def _svc(session: Session) -> WordPressPreviewService:
    return WordPressPreviewService(session)


def test_draft_request_preview_builds_exact_payload(session: Session) -> None:
    ps = promoted_scenario(session)
    art = article_of(session, ps.article_id)
    pv = _svc(session).preview(ps.article_id)

    out = _svc(session).draft_request_preview(ps.article_id)

    assert out.method == "POST"
    assert out.endpoint_path == "/wp-json/wp/v2/posts"
    assert out.target_post_status == "draft"
    assert out.payload is not None
    assert sorted(out.payload) == ["content", "excerpt", "slug", "status", "title"]
    assert out.payload["status"] == "draft"
    assert out.payload["title"] == art.title
    assert out.payload["slug"] == art.slug
    assert out.payload["excerpt"] == art.meta_description
    assert out.payload["content"] == pv.rendered_content
    assert out.rendered_content_chars == pv.rendered_content_chars
    assert out.canonical_body_hash == compute_text_hash(art.body)
    assert out.canonical_meta_hash == compute_text_hash(art.meta_description)
    assert out.rendered_content_hash == pv.rendered_content_hash
    assert out.publishable is True
    assert out.blocking_reasons == []

    # payload_hash == SHA-256(canonical JSON of payload)
    canon = json.dumps(
        out.payload, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False,
    )
    assert hashlib.sha256(canon.encode("utf-8")).hexdigest() == out.payload_hash
    assert len(out.request_identity_hash) == 64


def test_draft_request_preview_no_db_write(session: Session) -> None:
    ps = promoted_scenario(session)
    p_before = session.scalar(select(func.count()).select_from(ArticleDraftPromotion))
    r_before = session.scalar(select(func.count()).select_from(DraftGenerationRun))
    _svc(session).draft_request_preview(ps.article_id)
    assert session.scalar(
        select(func.count()).select_from(ArticleDraftPromotion)
    ) == p_before
    assert session.scalar(
        select(func.count()).select_from(DraftGenerationRun)
    ) == r_before
    art = article_of(session, ps.article_id)
    assert art.status == "review"
    assert art.published_url is None and art.wordpress_post_id is None


def test_draft_request_preview_deterministic(session: Session) -> None:
    ps = promoted_scenario(session)
    a = _svc(session).draft_request_preview(ps.article_id)
    b = _svc(session).draft_request_preview(ps.article_id)
    assert a.payload_hash == b.payload_hash
    assert a.request_identity_hash == b.request_identity_hash


def test_expected_renderer_version_mismatch_rejected(session: Session) -> None:
    ps = promoted_scenario(session)
    with pytest.raises(RenderedCandidateChangedError):
        _svc(session).draft_request_preview(
            ps.article_id, expected_renderer_version="wordpress_html_v2"
        )


def test_expected_rendered_hash_mismatch_rejected(session: Session) -> None:
    ps = promoted_scenario(session)
    with pytest.raises(RenderedCandidateChangedError):
        _svc(session).draft_request_preview(
            ps.article_id, expected_rendered_content_hash="0" * 64
        )


def test_matching_expected_guards_pass(session: Session) -> None:
    ps = promoted_scenario(session)
    pv = _svc(session).preview(ps.article_id)
    out = _svc(session).draft_request_preview(
        ps.article_id,
        expected_renderer_version=pv.renderer_version,
        expected_rendered_content_hash=pv.rendered_content_hash,
    )
    assert out.payload_hash is not None


def test_not_publishable_yields_no_payload(session: Session) -> None:
    ps = promotable_scenario(session)  # not promoted -> not publishable
    out = _svc(session).draft_request_preview(ps.article_id)
    assert out.publishable is False
    assert out.payload is None
    assert out.payload_hash is None
    assert out.request_identity_hash is None
    assert "promotion_exists" in out.blocking_reasons
    assert out.target_post_status == "draft"


def test_api_preview_no_secrets_even_with_env(api_client, session, monkeypatch) -> None:
    from app.config.settings import get_settings

    monkeypatch.setenv("WORDPRESS_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("WORDPRESS_USERNAME", "wp-user")
    monkeypatch.setenv("WORDPRESS_APP_PASSWORD", "abcd efgh ijkl mnop")
    get_settings.cache_clear()
    try:
        ps = promoted_scenario(session)
        resp = api_client.post(
            f"/api/v1/articles/{ps.article_id}/wordpress-draft-request-preview",
            json={
                "expected_renderer_version": "wordpress_html_v1",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["wordpress_configured"] is True
        blob = json.dumps(data, ensure_ascii=False).lower()
        for tok in ("wp-user", "abcd efgh", "app_password", "authorization",
                    "example.invalid"):
            assert tok not in blob
        assert data["endpoint_path"] == "/wp-json/wp/v2/posts"
        assert data["target_post_status"] == "draft"
    finally:
        get_settings.cache_clear()


def test_api_preview_wrong_hash_409(api_client, session: Session) -> None:
    ps = promoted_scenario(session)
    resp = api_client.post(
        f"/api/v1/articles/{ps.article_id}/wordpress-draft-request-preview",
        json={"expected_rendered_content_hash": "0" * 64},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "rendered_candidate_changed"


def test_api_preview_missing_article_404(api_client) -> None:
    resp = api_client.post(
        "/api/v1/articles/999999/wordpress-draft-request-preview", json={}
    )
    assert resp.status_code == 404
