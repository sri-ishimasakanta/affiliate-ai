"""WordPressClient: 認証済み read/write の transport-mocked テスト。

実際の WordPress へは一切通信しない (httpx.MockTransport を使う)。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.config.settings import Settings
from app.exceptions import (
    ExternalProviderError,
    ProviderNotConfiguredError,
    WordPressAmbiguousOutcomeError,
    WordPressTargetError,
)
from app.wordpress.client import WordPressClient

_DRAFT_PAYLOAD_JSON = json.dumps(
    {
        "title": "t", "content": "<p>c</p>", "excerpt": "e",
        "slug": "exact-slug", "status": "draft",
    },
    sort_keys=True, ensure_ascii=False, separators=(",", ":"),
)

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


# -- API surface: only the one approved write operation ------------------
def test_client_exposes_only_approved_operations() -> None:
    public_attrs = {name for name in dir(WordPressClient) if not name.startswith("_")}
    assert public_attrs == {
        "probe_current_user",
        "find_draft_posts_by_slug",
        "get_post",
        "create_draft_post_exact",
        "target_base_url",
    }
    forbidden_names = (
        "update_post", "delete_post", "publish_post",
        "bulk_create", "upload_media", "publish", "put", "patch", "delete",
    )
    for forbidden in forbidden_names:
        assert forbidden not in public_attrs


# ==================== create_draft_post_exact ============================
def test_create_draft_post_exact_sends_exact_bytes_and_content_type() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = request.content
        seen["headers"] = {k.lower() for k in request.headers.keys()}
        return httpx.Response(
            201,
            json={
                "id": 42,
                "status": "draft",
                "slug": "exact-slug",
                "link": "https://wp.example.test/?p=42",
            },
        )

    result = _client(handler).create_draft_post_exact(_DRAFT_PAYLOAD_JSON)
    assert seen["path"] == "/wp-json/wp/v2/posts"
    assert seen["content_type"] == "application/json; charset=utf-8"
    # exact frozen bytes, no re-serialization / pretty-printing / newline addition
    assert seen["body"] == _DRAFT_PAYLOAD_JSON.encode("utf-8")
    assert "authorization" in seen["headers"]
    assert result.id == 42
    assert result.status == "draft"
    assert result.slug == "exact-slug"
    assert result.link == "https://wp.example.test/?p=42"


def test_create_draft_post_exact_rejects_non_draft_payload() -> None:
    non_draft = json.dumps({"title": "t", "content": "c", "excerpt": "e",
                             "slug": "s", "status": "publish"})

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not be called for a non-draft payload")

    with pytest.raises(ValueError):
        _client(handler).create_draft_post_exact(non_draft)


def test_create_draft_post_401_raises_sanitized_error_and_no_credentials() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": "rest_forbidden"})

    with pytest.raises(ExternalProviderError) as exc_info:
        _client(handler).create_draft_post_exact(_DRAFT_PAYLOAD_JSON)
    assert "401" in str(exc_info.value)
    assert _USERNAME not in str(exc_info.value)
    assert _APP_PASSWORD not in str(exc_info.value)


def test_create_draft_post_403_raises_sanitized_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"code": "rest_cannot_create"})

    with pytest.raises(ExternalProviderError) as exc_info:
        _client(handler).create_draft_post_exact(_DRAFT_PAYLOAD_JSON)
    assert "403" in str(exc_info.value)


def test_create_draft_post_500_raises_sanitized_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal server error")

    with pytest.raises(ExternalProviderError) as exc_info:
        _client(handler).create_draft_post_exact(_DRAFT_PAYLOAD_JSON)
    assert "500" in str(exc_info.value)


def test_create_draft_post_blocks_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://attacker.example/x"})

    with pytest.raises(ExternalProviderError) as exc_info:
        _client(handler).create_draft_post_exact(_DRAFT_PAYLOAD_JSON)
    assert "redirect" in str(exc_info.value)


def test_create_draft_post_timeout_is_ambiguous_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.TimeoutException("simulated timeout")

    with pytest.raises(WordPressAmbiguousOutcomeError) as exc_info:
        _client(handler).create_draft_post_exact(_DRAFT_PAYLOAD_JSON)
    assert calls["n"] == 1  # exactly one attempt, no automatic retry
    assert "ambiguous_wordpress_outcome" in str(exc_info.value)
    assert _APP_PASSWORD not in str(exc_info.value)


def test_create_draft_post_connection_error_is_ambiguous_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("simulated connection drop")

    with pytest.raises(WordPressAmbiguousOutcomeError):
        _client(handler).create_draft_post_exact(_DRAFT_PAYLOAD_JSON)
    assert calls["n"] == 1


def test_create_draft_post_unexpected_status_field_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201, json={"id": 42, "status": "publish", "slug": "exact-slug"}
        )

    with pytest.raises(ExternalProviderError) as exc_info:
        _client(handler).create_draft_post_exact(_DRAFT_PAYLOAD_JSON)
    assert "draft" in str(exc_info.value)


def test_create_draft_post_credentials_never_in_body_or_query() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert _USERNAME not in str(request.url)
        assert _APP_PASSWORD not in str(request.url)
        assert _USERNAME not in request.content.decode("utf-8")
        assert _APP_PASSWORD not in request.content.decode("utf-8")
        return httpx.Response(
            201, json={"id": 1, "status": "draft", "slug": "exact-slug"}
        )

    _client(handler).create_draft_post_exact(_DRAFT_PAYLOAD_JSON)


# ==================== find_draft_posts_by_slug (duplicate preflight) =====
def test_find_draft_posts_by_slug_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("slug") == "exact-slug"
        assert request.url.params.get("status") == "draft"
        return httpx.Response(200, json=[])

    assert _client(handler).find_draft_posts_by_slug("exact-slug") == []


def test_find_draft_posts_by_slug_one_match() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": 99, "slug": "exact-slug"}])

    assert _client(handler).find_draft_posts_by_slug("exact-slug") == [99]


def test_find_draft_posts_by_slug_multiple_matches() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=[{"id": 5, "slug": "exact-slug"}, {"id": 6, "slug": "exact-slug"}]
        )

    assert _client(handler).find_draft_posts_by_slug("exact-slug") == [5, 6]


def test_find_draft_posts_by_slug_is_get_only_no_mutation_side_effect() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.method == "GET"
        return httpx.Response(200, json=[])

    _client(handler).find_draft_posts_by_slug("exact-slug")
    assert calls["n"] == 1


# ==================== get_post (read-back) =================================
def test_get_post_returns_parsed_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/wp-json/wp/v2/posts/42"
        assert request.method == "GET"
        return httpx.Response(200, json={"id": 42, "status": "draft", "slug": "exact-slug"})

    data = _client(handler).get_post(42)
    assert data["id"] == 42
    assert data["status"] == "draft"
