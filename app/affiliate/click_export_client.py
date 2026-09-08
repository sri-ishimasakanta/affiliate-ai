"""WordPress runtime の outbound-click export endpoint を叩く control-plane client。

D-B2 の HMAC v1 署名/hash contract を GET + query cursor に適用する。**この module は
自分では通信を判断しない** — ``execute_click_export`` を明示的に呼んだときだけ
**ちょうど 1 回** GET する。auto retry しない。redirect は追わない。

secret / signature / raw response body / token 値 は repr / log / 例外文言に
一切出さない (``PreparedClickExport`` の header フィールドは ``repr=False``)。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

from app.affiliate.click_import_rows import (
    CLICK_EXPORT_SCHEMA_VERSION,
    ClickRow,
    validate_click_export_page,
)
from app.affiliate.projection_signing import (
    HEADER_CONTENT_SHA256,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    canonical_request_target,
    compute_signature,
    verify_signature,
)
from app.affiliate.runtime_http import known_server_code, require_https_origin
from app.exceptions import AffiliateClickImportError

CLICK_EXPORT_ENDPOINT_METHOD = "GET"
CLICK_EXPORT_ENDPOINT_PATH = "/wp-json/affiliate-ai/v1/outbound-clicks"

#: deployed MU-plugin が受け付ける最大 limit。
CLICK_EXPORT_MAX_LIMIT = 1000

_TIMEOUT_SECONDS = 15.0

#: 空 body (``b""``) の SHA-256。GET なので常にこの値で署名する。
_EMPTY_BODY_SHA256 = (
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)

#: deployed PHP (export endpoint + ``BFL_Hmac::verify``) が返しうる **安全な** code。
#: これ以外は握りつぶす (raw body を出さない)。
_KNOWN_SERVER_CODES = frozenset(
    {
        "secret_not_configured",
        "bad_timestamp",
        "content_sha256_mismatch",
        "signature_mismatch",
        "timestamp_outside_window",
        "bad_cursor",
        "bad_limit",
    }
)


def _require_plain_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AffiliateClickImportError(f"{field_name} must be an integer")
    return value


@dataclass(frozen=True)
class PreparedClickExport:
    """1 回の click export GET の凍結済みリクエスト。secret は保持しない。"""

    method: str
    url: str
    endpoint_path: str
    signed_target: str
    requested_since_id: int
    requested_limit: int
    body_sha256: str
    timestamp: int
    _headers: dict[str, str] = field(repr=False, compare=False)

    def headers(self) -> dict[str, str]:
        """送信ヘッダのコピー (``X-BFL-Signature`` を含む)。"""

        return dict(self._headers)


@dataclass(frozen=True)
class ClickExportPage:
    """検証済みの export レスポンス 1 ページ。"""

    http_status: int
    schema_version: int
    count: int
    limit: int
    next_since_id: int
    requested_since_id: int
    rows: tuple[ClickRow, ...]

    @property
    def has_more(self) -> bool:
        """受領件数が要求 limit 一杯なら、続きがある可能性が高い。"""

        return self.count == self.limit


def prepare_click_export(
    *,
    base_url: str,
    shared_secret: str,
    since_id: int,
    limit: int,
    now: int | None = None,
) -> PreparedClickExport:
    """cursor + limit -> 署名済み prepared GET。query は署名文字列に束縛する。"""

    if not shared_secret:
        raise AffiliateClickImportError(
            "shared secret is required to sign a click export request"
        )
    since_id = _require_plain_int(since_id, field_name="since_id")
    limit = _require_plain_int(limit, field_name="limit")
    if since_id < 0:
        raise AffiliateClickImportError("since_id must not be negative")
    if not (1 <= limit <= CLICK_EXPORT_MAX_LIMIT):
        raise AffiliateClickImportError(
            f"limit must be between 1 and {CLICK_EXPORT_MAX_LIMIT}"
        )

    origin = require_https_origin(base_url, error_cls=AffiliateClickImportError)
    # keys sorted: "limit" < "since_id" → path?limit=<L>&since_id=<N>
    signed_target = canonical_request_target(
        CLICK_EXPORT_ENDPOINT_PATH, {"limit": limit, "since_id": since_id}
    )
    ts = int(now) if now is not None else int(time.time())
    bh = _EMPTY_BODY_SHA256
    signature = compute_signature(
        shared_secret=shared_secret,
        method=CLICK_EXPORT_ENDPOINT_METHOD,
        path=signed_target,
        timestamp=ts,
        body_sha256_hex=bh,
    )
    headers = {
        HEADER_TIMESTAMP: str(ts),
        HEADER_CONTENT_SHA256: bh,
        HEADER_SIGNATURE: signature,
    }
    return PreparedClickExport(
        method=CLICK_EXPORT_ENDPOINT_METHOD,
        url=origin + signed_target,
        endpoint_path=CLICK_EXPORT_ENDPOINT_PATH,
        signed_target=signed_target,
        requested_since_id=since_id,
        requested_limit=limit,
        body_sha256=bh,
        timestamp=ts,
        _headers=headers,
    )


def signature_of(prepared: PreparedClickExport) -> str:
    """内部/テスト用: 送信予定の署名 (通常の repr/log には出さない)。"""

    return prepared._headers[HEADER_SIGNATURE]


def verify_prepared_signature(
    prepared: PreparedClickExport, *, shared_secret: str
) -> bool:
    """prepared の署名が (scheme, GET, signed target, ts, empty-body SHA) に対して
    正しいか (テスト seam)。"""

    return verify_signature(
        shared_secret=shared_secret,
        method=prepared.method,
        path=prepared.signed_target,
        timestamp=prepared.timestamp,
        body_sha256_hex=prepared.body_sha256,
        provided_signature=signature_of(prepared),
    )


def execute_click_export(
    prepared: PreparedClickExport,
    *,
    transport: httpx.BaseTransport | None = None,
    verify_tls: bool = True,
) -> ClickExportPage:
    """prepared GET を **ちょうど 1 回** 送る。auto retry しない。redirect は拒否。"""

    try:
        with httpx.Client(
            transport=transport,
            verify=verify_tls,
            timeout=_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as client:
            response = client.request(
                prepared.method,
                prepared.url,  # exact constructed URL; no params= reordering
                content=b"",  # empty body; hash/signature computed over b""
                headers=prepared.headers(),
            )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise AffiliateClickImportError(
            "export request failed; no page imported; re-run from the same since_id"
        ) from exc
    except httpx.HTTPError as exc:  # pragma: no cover - httpx 内部の他エラー
        raise AffiliateClickImportError(
            "export request failed; no page imported; re-run from the same since_id"
        ) from exc

    if response.is_redirect:
        raise AffiliateClickImportError(
            "click export endpoint returned a redirect"
        )

    if response.status_code != 200:
        code = known_server_code(response, _KNOWN_SERVER_CODES)
        raise AffiliateClickImportError(
            f"click export endpoint returned HTTP {response.status_code}"
            + (f" ({code})" if code else ""),
            http_status=response.status_code,
            server_code=code,
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise AffiliateClickImportError(
            "click export endpoint returned a non-JSON 200 response", http_status=200
        ) from exc

    rows, next_since_id = validate_click_export_page(
        data,
        since_id=prepared.requested_since_id,
        limit=prepared.requested_limit,
    )
    return ClickExportPage(
        http_status=200,
        schema_version=CLICK_EXPORT_SCHEMA_VERSION,
        count=len(rows),
        limit=prepared.requested_limit,
        next_since_id=next_since_id,
        requested_since_id=prepared.requested_since_id,
        rows=tuple(rows),
    )
