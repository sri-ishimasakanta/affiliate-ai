"""Threads API の HTTP client (T1)。

関心事はひとつだけ: **認証つきの HTTP をどう話すか**。ここにアプリケーションの
判断は入れない (それは :mod:`app.social.threads.service` の担当)。

安全上の約束:

- access token は ``Settings`` から読むだけで、**ログにも DB にも例外にも出さない**。
  Meta の API は token をリクエストパラメータとして受け取るので、診断に出す前に
  必ず :func:`~app.social.threads.errors.redact` を通す。
- TLS 検証は既定のまま。無効化するスイッチは **作らない**。
- 明示的なタイムアウトを必ず付ける (無期限に待たない)。
- 応答サイズに上限を置く (壊れた応答でメモリを食い潰さない)。
- 生の応答本文を例外に載せない。載せるのは分類・status・API のコードだけ。

**T1 では本番公開を行わない。** 公開系のメソッドは実装してあるが、呼び出し側が
明示的に実行を選んだときにだけ到達する (service 側が既定を PLAN にしている)。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.social.threads.errors import (
    ThreadsNotConfiguredError,
    ThreadsResponseError,
    ThreadsTimeoutError,
    classify,
    redact,
)
from app.social.threads.models import (
    CONTAINER_FIELDS,
    DEFAULT_API_BASE_URL,
    DEFAULT_API_VERSION,
    MEDIA_FIELDS,
    MEDIA_TYPE_TEXT,
    PROFILE_FIELDS,
    ThreadsContainer,
    ThreadsProfile,
    ThreadsPublication,
)

DEFAULT_TIMEOUT_SECONDS = 20.0
#: 応答の上限 (Threads の JSON はごく小さい)。
MAX_RESPONSE_BYTES = 512 * 1024


class ThreadsClient:
    """認証つき HTTP のみ。アプリケーションの判断は持たない。"""

    def __init__(self, settings, *, http_client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._http = http_client

    # -- configuration --------------------------------------------------------
    @property
    def base(self) -> str:
        raw = (getattr(self._settings, "threads_api_base_url", "") or "").strip()
        base = (raw or DEFAULT_API_BASE_URL).rstrip("/")
        if not base.startswith("https://"):
            raise ThreadsNotConfiguredError("the Threads API base URL must be https")
        version = (
            getattr(self._settings, "threads_api_version", "") or ""
        ).strip() or DEFAULT_API_VERSION
        return f"{base}/{version}"

    def _token(self) -> str:
        token = (getattr(self._settings, "threads_access_token", "") or "").strip()
        if not token:
            raise ThreadsNotConfiguredError("THREADS_ACCESS_TOKEN is not configured")
        return token

    def _user_id(self) -> str:
        user_id = str(getattr(self._settings, "threads_user_id", "") or "").strip()
        if not user_id:
            raise ThreadsNotConfiguredError("THREADS_USER_ID is not configured")
        return user_id

    # -- read -----------------------------------------------------------------
    def fetch_profile(self) -> ThreadsProfile:
        """アカウントの最小情報を読む (接続確認用、副作用なし)。"""

        payload = self._get(f"/{self._user_id()}", {"fields": ",".join(PROFILE_FIELDS)})
        user_id = payload.get("id")
        if not user_id:
            raise ThreadsResponseError("the profile response carried no id")
        return ThreadsProfile(user_id=str(user_id), username=payload.get("username"))

    def fetch_media(self, media_id: str, *, fields=MEDIA_FIELDS) -> dict[str, Any]:
        """公開済み投稿 1 件を読む (T3 の計測で使う)。"""

        return self._get(f"/{media_id}", {"fields": ",".join(fields)})

    def fetch_container_status(self, creation_id: str) -> dict[str, Any]:
        """コンテナの状態を読む (公開が成立したかの照合に使う)。

        公式のトラブルシューティングに従い ``status`` / ``error_message`` を問う。
        ``PUBLISHED`` が返れば、応答を取りこぼしただけで公開は成立している。
        """

        return self._get(f"/{creation_id}", {"fields": ",".join(CONTAINER_FIELDS)})

    def fetch_media_insights(self, media_id: str, metrics) -> dict[str, Any]:
        return self._get(f"/{media_id}/insights", {"metric": ",".join(metrics)})

    def fetch_user_insights(self, metrics, *, since=None, until=None) -> dict[str, Any]:
        params: dict[str, Any] = {"metric": ",".join(metrics)}
        if since is not None:
            params["since"] = int(since)
        if until is not None:
            params["until"] = int(until)
        return self._get(f"/{self._user_id()}/threads_insights", params)

    # -- write (T1 では呼ばれない) --------------------------------------------
    def create_text_container(self, text: str) -> ThreadsContainer:
        """テキスト投稿のコンテナを作る。**まだ公開されない。**"""

        payload = self._post(
            f"/{self._user_id()}/threads", {"media_type": MEDIA_TYPE_TEXT, "text": text}
        )
        creation_id = payload.get("id")
        if not creation_id:
            raise ThreadsResponseError("the container response carried no id")
        return ThreadsContainer(creation_id=str(creation_id))

    def publish_container(self, creation_id: str) -> ThreadsPublication:
        """コンテナを公開する。**これが唯一の外部公開操作である。**"""

        payload = self._post(
            f"/{self._user_id()}/threads_publish", {"creation_id": str(creation_id)}
        )
        media_id = payload.get("id")
        if not media_id:
            raise ThreadsResponseError("the publish response carried no id")
        return ThreadsPublication(media_id=str(media_id))

    # -- transport ------------------------------------------------------------
    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._request("GET", path, params={**params, "access_token": self._token()})

    def _post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, data={**data, "access_token": self._token()})

    def _request(self, method: str, path: str, *, params=None, data=None) -> dict[str, Any]:
        url = f"{self.base}{path}"
        owns = self._http is None
        http = self._http or httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS)
        try:
            response = http.request(method, url, params=params, data=data)
        except httpx.TimeoutException:
            raise ThreadsTimeoutError(f"{method} {_safe_path(path)} timed out") from None
        except Exception as exc:  # noqa: BLE001 - 応答/URL を載せない
            raise ThreadsResponseError(
                f"transport failure on {_safe_path(path)}: {type(exc).__name__}"
            ) from None
        finally:
            if owns:
                http.close()
        return self._decode(response, path)

    def _decode(self, response, path: str) -> dict[str, Any]:
        body = response.content or b""
        if len(body) > MAX_RESPONSE_BYTES:
            raise ThreadsResponseError(f"the response for {_safe_path(path)} was too large")
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (ValueError, UnicodeDecodeError):
            raise ThreadsResponseError(
                f"the response for {_safe_path(path)} was not JSON"
            ) from None
        if not isinstance(payload, dict):
            raise ThreadsResponseError(f"the response for {_safe_path(path)} was not an object")

        if response.status_code >= 400:
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            api_code = error.get("code")
            subcode = error.get("error_subcode")
            # message は Meta 側の文言。redact を通してから載せる。
            message = redact(str(error.get("message") or "")) or "no message"
            cls = classify(response.status_code, api_code, subcode)
            raise cls(
                f"{_safe_path(path)} failed: {message}",
                status=response.status_code,
                api_code=str(api_code) if api_code is not None else None,
            )
        return payload


def _safe_path(path: str) -> str:
    """診断に出してよい経路 (id は残るが、token は構造上ここに現れない)。"""

    return redact(path)
