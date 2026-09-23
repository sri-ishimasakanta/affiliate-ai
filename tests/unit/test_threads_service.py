"""ThreadsService の境界 (T1、Meta には一切接続しない)。

pin する契約:

- **既定は PLAN。** ``execute=True`` を明示しない限り外部に書かない。
- 承認の痕跡なしに公開しない (承認と公開は別物)。
- 設定が無ければ、外部に触れる前に止まる。
- 公式に存在しない指標を問い合わせない。欠測を 0 で埋めない。
- token は状況出力のどこにも現れない。
- T2 の文体要件と T3 の計測項目が記録されている。
"""

from __future__ import annotations

import pytest

from app.social.threads.errors import ThreadsAuthError, ThreadsNotConfiguredError
from app.social.threads.models import (
    DAILY_PUBLISH_LIMIT,
    MEDIA_METRICS,
    RECOMMENDED_PUBLISH_DELAY_SECONDS,
    TEXT_MAX_LENGTH,
    UNVERIFIED_FACTS,
    USER_METRICS,
    ThreadsContainer,
    ThreadsProfile,
    ThreadsPublication,
)
from app.social.threads.service import ThreadsService

_TOKEN = "THAAAsecret-long-lived-token-value-do-not-leak"
_APPROVED = "a" * 64


class _Settings:
    threads_enabled = True
    threads_user_id = "9876543210"
    threads_access_token = _TOKEN
    threads_api_base_url = "https://graph.threads.net"
    threads_api_version = "v1.0"


def _settings(**overrides):
    return type("S", (_Settings,), overrides)()


class _FakeClient:
    """呼ばれたことだけを記録する。HTTP には触れない。"""

    def __init__(self, *, profile=None, insights=None, raises=None) -> None:
        self.calls: list[str] = []
        self._profile = profile or ThreadsProfile("9876543210", "bizfluxlab")
        self._insights = insights or {"data": []}
        self._raises = raises

    def fetch_profile(self):
        self.calls.append("fetch_profile")
        if self._raises:
            raise self._raises
        return self._profile

    def create_text_container(self, text):
        self.calls.append("create_text_container")
        if self._raises:
            raise self._raises
        return ThreadsContainer("container-1")

    def publish_container(self, creation_id):
        self.calls.append("publish_container")
        return ThreadsPublication("media-1")

    def fetch_media_insights(self, media_id, metrics):
        self.calls.append(f"fetch_media_insights:{','.join(metrics)}")
        return self._insights

    def fetch_user_insights(self, metrics, *, since=None, until=None):
        self.calls.append(f"fetch_user_insights:{','.join(metrics)}")
        return self._insights

    def fetch_media(self, media_id, **kwargs):
        self.calls.append("fetch_media")
        return {"id": media_id, "permalink": "https://www.threads.net/p/x"}


def _service(client=None, sleeps=None, **overrides) -> ThreadsService:
    return ThreadsService(
        _settings(**overrides),
        client=client or _FakeClient(),
        sleep=(sleeps.append if sleeps is not None else (lambda _s: None)),
    )


# -- configuration --------------------------------------------------------------
def test_describe_never_reveals_the_token() -> None:
    status = _service().describe()
    payload = status.as_dict()

    assert payload["threads_access_token_configured"] is True
    assert _TOKEN not in repr(payload)
    # 先頭数文字も出さない。
    assert _TOKEN[:6] not in repr(payload)


def test_describe_lists_what_is_missing() -> None:
    status = _service(threads_enabled=False, threads_user_id=None, threads_access_token=None)
    payload = status.describe().as_dict()

    assert payload["threads_configured"] is False
    assert any("THREADS_ENABLED" in n for n in payload["notes"])
    assert any("THREADS_USER_ID" in n for n in payload["notes"])
    assert any("THREADS_ACCESS_TOKEN" in n for n in payload["notes"])


def test_check_connection_skips_the_api_when_unconfigured() -> None:
    client = _FakeClient()
    status = ThreadsService(_settings(threads_enabled=False), client=client).check_connection()

    assert status.reachable is None
    assert client.calls == []


