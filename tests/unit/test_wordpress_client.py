"""WordPressClient: 認証済み read-only probe の transport-mocked テスト。

実際の WordPress へは一切通信しない (httpx.MockTransport を使う)。
"""

from __future__ import annotations

import httpx
import pytest

from app.config.settings import Settings
from app.exceptions import ExternalProviderError, ProviderNotConfiguredError, WordPressTargetError
from app.wordpress.client import WordPressClient

_USERNAME = "wp-user-secret"
_APP_PASSWORD = "aaaa bbbb cccc dddd"


def _settings(**overrides) -> Settings:
    base = {
        "_env_file": None,
        "wordpress_base_url": "https://wp.example.test",
        "wordpress_username": _USERNAME,
        "wordpress_app_password": _APP_PASSWORD,
    }
    base.update(overrides)
    return Settings(**base)


def _client(handler, **settings_overrides) -> WordPressClient:
    transport = httpx.MockTransport(handler)
    return WordPressClient(_settings(**settings_overrides), transport=transport)


# -- happy path -----------------------------------------------------
def test_probe_current_user_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/wp-json/wp/v2/users/me"
        assert request.url.params.get("context") == "edit"
        return httpx.Response(
            200,
            json={
                "id": 7,
                "roles": ["administrator"],
                "capabilities": {"edit_posts": True, "publish_posts": True},
            },
        )

    result = _client(handler).probe_current_user()
    assert result.authenticated is True
    assert result.user_id == 7
    assert result.roles == ["administrator"]
    assert result.capabilities_present is True
    assert result.draft_create_capability == "verified"
    assert result.target_base_url == "https://wp.example.test"
    assert result.http_status == 200


def test_probe_current_user_no_capabilities_returned_is_unverified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 3, "roles": ["editor"]})

    result = _client(handler).probe_current_user()
    assert result.authenticated is True
    assert result.capabilities_present is False
    assert result.draft_create_capability == "unverified"


# -- error status codes ----------------------------------------------
def test_probe_401_raises_sanitized_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": "rest_forbidden"})

    with pytest.raises(ExternalProviderError) as exc_info:
        _client(handler).probe_current_user()
    assert "401" in str(exc_info.value)
    assert _USERNAME not in str(exc_info.value)
    assert _APP_PASSWORD not in str(exc_info.value)


def test_probe_403_raises_sanitized_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"code": "rest_cannot_view"})

    with pytest.raises(ExternalProviderError) as exc_info:
        _client(handler).probe_current_user()
    assert "403" in str(exc_info.value)
    assert _USERNAME not in str(exc_info.value)
    assert _APP_PASSWORD not in str(exc_info.value)


def test_probe_unexpected_status_raises_sanitized_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal server error")

    with pytest.raises(ExternalProviderError) as exc_info:
        _client(handler).probe_current_user()
    assert "500" in str(exc_info.value)


# -- transport-level failures -----------------------------------------
def test_probe_timeout_raises_sanitized_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated timeout")

    with pytest.raises(ExternalProviderError) as exc_info:
        _client(handler).probe_current_user()
    assert "timed out" in str(exc_info.value)
    assert _APP_PASSWORD not in str(exc_info.value)


def test_probe_connection_error_raises_sanitized_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated TLS/connection failure")

    with pytest.raises(ExternalProviderError) as exc_info:
        _client(handler).probe_current_user()
    assert "connection failed" in str(exc_info.value)
    assert _APP_PASSWORD not in str(exc_info.value)
    assert "simulated TLS/connection failure" not in str(exc_info.value)


# -- redirect safety ---------------------------------------------------
def test_probe_blocks_different_origin_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"location": "https://attacker.example/steal"}
        )

    with pytest.raises(ExternalProviderError) as exc_info:
        _client(handler).probe_current_user()
    msg = str(exc_info.value)
    assert "redirect" in msg
    assert "attacker.example" in msg  # origin only, safe to report
    assert _USERNAME not in msg
    assert _APP_PASSWORD not in msg


def test_probe_blocks_same_origin_redirect_too() -> None:
    # narrow scope: this phase does not implement same-origin auto-follow.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            301, headers={"location": "https://wp.example.test/wp-json/wp/v2/users/me/"}
        )

    with pytest.raises(ExternalProviderError):
        _client(handler).probe_current_user()


# -- secret hygiene ------------------------------------------------
def test_credentials_not_exposed_in_successful_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 1, "roles": ["administrator"]})

    result = _client(handler).probe_current_user()
    blob = result.model_dump_json()
    assert _USERNAME not in blob
    assert _APP_PASSWORD not in blob
    assert "authorization" not in blob.lower()


def test_credentials_never_sent_in_query_or_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert _USERNAME not in str(request.url)
        assert _APP_PASSWORD not in str(request.url)
        # credentials must travel only via the Authorization header, set by httpx.BasicAuth
        assert "authorization" in {k.lower() for k in request.headers.keys()}
        return httpx.Response(200, json={"id": 1, "roles": []})

    _client(handler).probe_current_user()


# -- configuration / target guards -------------------------------------
def test_missing_credentials_raises_not_configured() -> None:
    with pytest.raises(ProviderNotConfiguredError):
        WordPressClient(_settings(wordpress_username=None))


def test_non_https_target_rejected_before_any_network_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not be called for a rejected http target")

    with pytest.raises(WordPressTargetError):
        _client(handler, wordpress_base_url="http://wp.example.test")


# -- API surface: read-only only ----------------------------------------
def test_client_exposes_no_write_operations() -> None:
    public_attrs = {name for name in dir(WordPressClient) if not name.startswith("_")}
    assert public_attrs == {"probe_current_user", "target_base_url"}
    forbidden_names = (
        "create_post", "update_post", "delete_post",
        "publish", "post", "put", "patch", "delete",
    )
    for forbidden in forbidden_names:
        assert forbidden not in public_attrs
