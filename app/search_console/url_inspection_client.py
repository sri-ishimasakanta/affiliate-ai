"""Google Search Console **URL Inspection** の read-only client (C5.1)。

``POST /v1/urlInspection/index:inspect`` は HTTP POST だが、Search Console の
**inspection (読み取り)** であって mutation ではない -- index 登録リクエストでも
サイト設定の変更でもない。この client は他の endpoint を一切持たない。

- C1-A の credential / auth transport を再利用する
  (:mod:`app.search_console.credentials` / :mod:`app.search_console.google_client`)。
  scope は ``webmasters.readonly`` 固定で、ここで広げることはできない。
- private_key / access token / Authorization header を print / 例外文言 /
  戻り値に一切含めない。
- URL Inspection API のクォータ (プロパティあたり 1 日 2,000 / 1 分 600) を
  尊重するため、呼び出し側が URL 数を決める。この client は自動 retry も
  バックグラウンド実行もしない。

Search Analytics (performance) と URL Inspection (index state) は別物であり、
この client は後者しか扱わない。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from app.exceptions import ExternalProviderError
from app.search_console.credentials import load_readonly_credentials
from app.search_console.google_client import _HttpxAuthRequest

_PROVIDER = "search_console"
_INSPECT_URL = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
_TIMEOUT_SECONDS = 60.0


#: path が percent-encoding された「予約文字ではないバイト列」だけを含むか判定するため、
#: decode 後に現れてはいけない文字。これらが現れる場合は decode すると URL の
#: 構造そのものが変わるので、元の表記のまま送る。
_STRUCTURAL_CHARACTERS = ("?", "#", "%")


def canonical_inspection_url(url: str) -> str:
    """URL Inspection API に渡す表記に正規化する。

    **なぜ必要か** (C6 で実測): Google は日本語 slug のページを **decode 済みの
    表記** で認識している。同じページを percent-encoded のまま inspect すると
    ``URL が Google に認識されていません`` (NEUTRAL) が返り、decode した表記で
    inspect すると ``送信して登録されました`` (PASS) が返る -- Search Console UI の
    表示とも後者が一致する。encoded のまま問い合わせると、インデックス済みの
    ページを未認識と誤判定してしまう。

    ただし ``%2F`` のように **予約文字** を表す escape は decode すると path の
    構造が変わるため、そのまま残す。decode 後に ``/`` の数が変わる、あるいは
    ``?`` ``#`` ``%`` が現れる場合は、元の表記を返す (安全側に倒す)。
    ASCII だけの URL では decode は何も変えないので、この正規化は無害である。
    """

    if not url or "%" not in url:
        return url
    parts = urlsplit(url)
    decoded_path = unquote(parts.path)
    if decoded_path.count("/") != parts.path.count("/"):
        return url
    if any(ch in decoded_path for ch in _STRUCTURAL_CHARACTERS):
        return url
    return urlunsplit((parts.scheme, parts.netloc, decoded_path, parts.query, parts.fragment))


@dataclass(frozen=True)
class UrlInspectionResult:
    """1 URL の inspection 結果。secret は含まない。"""

    inspection_url: str
    http_status: int | None
    inspection_result: dict | None
    error_status: str | None = None
    error_message: str | None = None

    @property
    def ok(self) -> bool:
        return self.http_status == 200 and self.inspection_result is not None


class UrlInspectionClient:
    """URL Inspection だけを行う最小 client。"""

    def __init__(self, settings, *, http_client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._http_client = http_client
        self._credentials = None

    @property
    def property_uri(self) -> str:
        uri = self._settings.search_console_property_uri
        if not uri:
            raise ExternalProviderError(_PROVIDER, "no Search Console property configured")
        return uri

    def _token(self) -> str:
        if self._credentials is None:
            self._credentials, _facts = load_readonly_credentials(
                self._settings.search_console_credentials_file
            )
        if not self._credentials.valid:
            self._credentials.refresh(_HttpxAuthRequest())
        token = self._credentials.token
        if not token:
            raise ExternalProviderError(_PROVIDER, "failed to obtain an access token")
        return token

    def inspect(self, url: str, *, language_code: str = "ja") -> UrlInspectionResult:
        """1 URL を inspect する。例外は投げず、失敗は結果に載せる。

        送信前に :func:`canonical_inspection_url` で表記を正規化する
        (percent-encoded な日本語 slug が未認識と誤判定されるのを防ぐ)。
        結果の ``inspection_url`` には **呼び出し側が渡した URL** をそのまま返し、
        記事との突き合わせが崩れないようにする。
        """

        inspection_url = canonical_inspection_url(url)
        payload = {
            "inspectionUrl": inspection_url,
            "siteUrl": self.property_uri,
            "languageCode": language_code,
        }
        owns_client = self._http_client is None
        http = self._http_client or httpx.Client(timeout=_TIMEOUT_SECONDS)
        try:
            response = http.post(
                _INSPECT_URL,
                json=payload,
                headers={"Authorization": f"Bearer {self._token()}"},
            )
        except Exception as exc:  # noqa: BLE001 - 失敗そのものが結果
            if owns_client:
                http.close()
            return UrlInspectionResult(
                inspection_url=url,
                http_status=None,
                inspection_result=None,
                error_status="TRANSPORT_ERROR",
                error_message=type(exc).__name__,
            )
        try:
            if response.status_code == 200:
                body = response.json()
                result = body.get("inspectionResult") if isinstance(body, dict) else None
                return UrlInspectionResult(
                    inspection_url=url,
                    http_status=200,
                    inspection_result=result if isinstance(result, dict) else None,
                )
            error = {}
            try:
                error = (response.json() or {}).get("error") or {}
            except Exception:  # noqa: BLE001 - 本文が JSON でないことがある
                error = {}
            return UrlInspectionResult(
                inspection_url=url,
                http_status=response.status_code,
                inspection_result=None,
                error_status=error.get("status") or f"HTTP_{response.status_code}",
                # message は Google の説明文で、credential は含まれない。
                error_message=error.get("message"),
            )
        finally:
            if owns_client:
                http.close()
