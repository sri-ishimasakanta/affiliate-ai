"""WordPress REST API 用の最小 HTTP client。

scope:
- 認証済み read-only probe (``GET /wp-json/wp/v2/users/me``)
- 既存 draft の重複確認 read-only GET (``GET /wp-json/wp/v2/posts?slug=...&status=draft``)
- 凍結済み payload をそのまま送る draft 作成 POST 1 種のみ
  (``POST /wp-json/wp/v2/posts``, status は必ず ``draft``)
- 作成後の read-back read-only GET (``GET /wp-json/wp/v2/posts/{id}``)
- 既存 post を publish する POST 1 種のみ
  (``POST /wp-json/wp/v2/posts/{id}``, body は必ず exact ``{"status":"publish"}``)

generic update / delete / bulk / media upload / 任意 status 設定 / content・title・
category 変更は一切実装しない。

credential (username / app password) は ``httpx.BasicAuth`` を通じてのみ transport 層へ
渡し、このモジュールの外へ Authorization 値を一切構築・露出しない。エラーメッセージには
credential・レスポンス本文・環境変数を含めない。

書き込み POST は :func:`WordPressClient.create_draft_post_exact` /
:func:`WordPressClient.publish_existing_post_exact` が 1 回だけ送る。自動リトライは一切
行わない (呼び出し側も含め、このモジュールはリトライ機構を持たない)。
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

from app.config.settings import Settings
from app.exceptions import (
    ExternalProviderError,
    ProviderNotConfiguredError,
    WordPressAmbiguousOutcomeError,
    WordPressTargetError,
)
from app.wordpress.target import canonicalize_wordpress_base_url

_PROVIDER = "wordpress"
_TIMEOUT_SECONDS = 10.0
_USERS_ME_PATH = "/wp-json/wp/v2/users/me"
_POSTS_PATH = "/wp-json/wp/v2/posts"
_DRAFT_STATUS = "draft"
_PUBLISH_STATUS = "publish"

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


class WordPressCreatedPost(BaseModel):
    """create_draft_post_exact() の安全な戻り値。"""

    id: int
    status: str
    slug: str
    link: str | None


class WordPressPublishedPost(BaseModel):
    """publish_existing_post_exact() の安全な戻り値。credential・生 response は含まない。"""

    id: int
    status: str
    link: str
    slug: str | None
    date_gmt: str | None


class WordPressClient:
    """WordPress REST API への narrow な read/write。書き込みは draft 作成 1 種のみ。"""

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

    # -- read-only ------------------------------------------------------
    def probe_current_user(self) -> WordPressAuthProbeResult:
        """``GET /wp-json/wp/v2/users/me`` を一度だけ呼ぶ。書き込みは一切行わない。"""

        response = self._send(
            "GET",
            f"{self._base_url}{_USERS_ME_PATH}",
            params={"context": "edit"},
            ambiguous_on_no_response=False,
        )
        data = _expect_json_object(_check_status(response, expected_status=200))
        return _build_probe_result(
            data, target_base_url=self._base_url, http_status=response.status_code
        )

    def find_draft_posts_by_slug(self, slug: str) -> list[int]:
        """指定 slug の既存 draft を確認する read-only GET (重複作成 preflight 専用)。

        書き込みは一切行わない。safe な post id のみを返す。
        """

        response = _check_status(
            self._send(
                "GET",
                f"{self._base_url}{_POSTS_PATH}",
                params={"slug": slug, "status": _DRAFT_STATUS, "context": "edit"},
                ambiguous_on_no_response=False,
            ),
            expected_status=200,
        )
        try:
            items = response.json()
        except ValueError as exc:
            raise ExternalProviderError(_PROVIDER, "response was not valid JSON") from exc
        if not isinstance(items, list):
            raise ExternalProviderError(_PROVIDER, "unexpected response shape")
        return [
            item["id"]
            for item in items
            if isinstance(item, dict) and isinstance(item.get("id"), int)
        ]

    def get_post(self, post_id: int) -> dict:
        """作成後の read-back 専用 read-only GET。安全な最小フィールドのみ想定して呼ぶこと。"""

        response = self._send(
            "GET",
            f"{self._base_url}{_POSTS_PATH}/{post_id}",
            params={"context": "edit"},
            ambiguous_on_no_response=False,
        )
        return _expect_json_object(_check_status(response, expected_status=200))

    # -- the one write operation -----------------------------------------
    def create_draft_post_exact(self, payload_json: str) -> WordPressCreatedPost:
        """凍結済み ``payload_json`` の exact bytes をそのまま POST する。

        - 再シリアライズしない (``content=`` で生バイトを送る。``json=`` は使わない)。
        - status は必ず ``draft`` (呼び出し側の frozen payload を信頼しつつ、ここでも
          念のため検証する — publish を送る経路は存在しない)。
        - リトライは一切しない。timeout / 接続断はレスポンス未確定として
          :class:`WordPressAmbiguousOutcomeError` を送出する (呼び出し側が定義的な
          失敗と区別できるようにする)。
        """

        parsed = json.loads(payload_json)
        if not isinstance(parsed, dict) or parsed.get("status") != _DRAFT_STATUS:
            raise ValueError("create_draft_post_exact only accepts a frozen draft payload")

        body = payload_json.encode("utf-8")
        response = self._send(
            "POST",
            f"{self._base_url}{_POSTS_PATH}",
            content=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            ambiguous_on_no_response=True,
        )
        data = _expect_json_object(_check_status(response, expected_status=201))

        post_id = data.get("id")
        wp_status = data.get("status")
        slug = data.get("slug")
        link = data.get("link")
        if not isinstance(post_id, int) or post_id <= 0:
            raise ExternalProviderError(_PROVIDER, "response did not include a valid post id")
        if wp_status != _DRAFT_STATUS:
            raise ExternalProviderError(
                _PROVIDER, f"unexpected post status {wp_status!r} (expected draft)"
            )

        return WordPressCreatedPost(
            id=post_id,
            status=str(wp_status),
            slug=str(slug) if isinstance(slug, str) else "",
            link=str(link) if isinstance(link, str) else None,
        )

    def publish_existing_post_exact(
        self, wordpress_post_id: int, publish_payload_json: str
    ) -> WordPressPublishedPost:
        """既存 post を publish する。``POST /wp-json/wp/v2/posts/{id}`` を **1 回だけ**。

        - body は exact な frozen bytes (``content=`` で送る。``json=`` は使わない)。
        - payload は logically ちょうど ``{"status":"publish"}`` でなければ拒否
          (title / content / excerpt / slug / date / date_gmt / categories / meta /
          author など第 2 のキーが 1 つでもあれば ``ValueError``)。この client は
          別の WordPress mutation を構造的に行えない。
        - リトライは一切しない。timeout / 接続断はレスポンス未確定として
          :class:`WordPressAmbiguousOutcomeError` を送出する。
        """

        _assert_exact_publish_payload(publish_payload_json)

        body = publish_payload_json.encode("utf-8")
        response = self._send(
            "POST",
            f"{self._base_url}{_POSTS_PATH}/{wordpress_post_id}",
            content=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            ambiguous_on_no_response=True,
        )
        data = _expect_json_object(_check_status(response, expected_status=200))

        post_id = data.get("id")
        wp_status = data.get("status")
        link = data.get("link")
        slug = data.get("slug")
        date_gmt = data.get("date_gmt")

        if post_id != wordpress_post_id:
            raise ExternalProviderError(
                _PROVIDER,
                f"publish response post id {post_id!r} != expected {wordpress_post_id!r}",
            )
        if wp_status != _PUBLISH_STATUS:
            raise ExternalProviderError(
                _PROVIDER, f"unexpected post status {wp_status!r} (expected publish)"
            )
        link_ok = isinstance(link, str) and (
            link == self._base_url or link.startswith(f"{self._base_url}/")
        )
        if not link_ok:
            raise ExternalProviderError(
                _PROVIDER, "publish response link is missing or not same-origin https"
            )

        return WordPressPublishedPost(
            id=post_id,
            status=str(wp_status),
            link=link,
            slug=str(slug) if isinstance(slug, str) else None,
            date_gmt=str(date_gmt) if isinstance(date_gmt, str) and date_gmt else None,
        )

    # -- transport --------------------------------------------------------
    def _send(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        content: bytes | None = None,
        headers: dict | None = None,
        ambiguous_on_no_response: bool,
    ) -> httpx.Response:
        try:
            with httpx.Client(
                transport=self._transport,
                verify=self._verify_tls,
                timeout=_TIMEOUT_SECONDS,
                follow_redirects=False,
            ) as client:
                return client.request(
                    method,
                    url,
                    params=params,
                    content=content,
                    headers=headers,
                    auth=httpx.BasicAuth(self._username, self._app_password),
                )
        except httpx.TimeoutException as exc:
            if ambiguous_on_no_response:
                raise WordPressAmbiguousOutcomeError(
                    "request timed out; WordPress outcome unknown"
                ) from exc
            raise ExternalProviderError(_PROVIDER, "request timed out") from exc
        except httpx.TransportError as exc:
            if ambiguous_on_no_response:
                raise WordPressAmbiguousOutcomeError(
                    "connection failed; WordPress outcome unknown"
                ) from exc
            raise ExternalProviderError(_PROVIDER, "connection failed") from exc
        except httpx.HTTPError as exc:  # pragma: no cover - httpx 内部の他エラー
            if ambiguous_on_no_response:
                raise WordPressAmbiguousOutcomeError(
                    "request failed; WordPress outcome unknown"
                ) from exc
            raise ExternalProviderError(_PROVIDER, "request failed") from exc


def _assert_exact_publish_payload(publish_payload_json: str) -> None:
    """logically ちょうど ``{"status":"publish"}`` であることを検証する。

    それ以外 (第 2 のキー / 別の status 値 / dict でない) は ``ValueError``。
    """

    parsed = json.loads(publish_payload_json)
    if (
        not isinstance(parsed, dict)
        or set(parsed) != {"status"}
        or parsed.get("status") != _PUBLISH_STATUS
    ):
        raise ValueError(
            "publish_existing_post_exact only accepts an exact {\"status\":\"publish\"} payload"
        )


def _check_status(response: httpx.Response, *, expected_status: int) -> httpx.Response:
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
    if response.status_code != expected_status:
        raise ExternalProviderError(
            _PROVIDER, f"unexpected response status {response.status_code}"
        )
    return response


def _expect_json_object(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except ValueError as exc:
        raise ExternalProviderError(_PROVIDER, "response was not valid JSON") from exc
    if not isinstance(data, dict):
        raise ExternalProviderError(_PROVIDER, "unexpected response shape")
    return data


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
