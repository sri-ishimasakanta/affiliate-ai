"""WordPressPublicationRunService.execute: guards / preflight / publish / read-back /
failure taxonomy。

WordPress へは一切通信しない (httpx.MockTransport を注入した WordPressClient を使う)。
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.article.draft_promotion_canonical import compute_text_hash
from app.config.settings import get_settings
from app.exceptions import (
    EntityNotFoundError,
    ExternalProviderError,
    WordPressAmbiguousPublishOutcomeError,
    WordPressPublicationExternalSuccessLocalPersistFailedError,
    WordPressPublicationPreflightError,
    WordPressPublicationReadbackFailedError,
    WordPressPublicationRunExecutionError,
)
from app.models import ArticleDraftPromotion, WordPressDraftRun, WordPressPublicationRun
from app.models.enums import ArticleStatus
from app.models.wordpress_draft_run import WP_RUN_SUCCEEDED
from app.services.wordpress_draft_run_service import WordPressDraftRunService
from app.services.wordpress_preview_service import WordPressPreviewService
from app.services.wordpress_publication_run_service import WordPressPublicationRunService
from app.wordpress.client import WordPressClient
from tests.support.draft_promotion_fixture import article_of, promoted_scenario

_WP_POST_ID = 25
_BASE_URL = "https://wp.example.test/blog"
_RAW = "<div class=\"wp-table-scroll\"><table>APPROVED RAW CONTENT</table></div>"


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


def _ready(session: Session, *, raw_content: str = _RAW, suffix: str = ""):
    ps = promoted_scenario(session, suffix=suffix)
    dp = WordPressPreviewService(session).draft_request_preview(
        ps.article_id, expected_renderer_version="wordpress_html_v1"
    )
    pid = _promotion_id(session, ps.article_id)
    d_out = WordPressDraftRunService(session).prepare(
        ps.article_id,
        source_promotion_id=pid,
        expected_renderer_version=dp.renderer_version,
        expected_rendered_content_hash=dp.rendered_content_hash,
        expected_payload_hash=dp.payload_hash,
        expected_request_identity_hash=dp.request_identity_hash,
    )
    draft = session.get(WordPressDraftRun, d_out.run_id)
    draft.status = WP_RUN_SUCCEEDED
    draft.wordpress_post_id = str(_WP_POST_ID)
    draft.wordpress_post_status = "draft"
    draft.started_at = datetime.now(UTC)
    draft.finished_at = datetime.now(UTC)
    session.flush()

    art = article_of(session, ps.article_id)
    art.status = ArticleStatus.APPROVED.value
    art.wordpress_post_id = _WP_POST_ID
    session.flush()

    p_out = WordPressPublicationRunService(session).prepare(
        art.id,
        source_wordpress_draft_run_id=draft.id,
        expected_wordpress_post_id=_WP_POST_ID,
        expected_target_request_identity_hash=draft.target_request_identity_hash,
        wordpress_raw_content_hash=compute_text_hash(raw_content),
    )
    pub = session.get(WordPressPublicationRun, p_out.run_id)
    return art, draft, pub


class _WpMock:
    """preflight GET -> publish POST -> read-back GET を state で返す MockTransport handler。"""

    def __init__(
        self,
        *,
        art,
        raw_content: str = _RAW,
        preflight_status: str = "draft",
        preflight_categories=(4,),
        publish_exc: BaseException | None = None,
        publish_http: int = 200,
        publish_resp_id: int = _WP_POST_ID,
        publish_resp_status: str = "publish",
        readback_exc: BaseException | None = None,
        readback_status: str = "publish",
        readback_raw_content: str | None = None,
        readback_date_gmt: str | None = "2026-09-06T13:45:12",
    ) -> None:
        self._art = art
        self._raw = raw_content
        self._pf_status = preflight_status
        self._pf_cats = list(preflight_categories)
        self._pub_exc = publish_exc
        self._pub_http = publish_http
        self._pub_id = publish_resp_id
        self._pub_status = publish_resp_status
        self._rb_exc = readback_exc
        self._rb_status = readback_status
        self._rb_raw = readback_raw_content if readback_raw_content is not None else raw_content
        self._rb_date = readback_date_gmt
        self.get_calls = 0
        self.post_calls = 0

    def _post_json(self, status: str, raw: str) -> dict:
        return {
            "id": _WP_POST_ID,
            "status": status,
            "title": {"raw": self._art.title, "rendered": self._art.title},
            "slug": self._art.slug,
            "excerpt": {"raw": self._art.meta_description},
            "content": {"raw": raw},
            "categories": self._pf_cats,
            "link": f"https://wp.example.test/blog/?p={_WP_POST_ID}",
            "date_gmt": self._rb_date,
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            self.get_calls += 1
            if self.get_calls == 1:  # preflight
                return httpx.Response(200, json=self._post_json(self._pf_status, self._raw))
            # read-back
            if self._rb_exc is not None:
                raise self._rb_exc
            body = self._post_json(self._rb_status, self._rb_raw)
            return httpx.Response(200, json=body)
        if request.method == "POST":
            self.post_calls += 1
            assert request.content == b'{"status":"publish"}'
            if self._pub_exc is not None:
                raise self._pub_exc
            if self._pub_http != 200:
                return httpx.Response(self._pub_http, json={"code": "x"})
            return httpx.Response(
                200,
                json={
                    "id": self._pub_id,
                    "status": self._pub_status,
                    "slug": self._art.slug,
                    "link": f"https://wp.example.test/blog/?p={self._pub_id}",
                    "date_gmt": self._rb_date,
                },
            )
        raise AssertionError(f"unexpected method {request.method}")


def _svc(session: Session, mock: _WpMock) -> WordPressPublicationRunService:
    client = WordPressClient(get_settings(), transport=httpx.MockTransport(mock))
    return WordPressPublicationRunService(session, wordpress_client=client)


def _execute(session, art, pub, mock, **over):
    kwargs = dict(
        expected_target_publication_request_identity_hash=(
            pub.target_publication_request_identity_hash
        )
    )
    kwargs.update(over)
    return _svc(session, mock).execute(art.id, pub.id, **kwargs)


# ==================== successful execute (§29) ==========================
def test_execute_happy_path(session: Session, wp_env) -> None:
    art, draft, pub = _ready(session)
    mock = _WpMock(art=art)
    body_before, meta_before, title_before, slug_before = (
        art.body, art.meta_description, art.title, art.slug,
    )

    out = _execute(session, art, pub, mock)

    assert out.status == "succeeded"
    assert mock.post_calls == 1
    assert mock.get_calls == 2  # preflight + read-back

    prun = session.get(WordPressPublicationRun, pub.id)
    assert prun.status == "succeeded"
    assert prun.wordpress_post_status == "publish"
    assert prun.wordpress_post_url == "https://wp.example.test/blog/?p=25"
    assert prun.published_at_source == "wordpress_date_gmt"
    assert prun.response_snapshot == {
        "id": 25, "status": "publish",
        "link": "https://wp.example.test/blog/?p=25",
        "date_gmt": "2026-09-06T13:45:12",
    }
    assert prun.started_at is not None and prun.finished_at is not None
    assert prun.error_message is None

    a = article_of(session, art.id)
    assert a.status == ArticleStatus.PUBLISHED.value
    assert a.wordpress_post_id == _WP_POST_ID
    assert a.published_url == "https://wp.example.test/blog/?p=25"
    # exact regression lock: the offset-naive WordPress date_gmt from the mock
    # ("2026-09-06T13:45:12") must land on Article.published_at with every
    # year/month/day/hour/minute/second preserved, stored per the project-wide
    # naive-UTC convention.
    assert a.published_at == datetime(2026, 9, 6, 13, 45, 12)
    assert (
        a.published_at.year,
        a.published_at.month,
        a.published_at.day,
        a.published_at.hour,
        a.published_at.minute,
        a.published_at.second,
        a.published_at.microsecond,
    ) == (2026, 9, 6, 13, 45, 12, 0)
    assert a.body == body_before and a.meta_description == meta_before
    assert a.title == title_before and a.slug == slug_before

    d = session.get(WordPressDraftRun, draft.id)
    assert d.status == WP_RUN_SUCCEEDED and d.wordpress_post_status == "draft"


def test_execute_published_at_offset_aware_date_gmt_normalized_to_utc(
    session: Session, wp_env
) -> None:
    """offset-aware date_gmt -> normalized UTC -> project-standard naive UTC storage。"""

    art, draft, pub = _ready(session)
    mock = _WpMock(art=art, readback_date_gmt="2026-09-06T13:45:12+09:00")
    _execute(session, art, pub, mock)

    prun = session.get(WordPressPublicationRun, pub.id)
    assert prun.published_at_source == "wordpress_date_gmt"
    a = article_of(session, art.id)
    # 13:45:12 +09:00 == 04:45:12 UTC, stored naive.
    assert a.published_at == datetime(2026, 9, 6, 4, 45, 12)
    assert a.published_at.tzinfo is None


def test_execute_published_at_fallback_local_when_date_gmt_absent(
    session: Session, wp_env
) -> None:
    art, draft, pub = _ready(session)
    mock = _WpMock(art=art, readback_date_gmt=None)
    _execute(session, art, pub, mock)
    prun = session.get(WordPressPublicationRun, pub.id)
    assert prun.published_at_source == "local_confirmed_success_utc"
    assert article_of(session, art.id).published_at is not None


# ==================== execute guards / preflight (§28) ==================
def test_execute_rejects_run_not_prepared(session: Session, wp_env) -> None:
    art, draft, pub = _ready(session)
    pub.status = "running"
    session.flush()
    mock = _WpMock(art=art)
    with pytest.raises(WordPressPublicationRunExecutionError):
        _execute(session, art, pub, mock)
    assert mock.post_calls == 0


def test_execute_rejects_wrong_article(session: Session, wp_env) -> None:
    art, draft, pub = _ready(session)
    mock = _WpMock(art=art)
    with pytest.raises(EntityNotFoundError):
        _svc(session, mock).execute(
            art.id + 999, pub.id,
            expected_target_publication_request_identity_hash=(
                pub.target_publication_request_identity_hash
            ),
        )
    assert mock.post_calls == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda art, draft, s: setattr(art, "status", ArticleStatus.REVIEW.value),
        lambda art, draft, s: setattr(art, "wordpress_post_id", 26),
        lambda art, draft, s: setattr(art, "published_url", "https://wp.example.test/x"),
        lambda art, draft, s: setattr(art, "published_at", datetime.now(UTC)),
        lambda art, draft, s: setattr(art, "body", art.body + "\n\ndrift"),
        lambda art, draft, s: setattr(art, "meta_description", art.meta_description + "x"),
        lambda art, draft, s: setattr(draft, "status", "failed"),
        lambda art, draft, s: setattr(draft, "wordpress_post_status", "publish"),
        lambda art, draft, s: setattr(draft, "wordpress_post_id", "26"),
        lambda art, draft, s: setattr(draft, "error_message", "boom"),
    ],
)
def test_execute_guard_rejections(session: Session, wp_env, mutate) -> None:
    art, draft, pub = _ready(session)
    mutate(art, draft, session)
    session.flush()
    mock = _WpMock(art=art)
    with pytest.raises(WordPressPublicationRunExecutionError):
        _execute(session, art, pub, mock)
    assert mock.post_calls == 0
    assert session.get(WordPressPublicationRun, pub.id).status == "prepared"


def test_execute_rejects_expected_identity_mismatch(session: Session, wp_env) -> None:
    art, draft, pub = _ready(session)
    mock = _WpMock(art=art)
    with pytest.raises(WordPressPublicationRunExecutionError):
        _execute(
            session, art, pub, mock,
            expected_target_publication_request_identity_hash="0" * 64,
        )
    assert mock.post_calls == 0


def test_execute_rejects_publish_payload_hash_drift(session: Session, wp_env) -> None:
    art, draft, pub = _ready(session)
    pub.publish_payload_hash = "0" * 64
    session.flush()
    mock = _WpMock(art=art)
    with pytest.raises(WordPressPublicationRunExecutionError):
        _execute(session, art, pub, mock)
    assert mock.post_calls == 0


def test_execute_preflight_already_published_stops(session: Session, wp_env) -> None:
    art, draft, pub = _ready(session)
    mock = _WpMock(art=art, preflight_status="publish")
    with pytest.raises(WordPressPublicationPreflightError):
        _execute(session, art, pub, mock)
    assert mock.post_calls == 0
    assert session.get(WordPressPublicationRun, pub.id).status == "prepared"
    assert article_of(session, art.id).status == ArticleStatus.APPROVED.value


def test_execute_preflight_raw_hash_drift_stops(session: Session, wp_env) -> None:
    art, draft, pub = _ready(session)
    mock = _WpMock(art=art, raw_content="<p>SOMETHING ELSE ENTIRELY</p>")
    with pytest.raises(WordPressPublicationPreflightError):
        _execute(session, art, pub, mock)
    assert mock.post_calls == 0
    assert session.get(WordPressPublicationRun, pub.id).status == "prepared"


def test_execute_preflight_no_category_stops(session: Session, wp_env) -> None:
    art, draft, pub = _ready(session)
    mock = _WpMock(art=art, preflight_categories=())
    with pytest.raises(WordPressPublicationPreflightError):
        _execute(session, art, pub, mock)
    assert mock.post_calls == 0


# ==================== failure taxonomy (§30) ===========================
def test_taxonomy_A_publish_timeout_is_ambiguous(session: Session, wp_env) -> None:
    art, draft, pub = _ready(session)
    mock = _WpMock(art=art, publish_exc=httpx.TimeoutException("timeout"))
    with pytest.raises(WordPressAmbiguousPublishOutcomeError):
        _execute(session, art, pub, mock)
    assert mock.post_calls == 1  # exactly one attempt, no retry

    prun = session.get(WordPressPublicationRun, pub.id)
    assert prun.status == "failed"
    assert prun.error_message.startswith("ambiguous_wordpress_publish_outcome:")
    assert prun.finished_at is not None
    a = article_of(session, art.id)
    assert a.status == ArticleStatus.APPROVED.value
    assert a.published_url is None and a.published_at is None


def test_taxonomy_A_publish_connection_loss_is_ambiguous(session: Session, wp_env) -> None:
    art, draft, pub = _ready(session)
    mock = _WpMock(art=art, publish_exc=httpx.ConnectError("drop"))
    with pytest.raises(WordPressAmbiguousPublishOutcomeError):
        _execute(session, art, pub, mock)
    assert mock.post_calls == 1
    assert session.get(WordPressPublicationRun, pub.id).status == "failed"


def test_definite_publish_500_marks_failed_no_retry(session: Session, wp_env) -> None:
    art, draft, pub = _ready(session)
    mock = _WpMock(art=art, publish_http=500)
    with pytest.raises(ExternalProviderError):
        _execute(session, art, pub, mock)
    assert mock.post_calls == 1
    prun = session.get(WordPressPublicationRun, pub.id)
    assert prun.status == "failed"
    a = article_of(session, art.id)
    assert a.status == ArticleStatus.APPROVED.value


def test_definite_publish_returns_non_publish_status_marks_failed(
    session: Session, wp_env
) -> None:
    art, draft, pub = _ready(session)
    mock = _WpMock(art=art, publish_resp_status="draft")
    with pytest.raises(ExternalProviderError):
        _execute(session, art, pub, mock)
    assert mock.post_calls == 1
    assert session.get(WordPressPublicationRun, pub.id).status == "failed"
    assert article_of(session, art.id).status == ArticleStatus.APPROVED.value


def test_taxonomy_B_readback_fails_run_stays_running(session: Session, wp_env) -> None:
    art, draft, pub = _ready(session)
    mock = _WpMock(art=art, readback_exc=httpx.TimeoutException("rb timeout"))
    with pytest.raises(WordPressPublicationReadbackFailedError):
        _execute(session, art, pub, mock)
    assert mock.post_calls == 1  # publish happened once
    assert mock.get_calls == 2   # preflight + attempted read-back

    prun = session.get(WordPressPublicationRun, pub.id)
    assert prun.status == "running"  # durable state stays running
    a = article_of(session, art.id)
    assert a.status == ArticleStatus.APPROVED.value
    assert a.published_url is None and a.published_at is None


def test_taxonomy_B_readback_wrong_status_run_stays_running(
    session: Session, wp_env
) -> None:
    art, draft, pub = _ready(session)
    mock = _WpMock(art=art, readback_status="draft")
    with pytest.raises(WordPressPublicationReadbackFailedError):
        _execute(session, art, pub, mock)
    assert mock.post_calls == 1
    assert session.get(WordPressPublicationRun, pub.id).status == "running"
    assert article_of(session, art.id).status == ArticleStatus.APPROVED.value


def test_taxonomy_C_local_commit_failure_run_stays_running(
    session: Session, wp_env, monkeypatch
) -> None:
    art, draft, pub = _ready(session)
    mock = _WpMock(art=art)

    real_commit = session.commit
    calls = {"n": 0}

    def flaky_commit():
        calls["n"] += 1
        if calls["n"] == 1:  # prepared -> running commit succeeds
            return real_commit()
        raise RuntimeError("simulated local finalization failure")

    monkeypatch.setattr(session, "commit", flaky_commit)

    with pytest.raises(WordPressPublicationExternalSuccessLocalPersistFailedError) as e:
        _execute(session, art, pub, mock)
    assert "25" in str(e.value)
    assert mock.post_calls == 1  # NEVER a second publish POST

    monkeypatch.undo()
    prun = session.get(WordPressPublicationRun, pub.id)
    assert prun.status == "running"  # rolled back to last real commit
    a = article_of(session, art.id)
    assert a.status == ArticleStatus.APPROVED.value
    assert a.published_url is None and a.published_at is None


def test_execute_rejects_already_running_recovery_required(session: Session, wp_env) -> None:
    art, draft, pub = _ready(session)
    pub.status = "running"
    pub.started_at = datetime.now(UTC)
    session.flush()
    mock = _WpMock(art=art)
    with pytest.raises(WordPressPublicationRunExecutionError):
        _execute(session, art, pub, mock)
    assert mock.post_calls == 0


def test_execute_rejects_already_succeeded(session: Session, wp_env) -> None:
    art, draft, pub = _ready(session)
    pub.status = "succeeded"
    session.flush()
    mock = _WpMock(art=art)
    with pytest.raises(WordPressPublicationRunExecutionError):
        _execute(session, art, pub, mock)
    assert mock.post_calls == 0


# ==================== API level ========================================
def test_api_execute_rejects_unexpected_fields(api_client, session: Session, wp_env) -> None:
    art, draft, pub = _ready(session)
    resp = api_client.post(
        f"/api/v1/articles/{art.id}/wordpress-publication-runs/{pub.id}/execute",
        json={
            "expected_target_publication_request_identity_hash": (
                pub.target_publication_request_identity_hash
            ),
            "status": "publish",
        },
    )
    assert resp.status_code == 422