def test_check_connection_reads_only_the_profile() -> None:
    client = _FakeClient()
    status = _service(client).check_connection()

    assert status.reachable is True
    assert status.profile == {"user_id": "9876543210", "username": "bizfluxlab"}
    assert client.calls == ["fetch_profile"]


def test_check_connection_reports_an_auth_failure_safely() -> None:
    client = _FakeClient(raises=ThreadsAuthError(f"bad access_token={_TOKEN}", status=401))
    status = _service(client).check_connection()

    assert status.reachable is False
    assert status.error["category"] == "threads_auth"
    assert _TOKEN not in repr(status.as_dict())


# -- publishing is PLAN by default ---------------------------------------------
def test_publish_is_plan_by_default_and_touches_nothing() -> None:
    client = _FakeClient()

    outcome = _service(client).publish_text("短いテスト投稿")

    assert outcome.executed is False
    assert outcome.outcome == "planned"
    assert client.calls == []


def test_publishing_requires_an_approved_proposal_hash() -> None:
    """承認は公開ではない。承認の痕跡なしに外へ出さない。"""

    client = _FakeClient()

    outcome = _service(client).publish_text("テスト", execute=True)

    assert outcome.outcome == "blocked"
    assert any("human-approved proposal" in r for r in outcome.blocked_reasons)
    assert client.calls == []


def test_publishing_is_blocked_when_threads_is_disabled() -> None:
    client = _FakeClient()

    outcome = ThreadsService(_settings(threads_enabled=False), client=client).publish_text(
        "テスト", execute=True, approved_proposal_hash=_APPROVED
    )

    assert outcome.outcome == "blocked"
    assert any("THREADS_ENABLED" in r for r in outcome.blocked_reasons)
    assert client.calls == []


@pytest.mark.parametrize("text", ["", "x" * (TEXT_MAX_LENGTH + 1)])
def test_text_outside_the_documented_limit_is_blocked(text) -> None:
    """公式: テキスト投稿は 500 文字まで。"""

    client = _FakeClient()

    outcome = _service(client).publish_text(text, execute=True, approved_proposal_hash=_APPROVED)

    assert outcome.outcome == "blocked"
    assert client.calls == []


def test_an_approved_publish_follows_the_documented_two_step_flow() -> None:
    client = _FakeClient()
    sleeps: list[float] = []

    outcome = _service(client, sleeps=sleeps).publish_text(
        "承認済みのテキスト", execute=True, approved_proposal_hash=_APPROVED
    )

    assert outcome.outcome == "published"
    assert outcome.publication["media_id"] == "media-1"
    assert client.calls == ["create_text_container", "publish_container"]
    # 公式が推奨する待ち時間を守る。
    assert sleeps == [RECOMMENDED_PUBLISH_DELAY_SECONDS]


def test_a_publish_failure_is_reported_without_the_token() -> None:
    client = _FakeClient(raises=ThreadsAuthError(f"token access_token={_TOKEN}", status=401))

    outcome = _service(client).publish_text(
        "テキスト", execute=True, approved_proposal_hash=_APPROVED
    )

    assert outcome.outcome == "failed"
    assert outcome.error["category"] == "threads_auth"
    assert _TOKEN not in repr(outcome.as_dict())


# -- insights -------------------------------------------------------------------
def test_only_officially_documented_metrics_are_requested() -> None:
    client = _FakeClient()

    _service(client).media_insights("media-1", ("views", "likes", "invented_metric"))

    assert client.calls == ["fetch_media_insights:views,likes"]


def test_an_entirely_unsupported_metric_set_is_refused() -> None:
    client = _FakeClient()

    with pytest.raises(ThreadsNotConfiguredError, match="no supported media metric"):
        _service(client).media_insights("media-1", ("made_up",))

    assert client.calls == []


def test_follower_demographics_cannot_be_combined_with_a_time_range() -> None:
    """公式の注記どおり。黙って落とさず、呼び出し側に選ばせる。"""

    client = _FakeClient()

    with pytest.raises(ThreadsNotConfiguredError, match="follower_demographics"):
        _service(client).user_insights(("views", "follower_demographics"), since=1, until=2)

    assert client.calls == []


