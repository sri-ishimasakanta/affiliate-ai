"""Google Search Console への **read-only** な最小アクセス client。

C1-A の scope: ``Sites.list`` (``GET /webmasters/v3/sites``) だけ。Search Analytics の
取得は C1-B。

- OAuth scope は :data:`SEARCH_CONSOLE_READONLY_SCOPE` 固定。
- token 交換も含め、実 HTTP は **httpx** で行う (google-api-python-client は使わない)。
- Authorization ヘッダ / access token / private_key を print / 例外文言 / snapshot に
  一切含めない。
- mutation メソッドは実装しない (この client は Search Console を変更できない)。
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.exceptions import (
    ExternalProviderDataError,
    ExternalProviderError,
)
from app.search_console.credentials import (
    SEARCH_CONSOLE_READONLY_SCOPE,
    load_readonly_credentials,
)

_PROVIDER = "search_console"
_SITES_LIST_URL = "https://www.googleapis.com/webmasters/v3/sites"
_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class SiteAccess:
    site_url: str
    permission_level: str | None


@dataclass(frozen=True)
class SitesListResult:
    accessible_property_count: int
    configured_property_uri: str
    configured_property_found: bool
    configured_permission_level: str | None
    http_ok: bool


class _HttpxAuthRequest:
    """``google.auth.transport.Request`` 互換の httpx アダプタ (token refresh 用)。"""

    def __init__(self, verify: bool = True) -> None:
        self._verify = verify

    def __call__(
        self, url, method="GET", body=None, headers=None, timeout=None, **kwargs
    ):
        try:
            with httpx.Client(
                timeout=timeout or _TIMEOUT_SECONDS,
                verify=self._verify,
                follow_redirects=False,
            ) as client:
                resp = client.request(
                    method, url, content=body, headers=dict(headers or {})
                )
        except httpx.HTTPError as exc:
            raise ExternalProviderError(_PROVIDER, "auth transport failed") from exc

        from google.auth.transport import Response as _AuthResponse  # 遅延 import

        class _R(_AuthResponse):
            status = resp.status_code
            headers = dict(resp.headers)
            data = resp.content

        return _R()


class SearchConsoleAccessClient:
    """検証済み credential を使い、Search Console への read-only アクセスを確認する。"""

    def __init__(
        self,
        *,
        credentials_file: str | None,
        property_uri: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._property_uri = property_uri
        self._transport = transport
        # credential file の存在・種別・repo外・JSON妥当性を検証してから Credentials 化。
        self._credentials, self._facts = load_readonly_credentials(credentials_file)
        # scope が readonly 固定であることを実行時にも確認する。
        if list(getattr(self._credentials, "scopes", []) or []) != [
            SEARCH_CONSOLE_READONLY_SCOPE
        ]:
            raise ExternalProviderError(
                _PROVIDER, "credentials are not scoped to webmasters.readonly only"
            )

    @property
    def service_account_email(self) -> str:
        return self._facts.client_email

    def list_sites(self) -> SitesListResult:
        """``GET /webmasters/v3/sites`` を 1 回だけ実行し、安全な事実のみ返す。"""

        token = self._obtain_access_token()

        try:
            with httpx.Client(
                transport=self._transport,
                timeout=_TIMEOUT_SECONDS,
                follow_redirects=False,
            ) as client:
                resp = client.get(
                    _SITES_LIST_URL,
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.TimeoutException as exc:
            raise ExternalProviderError(_PROVIDER, "request timed out") from exc
        except httpx.TransportError as exc:
            raise ExternalProviderError(_PROVIDER, "connection failed") from exc
        except httpx.HTTPError as exc:  # pragma: no cover
            raise ExternalProviderError(_PROVIDER, "request failed") from exc

        if resp.status_code == 401:
            raise ExternalProviderError(_PROVIDER, "authentication failed (401)")
        if resp.status_code == 403:
            raise ExternalProviderError(_PROVIDER, "permission denied (403)")
        if resp.status_code != 200:
            raise ExternalProviderDataError(
                _PROVIDER, f"unexpected response status {resp.status_code}"
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise ExternalProviderDataError(
                _PROVIDER, "response was not valid JSON"
            ) from exc
        if not isinstance(data, dict):
            raise ExternalProviderDataError(_PROVIDER, "unexpected response shape")

        entries_raw = data.get("siteEntry")
        entries = entries_raw if isinstance(entries_raw, list) else []
        sites = [
            SiteAccess(
                site_url=str(e.get("siteUrl")),
                permission_level=(
                    str(e["permissionLevel"]) if e.get("permissionLevel") else None
                ),
            )
            for e in entries
            if isinstance(e, dict) and e.get("siteUrl")
        ]

        match = next((s for s in sites if s.site_url == self._property_uri), None)
        return SitesListResult(
            accessible_property_count=len(sites),
            configured_property_uri=self._property_uri,
            configured_property_found=match is not None,
            configured_permission_level=match.permission_level if match else None,
            http_ok=True,
        )

    # -- internal -------------------------------------------------------
    def _obtain_access_token(self) -> str:
        try:
            self._credentials.refresh(_HttpxAuthRequest())
        except ExternalProviderError:
            raise
        except Exception as exc:  # RefreshError 等。token/key は載せない。
            raise ExternalProviderError(
                _PROVIDER, "service-account authentication failed"
            ) from exc
        token = getattr(self._credentials, "token", None)
        if not token:
            raise ExternalProviderError(_PROVIDER, "no access token obtained")
        return token
