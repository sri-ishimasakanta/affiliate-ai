"""WordPressDraftRunService.prepare: gates / drift / idempotency / duplicate / tx / secrets。"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.article.draft_promotion_canonical import compute_text_hash
from app.config.settings import Settings, get_settings
from app.exceptions import (
    EntityNotFoundError,
    RenderedCandidateChangedError,
    WordPressDraftRunStateError,
)
from app.models import ArticleDraftPromotion, DraftGenerationRun, WordPressDraftRun
from app.models.enums import ArticleStatus
from app.services.wordpress_draft_run_service import WordPressDraftRunService
from app.services.wordpress_preview_service import WordPressPreviewService
from tests.support.draft_promotion_fixture import article_of, promoted_scenario

_BASE_URL = "https://wp.example.test/blog"


@pytest.fixture
def wp_env(monkeypatch):
    monkeypatch.setenv("WORDPRESS_BASE_URL", _BASE_URL)
    monkeypatch.setenv("WORDPRESS_USERNAME", "wp-user-secret")
    monkeypatch.setenv("WORDPRESS_APP_PASSWORD", "aaaa bbbb cccc dddd")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _svc(session: Session) -> WordPressDraftRunService:
    return WordPressDraftRunService(session)


def _count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(WordPressDraftRun))


def _expected(session: Session, ps):
    dp = WordPressPreviewService(session).draft_request_preview(
        ps.article_id,
        expected_renderer_version="wordpress_html_v1",
    )
    return {
        "expected_renderer_version": dp.renderer_version,
        "expected_rendered_content_hash": dp.rendered_content_hash,
        "expected_payload_hash": dp.payload_hash,
        "expected_request_identity_hash": dp.request_identity_hash,
    }, dp


def _promotion_id(session, article_id) -> int:
    row = session.scalars(
        select(ArticleDraftPromotion).where(
            ArticleDraftPromotion.article_id == article_id
        )
    ).first()
    return row.id


# -- happy path -----------------------------------------------------
def test_prepare_creates_one_prepared_run(session: Session, wp_env) -> None:
    ps = promoted_scenario(session)
    exp, dp = _expected(session, ps)

    out = _svc(session).prepare(
        ps.article_id,
        source_promotion_id=_promotion_id(session, ps.article_id),
        **exp,
    )
    assert out.status == "prepared"
    assert out.already_prepared is False
    assert _count(session) == 1

    run = session.get(WordPressDraftRun, out.run_id)
    assert run.article_id == ps.article_id
    assert run.method == "POST"
    assert run.endpoint_path == "/wp-json/wp/v2/posts"
    assert run.target_base_url == _BASE_URL  # already canonical
    assert run.payload_hash == dp.payload_hash
    assert run.request_identity_hash == dp.request_identity_hash
    assert compute_text_hash(run.payload_json) == dp.payload_hash
    assert json.loads(run.payload_json)["status"] == "draft"
    assert run.wordpress_post_id is None
    assert run.wordpress_post_status is None
    assert run.error_message is None
    assert run.started_at is None and run.finished_at is None
    # target hash = deterministic bind of (request_identity_hash, canonical base url)
    from app.wordpress.target import compute_target_request_identity_hash

    assert out.target_request_identity_hash == compute_target_request_identity_hash(
        request_identity_hash=dp.request_identity_hash, target_base_url=_BASE_URL
    )
    assert len(out.target_request_identity_hash) == 64

    # source run + promotion + Article untouched
    art = article_of(session, ps.article_id)
    assert art.status == ArticleStatus.REVIEW.value
    assert art.wordpress_post_id is None and art.published_url is None
    assert session.get(DraftGenerationRun, ps.run_id).status == "succeeded"


def test_prepare_target_base_url_canonicalized(session: Session, monkeypatch) -> None:
    monkeypatch.setenv("WORDPRESS_BASE_URL", "HTTPS://WP.Example.Test/blog/")
    get_settings.cache_clear()
    try:
        ps = promoted_scenario(session)
        exp, _ = _expected(session, ps)
        out = _svc(session).prepare(
            ps.article_id,
            source_promotion_id=_promotion_id(session, ps.article_id),
            **exp,
        )
        run = session.get(WordPressDraftRun, out.run_id)
        assert run.target_base_url == "https://wp.example.test/blog"
    finally:
        get_settings.cache_clear()


def test_prepare_missing_base_url_state_error(session: Session, monkeypatch) -> None:
    # monkeypatch.delenv alone is not sufficient: Settings falls back to the
    # real .env file (SettingsConfigDict(env_file=".env")), so a developer's
    # local WORDPRESS_BASE_URL would silently defeat this test. Isolate the
    # settings object explicitly with _env_file=None (same pattern as
    # test_google_ads_optional_and_unconfigured_by_default in
    # tests/unit/test_settings.py) and patch it into the exact module where
    # WordPressDraftRunService consumes get_settings.
    monkeypatch.delenv("WORDPRESS_BASE_URL", raising=False)
    isolated_settings = Settings(_env_file=None)
    monkeypatch.setattr(
        "app.services.wordpress_draft_run_service.get_settings",
        lambda: isolated_settings,
    )
    ps = promoted_scenario(session)
    exp, _ = _expected(session, ps)
    with pytest.raises(WordPressDraftRunStateError):
        _svc(session).prepare(
            ps.article_id,
            source_promotion_id=_promotion_id(session, ps.article_id),
            **exp,
        )
    assert _count(session) == 0


# -- drift guards -------------------------------------------------
def test_prepare_rejects_renderer_version_drift(session: Session, wp_env) -> None:
    ps = promoted_scenario(session)
    exp, _ = _expected(session, ps)
    exp["expected_renderer_version"] = "wordpress_html_v2"
    with pytest.raises(RenderedCandidateChangedError):
        _svc(session).prepare(
            ps.article_id,
            source_promotion_id=_promotion_id(session, ps.article_id),
            **exp,
        )
    assert _count(session) == 0


def test_prepare_rejects_rendered_hash_drift(session: Session, wp_env) -> None:
    ps = promoted_scenario(session)
    exp, _ = _expected(session, ps)
    exp["expected_rendered_content_hash"] = "0" * 64
    with pytest.raises(RenderedCandidateChangedError):
        _svc(session).prepare(
            ps.article_id,
            source_promotion_id=_promotion_id(session, ps.article_id),
            **exp,
        )
    assert _count(session) == 0


def test_prepare_rejects_payload_hash_drift(session: Session, wp_env) -> None:
    ps = promoted_scenario(session)
    exp, _ = _expected(session, ps)
    exp["expected_payload_hash"] = "0" * 64
    with pytest.raises(WordPressDraftRunStateError):
        _svc(session).prepare(
            ps.article_id,
            source_promotion_id=_promotion_id(session, ps.article_id),
            **exp,
        )
    assert _count(session) == 0


def test_prepare_rejects_request_identity_drift(session: Session, wp_env) -> None:
    ps = promoted_scenario(session)
    exp, _ = _expected(session, ps)
    exp["expected_request_identity_hash"] = "0" * 64
    with pytest.raises(WordPressDraftRunStateError):
        _svc(session).prepare(
            ps.article_id,
            source_promotion_id=_promotion_id(session, ps.article_id),
            **exp,
        )
    assert _count(session) == 0


def test_prepare_rejects_wrong_article_status(session: Session, wp_env) -> None:
    ps = promoted_scenario(session)
    exp, _ = _expected(session, ps)
    art = article_of(session, ps.article_id)
    art.status = ArticleStatus.APPROVED.value
    session.flush()
    with pytest.raises(WordPressDraftRunStateError):
        _svc(session).prepare(
            ps.article_id,
            source_promotion_id=_promotion_id(session, ps.article_id),
            **exp,
        )
    assert _count(session) == 0


def test_prepare_rejects_wrong_promotion_id(session: Session, wp_env) -> None:
    ps = promoted_scenario(session)
    exp, _ = _expected(session, ps)
    with pytest.raises(WordPressDraftRunStateError):
        _svc(session).prepare(
            ps.article_id, source_promotion_id=99999, **exp
        )
    assert _count(session) == 0


def test_prepare_missing_article_404(session: Session, wp_env) -> None:
    ps = promoted_scenario(session)
    exp, _ = _expected(session, ps)
    with pytest.raises(EntityNotFoundError):
        _svc(session).prepare(999999, source_promotion_id=1, **exp)


# -- idempotency / duplicate -----------------------------------
def test_idempotency_same_key_returns_existing(session: Session, wp_env) -> None:
    ps = promoted_scenario(session)
    exp, _ = _expected(session, ps)
    pid = _promotion_id(session, ps.article_id)
    a = _svc(session).prepare(ps.article_id, source_promotion_id=pid, **exp,
                              idempotency_key="k-1")
    b = _svc(session).prepare(ps.article_id, source_promotion_id=pid, **exp,
                              idempotency_key="k-1")
    assert a.run_id == b.run_id
    assert b.already_prepared is True
    assert _count(session) == 1


def test_duplicate_active_run_same_target_returns_existing(
    session: Session, wp_env
) -> None:
    ps = promoted_scenario(session)
    exp, _ = _expected(session, ps)
    pid = _promotion_id(session, ps.article_id)
    a = _svc(session).prepare(ps.article_id, source_promotion_id=pid, **exp)
    # no idempotency key this time -> duplicate protection kicks in
    b = _svc(session).prepare(ps.article_id, source_promotion_id=pid, **exp)
    assert a.run_id == b.run_id
    assert b.already_prepared is True
    assert _count(session) == 1


def test_idempotency_key_conflict_different_target(session: Session, monkeypatch) -> None:
    monkeypatch.setenv("WORDPRESS_BASE_URL", "https://wp-a.example.test")
    get_settings.cache_clear()
    try:
        ps = promoted_scenario(session)
        exp, _ = _expected(session, ps)
        pid = _promotion_id(session, ps.article_id)
        _svc(session).prepare(ps.article_id, source_promotion_id=pid, **exp,
                              idempotency_key="k-x")
    finally:
        pass
    monkeypatch.setenv("WORDPRESS_BASE_URL", "https://wp-b.example.test")
    get_settings.cache_clear()
    try:
        with pytest.raises(WordPressDraftRunStateError):
            _svc(session).prepare(ps.article_id, source_promotion_id=pid, **exp,
                                  idempotency_key="k-x")
        assert _count(session) == 1
    finally:
        get_settings.cache_clear()


# -- transaction / secrets ------------------------------------
def test_prepare_service_owns_single_commit(session: Session, wp_env, monkeypatch) -> None:
    ps = promoted_scenario(session)
    exp, _ = _expected(session, ps)
    commits = {"n": 0}
    real_commit = session.commit
    monkeypatch.setattr(
        session, "commit", lambda: (commits.__setitem__("n", commits["n"] + 1),
                                    real_commit())[1]
    )
    _svc(session).prepare(
        ps.article_id, source_promotion_id=_promotion_id(session, ps.article_id), **exp
    )
    assert commits["n"] == 1
    assert _count(session) == 1


def test_prepare_rolls_back_on_commit_failure(session: Session, wp_env, monkeypatch) -> None:
    ps = promoted_scenario(session)
    exp, _ = _expected(session, ps)

    def _boom():
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(session, "commit", _boom)
    with pytest.raises(RuntimeError):
        _svc(session).prepare(
            ps.article_id,
            source_promotion_id=_promotion_id(session, ps.article_id),
            **exp,
        )
    monkeypatch.undo()
    assert _count(session) == 0


def test_prepare_row_and_response_hold_no_secrets(session: Session, wp_env) -> None:
    ps = promoted_scenario(session)
    exp, _ = _expected(session, ps)
    out = _svc(session).prepare(
        ps.article_id, source_promotion_id=_promotion_id(session, ps.article_id), **exp
    )
    run = session.get(WordPressDraftRun, out.run_id)
    blob = " ".join(
        str(getattr(run, c.name)) for c in WordPressDraftRun.__table__.columns
    ).lower()
    for tok in ("wp-user-secret", "aaaa bbbb", "authorization", "app_password",
                "password="):
        assert tok not in blob
    resp_blob = json.dumps(out.model_dump(), ensure_ascii=False, default=str).lower()
    for tok in ("wp-user-secret", "aaaa bbbb", "authorization", "app_password"):
        assert tok not in resp_blob


def test_api_prepare_and_reads(api_client, session: Session, monkeypatch) -> None:
    monkeypatch.setenv("WORDPRESS_BASE_URL", _BASE_URL)
    get_settings.cache_clear()
    try:
        ps = promoted_scenario(session)
        exp, _ = _expected(session, ps)
        resp = api_client.post(
            f"/api/v1/articles/{ps.article_id}/wordpress-draft-runs/prepare",
            json={
                "source_promotion_id": _promotion_id(session, ps.article_id),
                **exp,
                "idempotency_key": "api-k1",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "prepared"
        assert data["target_base_url"] == _BASE_URL
        assert "app_password" not in json.dumps(data).lower()

        lst = api_client.get(
            f"/api/v1/articles/{ps.article_id}/wordpress-draft-runs"
        )
        assert lst.status_code == 200 and len(lst.json()) == 1

        detail = api_client.get(
            f"/api/v1/articles/{ps.article_id}/wordpress-draft-runs/{data['run_id']}"
        )
        assert detail.status_code == 200
        d = detail.json()
        # stored payload_json is the canonical (sorted-key) serialization
        assert sorted(d["payload_keys"]) == ["content", "excerpt", "slug", "status", "title"]
        assert json.loads(d["payload_json"])["status"] == "draft"
        assert compute_text_hash(d["payload_json"]) == d["payload_hash"]
    finally:
        get_settings.cache_clear()
