"""WordPress REST API 用の最小 HTTP client。

このフェーズの scope は **認証済み read-only probe のみ**
(``GET /wp-json/wp/v2/users/me``)。POST / PUT / PATCH / DELETE は一切実装しない。

credential (username / app password) は ``httpx.BasicAuth`` を通じてのみ transport 層へ
渡し、このモジュールの外へ Authorization 値を一切構築・露出しない。エラーメッセージには
credential・レスポンス本文・環境変数を含めない。
"""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

from app.config.settings import Settings
from app.exceptions import (
    ExternalProviderError,
    ProviderNotConfiguredError,
    WordPressTargetError,
)
from app.wordpress.target import canonicalize_wordpress_base_url

_PROVIDER = "wordpress"
_TIMEOUT_SECONDS = 10.0
_USERS_ME_PATH = "/wp-json/wp/v2/users/me"

# WordPress core: draft を作成 (公開はしない) するのに必要な最小 capability。
_DRAFT_CREATE_CAPABILITY_KEYS = ("edit_posts",)


class WordPressAuthProbeResult(BaseModel):
    """probe_current_user() の安全な戻り値。credential・email・生 user object は含まない。"""

    authenticated: bool
    user_id: int | None
    roles: list[str]
    capabilities_present: bool
    draft_create_capability: str  # "verified" | "unverified"
    target_base_url: str
    http_status: int


class WordPressClient:
    """認証済み read-only probe のみを提供する。書き込み系メソッドは存在しない。"""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not settings.wordpress_configured:
            raise ProviderNotConfiguredError(_PROVIDER)

        base_url = canonicalize_wordpress_base_url(settings.wordpress_base_url or "")
        if not base_url.startswith("https://"):
            raise WordPressTargetError(
                "WordPress client requires an https target (got non-https base URL)"
            )

        self._base_url = base_url
        self._username = settings.wordpress_username or ""
        self._app_password = settings.wordpress_app_password or ""
        self._verify_tls = settings.wordpress_verify_tls
        self._transport = transport

    @property
    def target_base_url(self) -> str:
        return self._base_url

    def probe_current_user(self) -> WordPressAuthProbeResult:
        """``GET /wp-json/wp/v2/users/me`` を一度だけ呼ぶ。書き込みは一切行わない。"""

        url = f"{self._base_url}{_USERS_ME_PATH}"
        try:
            with httpx.Client(
                transport=self._transport,
                verify=self._verify_tls,
                timeout=_TIMEOUT_SECONDS,
                follow_redirects=False,
            ) as client:
                response = client.get(
                    url,
                    params={"context": "edit"},
                    auth=httpx.BasicAuth(self._username, self._app_password),
                )
        except httpx.TimeoutException as exc:
            raise ExternalProviderError(_PROVIDER, "request timed out") from exc
        except httpx.TransportError as exc:
            raise ExternalProviderError(_PROVIDER, "connection failed") from exc
        except httpx.HTTPError as exc:  # pragma: no cover - httpx 内部の他エラー
            raise ExternalProviderError(_PROVIDER, "request failed") from exc

        return _handle_response(response, target_base_url=self._base_url)


def _handle_response(response: httpx.Response, *, target_base_url: str) -> WordPressAuthProbeResult:
    if response.is_redirect:
        origin = _origin_of(response.headers.get("location", ""))
        raise ExternalProviderError(
            _PROVIDER,
            f"blocked redirect to a different origin ({origin or 'unknown'})",
        )
    if response.status_code == 401:
        raise ExternalProviderError(_PROVIDER, "authentication failed (401)")
    if response.status_code == 403:
        raise ExternalProviderError(_PROVIDER, "insufficient permissions (403)")
    if response.status_code != 200:
        raise ExternalProviderError(
            _PROVIDER, f"unexpected response status {response.status_code}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise ExternalProviderError(_PROVIDER, "response was not valid JSON") from exc
    if not isinstance(data, dict):
        raise ExternalProviderError(_PROVIDER, "unexpected response shape")

    return _build_probe_result(
        data, target_base_url=target_base_url, http_status=response.status_code
    )


def _origin_of(location: str) -> str:
    parts = urlsplit(location)
    if parts.scheme and parts.hostname:
        return f"{parts.scheme}://{parts.hostname}"
    return ""


def _build_probe_result(
    data: dict, *, target_base_url: str, http_status: int
) -> WordPressAuthProbeResult:
    raw_id = data.get("id")
    user_id = raw_id if isinstance(raw_id, int) else None

    raw_roles = data.get("roles")
    roles = [str(r) for r in raw_roles] if isinstance(raw_roles, list) else []

    capabilities = data.get("capabilities")
    capabilities_present = isinstance(capabilities, dict) and bool(capabilities)

    draft_create_capability = "unverified"
    if capabilities_present and all(
        capabilities.get(k) for k in _DRAFT_CREATE_CAPABILITY_KEYS
    ):
        draft_create_capability = "verified"

    return WordPressAuthProbeResult(
        authenticated=True,
        user_id=user_id,
        roles=roles,
        capabilities_present=capabilities_present,
        draft_create_capability=draft_create_capability,
        target_base_url=target_base_url,
        http_status=http_status,
    )
