"""app/search_console/google_client.py の read-only アクセス client。

実 Google リクエストは一切行わない:
- credential 読み込み境界 (``load_readonly_credentials``) は fake に差し替え。
- HTTP 境界は ``httpx.MockTransport`` に差し替え。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.exceptions import ExternalProviderDataError, ExternalProviderError
from app.search_console import google_client as gc
from app.search_console.credentials import SEARCH_CONSOLE_READONLY_SCOPE

_PROP = "sc-domain:bizfluxlab.com"
_EMAIL = "gsc-probe@example.iam.gserviceaccount.com"


class _FakeCreds:
    def __init__(
        self,
        *,
        scopes=(SEARCH_CONSOLE_READONLY_SCOPE,),
        token: str | None = "fake-access-token",
        refresh_exc: Exception | None = None,
    ) -> None:
        self.scopes = list(scopes)
        self.token = None
        self._token_after_refresh = token
        self._refresh_exc = refresh_exc

    def refresh(self, request) -> None:  # noqa: ANN001 - google.auth.transport.Request
        if self._refresh_exc is not None:
            raise self._refresh_exc
        self.token = self._token_after_refresh


class _FakeFacts:
    def __init__(self) -> None:
        self.client_email = _EMAIL
        self.path = Path("/outside/repo/gsc.json")


@pytest.fixture
def patch_loader(monkeypatch):
    """``load_readonly_credentials`` を fake に差し替えるファクトリを返す。"""

    def _apply(creds: _FakeCreds):
        def _fake_load(raw_path):  # noqa: ANN001
            return creds, _FakeFacts()

        monkeypatch.setattr(gc, "load_readonly_credentials", _fake_load)

    return _apply


def _client(transport: httpx.MockTransport) -> gc.SearchConsoleAccessClient:
    return gc.SearchConsoleAccessClient(
        credentials_file="/outside/repo/gsc.json",
        property_uri=_PROP,
        transport=transport,
    )


def _sites_payload(*entries: dict) -> dict:
    return {"siteEntry": list(entries)}


# --------------------------------------------------------------------------
# scope / construction
# --------------------------------------------------------------------------
def test_rejects_credentials_with_extra_scope(patch_loader) -> None:
    patch_loader(
        _FakeCreds(
            scopes=(
                SEARCH_CONSOLE_READONLY_SCOPE,
                "https://www.googleapis.com/auth/webmasters",
            )
        )
    )
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
    with pytest.raises(ExternalProviderError, match="readonly only"):
        _client(transport)


def test_rejects_credentials_with_no_scope(patch_loader) -> None:
    patch_loader(_FakeCreds(scopes=()))
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
    with pytest.raises(ExternalProviderError):
        _client(transport)


def test_service_account_email_is_safe_fact(patch_loader) -> None:
    patch_loader(_FakeCreds())
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
    client = _client(transport)
    assert client.service_account_email == _EMAIL


# --------------------------------------------------------------------------
# happy paths
# --------------------------------------------------------------------------
def test_list_sites_makes_exactly_one_get_to_sites_list(patch_loader) -> None:
    patch_loader(_FakeCreds())
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json=_sites_payload(
                {"siteUrl": _PROP, "permissionLevel": "siteOwner"},
                {"siteUrl": "https://other.example/", "permissionLevel": "siteFullUser"},
            ),
        )

    client = _client(httpx.MockTransport(handler))
    result = client.list_sites()

    assert len(calls) == 1
    assert calls[0].method == "GET"
    assert str(calls[0].url) == gc._SITES_LIST_URL
    assert calls[0].headers["authorization"] == "Bearer fake-access-token"
    assert result.http_ok is True
    assert result.accessible_property_count == 2
    assert result.configured_property_found is True
    assert result.configured_permission_level == "siteOwner"
    assert result.configured_property_uri == _PROP


def test_configured_property_absent(patch_loader) -> None:
    patch_loader(_FakeCreds())
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            json=_sites_payload(
                {"siteUrl": "https://other.example/", "permissionLevel": "siteOwner"}
            ),
        )
    )
    result = _client(transport).list_sites()
    assert result.accessible_property_count == 1
    assert result.configured_property_found is False
    assert result.configured_permission_level is None


def test_empty_site_list(patch_loader) -> None:
    patch_loader(_FakeCreds())
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
    result = _client(transport).list_sites()
    assert result.accessible_property_count == 0
    assert result.configured_property_found is False


def test_entry_without_permission_level(patch_loader) -> None:
    patch_loader(_FakeCreds())
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json=_sites_payload({"siteUrl": _PROP}))
    )
    result = _client(transport).list_sites()
    assert result.configured_property_found is True
    assert result.configured_permission_level is None


# --------------------------------------------------------------------------
# HTTP error mapping
# --------------------------------------------------------------------------
def test_http_401_maps_to_provider_error(patch_loader) -> None:
    patch_loader(_FakeCreds())
    transport = httpx.MockTransport(lambda req: httpx.Response(401, json={}))
    with pytest.raises(ExternalProviderError, match="401") as exc:
        _client(transport).list_sites()
    assert "fake-access-token" not in str(exc.value)
    assert "Bearer" not in str(exc.value)


def test_http_403_maps_to_provider_error(patch_loader) -> None:
    patch_loader(_FakeCreds())
    transport = httpx.MockTransport(lambda req: httpx.Response(403, json={}))
    with pytest.raises(ExternalProviderError, match="403") as exc:
        _client(transport).list_sites()
    assert "fake-access-token" not in str(exc.value)


def test_http_500_maps_to_data_error(patch_loader) -> None:
    patch_loader(_FakeCreds())
    transport = httpx.MockTransport(lambda req: httpx.Response(500, text="boom"))
    with pytest.raises(ExternalProviderDataError, match="status 500"):
        _client(transport).list_sites()


def test_non_json_body_maps_to_data_error(patch_loader) -> None:
    patch_loader(_FakeCreds())
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, text="<html>not json</html>")
    )
    with pytest.raises(ExternalProviderDataError):
        _client(transport).list_sites()


def test_json_array_body_maps_to_data_error(patch_loader) -> None:
    patch_loader(_FakeCreds())
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=[1, 2, 3]))
    with pytest.raises(ExternalProviderDataError, match="shape"):
        _client(transport).list_sites()


# --------------------------------------------------------------------------
# transport failure mapping
# --------------------------------------------------------------------------
def test_timeout_maps_to_provider_error(patch_loader) -> None:
    patch_loader(_FakeCreds())

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow", request=request)

    with pytest.raises(ExternalProviderError, match="timed out"):
        _client(httpx.MockTransport(handler)).list_sites()


def test_connection_failure_maps_to_provider_error(patch_loader) -> None:
    patch_loader(_FakeCreds())

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(ExternalProviderError, match="connection failed"):
        _client(httpx.MockTransport(handler)).list_sites()


# --------------------------------------------------------------------------
# auth (token exchange) failure mapping
# --------------------------------------------------------------------------
def test_refresh_failure_maps_to_provider_error_without_secret(patch_loader) -> None:
    patch_loader(_FakeCreds(refresh_exc=RuntimeError("invalid_grant: bad key material")))
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
    with pytest.raises(ExternalProviderError, match="authentication failed") as exc:
        _client(transport).list_sites()
    assert "bad key material" not in str(exc.value)


def test_missing_token_after_refresh_maps_to_provider_error(patch_loader) -> None:
    patch_loader(_FakeCreds(token=None))
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
    with pytest.raises(ExternalProviderError, match="no access token"):
        _client(transport).list_sites()


# --------------------------------------------------------------------------
# read-only guarantees (static)
# --------------------------------------------------------------------------
def test_client_exposes_no_mutation_methods() -> None:
    public = {
        name
        for name in dir(gc.SearchConsoleAccessClient)
        if not name.startswith("_")
    }
    assert public == {"list_sites", "service_account_email"}


def test_module_issues_no_mutating_http_verbs() -> None:
    src = Path(gc.__file__).read_text(encoding="utf-8")
    for verb in (".post(", ".put(", ".patch(", ".delete("):
        assert verb not in src, f"unexpected mutating verb {verb!r} in google_client"


def test_module_does_not_touch_the_database() -> None:
    src = Path(gc.__file__).read_text(encoding="utf-8")
    assert "sqlalchemy" not in src.lower()
    assert "session" not in src.lower()
