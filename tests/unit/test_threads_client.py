"""Threads API client の振る舞い (T1、Meta には一切接続しない)。

pin する契約:

- 未設定なら **外部に触れる前に** 失敗する。
- access token は URL / 例外 / 診断のどこにも現れない。
- HTTP の失敗は、呼び出し側が判断できる分類へ落ちる。
- 応答が想定の形でなければ、推測で埋めずに失敗する。
- タイムアウトと TLS 検証は無効化できない。

エンドポイントとパラメータ名は 2026-09-24 時点の公式ドキュメントで確認したもの。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.social.threads.client import DEFAULT_TIMEOUT_SECONDS, ThreadsClient
from app.social.threads.errors import (
    ThreadsAuthError,
    ThreadsNotConfiguredError,
    ThreadsPermissionError,
    ThreadsRateLimitError,
    ThreadsResponseError,
    ThreadsServerError,
    ThreadsTimeoutError,
    redact,
)

_TOKEN = "THAAAsecret-long-lived-token-value-do-not-leak"


class _Settings:
    threads_enabled = True
    threads_user_id = "9876543210"
    threads_access_token = _TOKEN
    threads_api_base_url = "https://graph.threads.net"
    threads_api_version = "v1.0"


def _settings(**overrides):
    return type("S", (_Settings,), overrides)()


class _FakeHttp:
    """1 度の応答を返すだけ。ネットワークには出ない。"""

    def __init__(self, status=200, payload=None, raw=None, raises=None) -> None:
        self.status = status
        self.payload = payload if payload is not None else {}
        self.raw = raw
        self.raises = raises
        self.calls: list[dict] = []

    def request(self, method, url, *, params=None, data=None):
        self.calls.append({"method": method, "url": url, "params": params, "data": data})
        if self.raises is not None:
            raise self.raises
        body = self.raw if self.raw is not None else json.dumps(self.payload).encode()
        return httpx.Response(self.status, content=body)

    def close(self) -> None:
        return None


def _client(http, **overrides) -> ThreadsClient:
    return ThreadsClient(_settings(**overrides), http_client=http)


# -- configuration --------------------------------------------------------------
def test_a_missing_token_fails_before_any_request() -> None:
    http = _FakeHttp()
    client = _client(http, threads_access_token=None)

    with pytest.raises(ThreadsNotConfiguredError, match="THREADS_ACCESS_TOKEN"):
        client.fetch_profile()

    assert http.calls == []


def test_a_missing_user_id_fails_before_any_request() -> None:
    http = _FakeHttp()
    client = _client(http, threads_user_id=None)

    with pytest.raises(ThreadsNotConfiguredError, match="THREADS_USER_ID"):
        client.fetch_profile()

    assert http.calls == []


def test_a_non_https_base_url_is_refused() -> None:
    http = _FakeHttp()
    client = _client(http, threads_api_base_url="http://graph.threads.net")

    with pytest.raises(ThreadsNotConfiguredError, match="https"):
        client.fetch_profile()

    assert http.calls == []


def test_the_base_url_carries_the_configured_version() -> None:
    http = _FakeHttp(payload={"id": "1", "username": "bizfluxlab"})
    _client(http).fetch_profile()

    assert http.calls[0]["url"].startswith("https://graph.threads.net/v1.0/9876543210")


# -- documented endpoints -------------------------------------------------------
def test_profile_uses_the_documented_fields() -> None:
    http = _FakeHttp(payload={"id": "9876543210", "username": "bizfluxlab"})

    profile = _client(http).fetch_profile()

    assert profile.user_id == "9876543210"
    assert profile.username == "bizfluxlab"
    assert http.calls[0]["params"]["fields"] == "id,username"


def test_creating_a_text_container_uses_the_documented_parameters() -> None:
    """公式: POST /{user-id}/threads with media_type=TEXT, text."""

    http = _FakeHttp(payload={"id": "container-1"})

    container = _client(http).create_text_container("こんにちは")

    call = http.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/9876543210/threads")
    assert call["data"]["media_type"] == "TEXT"
    assert call["data"]["text"] == "こんにちは"
    assert container.creation_id == "container-1"


def test_publishing_uses_the_documented_parameters() -> None:
    """公式: POST /{user-id}/threads_publish with creation_id."""

    http = _FakeHttp(payload={"id": "media-1"})

    publication = _client(http).publish_container("container-1")

    call = http.calls[0]
    assert call["url"].endswith("/9876543210/threads_publish")
    assert call["data"]["creation_id"] == "container-1"
    assert publication.media_id == "media-1"


def test_media_insights_uses_the_documented_endpoint() -> None:
    http = _FakeHttp(payload={"data": []})

    _client(http).fetch_media_insights("media-1", ("views", "likes"))

    assert http.calls[0]["url"].endswith("/media-1/insights")
    assert http.calls[0]["params"]["metric"] == "views,likes"


def test_user_insights_supports_the_documented_time_range() -> None:
    http = _FakeHttp(payload={"data": []})

    _client(http).fetch_user_insights(("views",), since=1000, until=2000)

    call = http.calls[0]
    assert call["url"].endswith("/9876543210/threads_insights")
    assert call["params"]["since"] == 1000
    assert call["params"]["until"] == 2000


# -- failures -------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        (401, {"code": 190, "message": "Invalid OAuth access token"}, ThreadsAuthError),
        (403, {"code": 200, "message": "Permissions error"}, ThreadsPermissionError),
        (429, {"code": 4, "message": "rate limited"}, ThreadsRateLimitError),
        (500, {"code": 1, "message": "server"}, ThreadsServerError),
        (400, {"code": 100, "message": "bad request"}, ThreadsResponseError),
        (400, {"code": 190, "error_subcode": 463, "message": "expired"}, ThreadsAuthError),
    ],
)
def test_api_failures_map_to_actionable_categories(status, error, expected) -> None:
    http = _FakeHttp(status=status, payload={"error": error})

    with pytest.raises(expected):
        _client(http).fetch_profile()


def test_a_timeout_is_reported_as_a_timeout() -> None:
    http = _FakeHttp(raises=httpx.ReadTimeout("slow"))

    with pytest.raises(ThreadsTimeoutError):
        _client(http).fetch_profile()


def test_a_malformed_response_is_refused() -> None:
    http = _FakeHttp(raw=b"<html>not json</html>")

    with pytest.raises(ThreadsResponseError, match="not JSON"):
        _client(http).fetch_profile()


def test_a_response_without_an_id_is_refused() -> None:
    """推測で埋めない。"""

    http = _FakeHttp(payload={"username": "bizfluxlab"})

    with pytest.raises(ThreadsResponseError, match="no id"):
        _client(http).fetch_profile()

    http = _FakeHttp(payload={})
    with pytest.raises(ThreadsResponseError, match="no id"):
        _client(http).create_text_container("x")


def test_an_oversized_response_is_refused() -> None:
    http = _FakeHttp(raw=b"x" * (600 * 1024))

    with pytest.raises(ThreadsResponseError, match="too large"):
        _client(http).fetch_profile()


# -- the token never leaks ------------------------------------------------------
def test_the_token_never_appears_in_an_error() -> None:
    http = _FakeHttp(
        status=401,
        payload={"error": {"code": 190, "message": f"Invalid token access_token={_TOKEN}"}},
    )

    with pytest.raises(ThreadsAuthError) as excinfo:
        _client(http).fetch_profile()

    assert _TOKEN not in str(excinfo.value)
    assert _TOKEN not in repr(excinfo.value.as_dict())
    assert "[redacted]" in excinfo.value.reason


def test_the_token_never_appears_in_a_transport_error() -> None:
    http = _FakeHttp(raises=RuntimeError(f"failed with access_token={_TOKEN}"))

    with pytest.raises(ThreadsResponseError) as excinfo:
        _client(http).fetch_profile()

    assert _TOKEN not in str(excinfo.value)


def test_the_redactor_covers_credential_shapes() -> None:
    for text in (
        f"https://graph.threads.net/v1.0/me?access_token={_TOKEN}",
        f'{{"access_token":"{_TOKEN}"}}',
        f"Authorization: Bearer {_TOKEN}",
        f"client_secret={_TOKEN}&x=1",
        f"refresh_token={_TOKEN}",
    ):
        cleaned = redact(text)
        assert _TOKEN not in cleaned, text
        assert "[redacted]" in cleaned


def test_the_client_never_disables_tls_or_the_timeout() -> None:
    import inspect

    from app.social.threads import client as client_mod

    source = inspect.getsource(client_mod)

    assert "verify=False" not in source
    assert "verify = False" not in source
    assert DEFAULT_TIMEOUT_SECONDS > 0
    assert "timeout=DEFAULT_TIMEOUT_SECONDS" in source