def test_missing_metrics_stay_missing_instead_of_zero() -> None:
    client = _FakeClient(insights={"data": [{"name": "views", "values": [{"value": 12}]}]})

    insights = _service(client).media_insights("media-1", ("views", "likes"))

    assert insights.values == {"views": 12}
    assert insights.missing == ("likes",)
    assert "likes" not in insights.values


# -- recorded facts -------------------------------------------------------------
def test_the_documented_api_facts_are_recorded() -> None:
    assert TEXT_MAX_LENGTH == 500
    assert RECOMMENDED_PUBLISH_DELAY_SECONDS == 30
    assert DAILY_PUBLISH_LIMIT == 250
    assert MEDIA_METRICS == ("views", "likes", "replies", "reposts", "quotes", "shares")
    assert "followers_count" in USER_METRICS
    assert "clicks" in USER_METRICS
    # 未確認のことは未確認として残す。
    assert UNVERIFIED_FACTS


def test_the_threads_writing_style_requirements_are_recorded_for_t2() -> None:
    from app.social.threads.style import (
        FACT_RULES,
        POST_ANGLES,
        STYLE_PROHIBITIONS,
        STYLE_REQUIREMENTS,
    )

    assert "口語寄りにする (硬い書き言葉のままにしない)" in STYLE_REQUIREMENTS
    assert any("短い文" in r for r in STYLE_REQUIREMENTS)
    assert any("単体で読んで役に立つ" in r for r in STYLE_REQUIREMENTS)
    for forbidden in ("作り話の実体験", "過剰な絵文字", "押しの強いアフィリエイト口調"):
        assert forbidden in STYLE_PROHIBITIONS
    assert set(POST_ANGLES) >= {"insight", "common_mistake", "comparison", "question"}
    assert FACT_RULES


def test_t1_does_not_activate_threads_approval_yet() -> None:
    """C8.8 の封筒は threads_post を表現できるが、まだ有効にしない。"""

    from app.models import SUBJECT_THREADS_POST, SUBJECT_TYPES, SUBJECT_TYPES_SUPPORTED_IN_V1

    assert SUBJECT_THREADS_POST in SUBJECT_TYPES
    assert SUBJECT_THREADS_POST not in SUBJECT_TYPES_SUPPORTED_IN_V1


def test_approval_is_not_publishing() -> None:
    """T2/T3 が守る順序を明文として固定する。"""

    from app.social.threads.service import PUBLISH_REQUIRES

    joined = " ".join(PUBLISH_REQUIRES)
    assert "human-approved" in joined
    assert "is not itself publishing" in joined
    assert "explicit execute intent" in joined


# -- T3 attribution (defined now, used later) -----------------------------------
def test_threads_attribution_is_deterministic() -> None:
    from app.social.threads.attribution import UTM_MEDIUM, UTM_SOURCE, decorate

    url = "https://bizfluxlab.com/generative-ai-guidelines/"
    first = decorate(url, article_id=21, angle="insight", proposal_hash="b" * 64)
    second = decorate(url, article_id=21, angle="insight", proposal_hash="b" * 64)

    assert first == second
    assert f"utm_source={UTM_SOURCE}" in first
    assert f"utm_medium={UTM_MEDIUM}" in first
    assert "utm_campaign=article-21-insight" in first
    assert "utm_content=bbbbbbbbbbbbbbbb" in first


def test_attribution_preserves_existing_query_parameters() -> None:
    from app.social.threads.attribution import decorate

    out = decorate(
        "https://bizfluxlab.com/x/?ref=abc", article_id=1, angle="question", proposal_hash="c" * 64
    )

    assert "ref=abc" in out
    assert "utm_campaign=article-1-question" in out


def test_different_angles_produce_different_campaigns() -> None:
    from app.social.threads.attribution import build_campaign

    assert build_campaign(article_id=21, angle="insight") != build_campaign(
        article_id=21, angle="comparison"
    )
