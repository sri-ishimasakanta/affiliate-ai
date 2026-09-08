"""Affiliate target projection を WordPress runtime へ push する control-plane client。

D-B2 の署名/hash contract をそのまま使う。**この module は自分では通信を判断しない** —
``execute_projection_push`` を明示的に呼んだときだけ 1 回 POST する。自動 retry しない。

secret / signature / 生 request body / destination URL は repr / log / 例外文言に
一切出さない (``PreparedProjectionPush`` の transport フィールドは ``repr=False``)。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from app.affiliate.projection import (
    AffiliateProjectionSnapshot,
    build_projection_batch_request,
)
from app.affiliate.projection_signing import (
    HEADER_CONTENT_SHA256,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    PROJECTION_ENDPOINT_METHOD,
    PROJECTION_ENDPOINT_PATH,
    body_sha256,
    compute_signature,
    verify_signature,
)
from app.article.draft_input_canonical import canonical_json
from app.exceptions import AffiliateProjectionPushError

_TIMEOUT_SECONDS = 15.0

# サーバが返しうる **安全な** machine code。これ以外は握りつぶす (raw body を出さない)。
_KNOWN_SERVER_CODES = frozenset(
    {
        "secret_not_configured",
        "bad_timestamp",
        "content_sha256_mismatch",
        "body_hash_mismatch",
        "signature_mismatch",
        "bad_signature",
        "timestamp_outside_window",
        "invalid_json",
        "bad_schema_version",
        "bad_targets",
        "bad_entry",
        "bad_token",
        "bad_status",
        "bad_version_for_status",
        "bad_link_identity_hash",
        "bad_projection_entry_hash",
        "bad_destination_url",
        "bad_destination_host",
        "bad_activated_at",
        "bad_disabled_at",
        "entry_hash_mismatch",
        "destination_validation_failed",
        "snapshot_hash_mismatch",
        "conflict_hash_mismatch",
        "conflict_stale_version",
        "forbidden_reactivation",
        "conflict_immutable_identity_drift",
        "persist_failed",
    }
)


def _require_https_origin(base_url: str) -> str:
    """base URL を **WordPress origin のみ** に制限して返す (``https://host[:port]``)。

    ``Settings.wordpress_base_url`` には検証が無いため、実行境界の安全性はこの client
    が担保する: scheme は https 固定 / userinfo 不可 / host 必須 / query・fragment・
    path を持たない。signed endpoint path (:data:`PROJECTION_ENDPOINT_PATH`) は
    caller から差し替えられない。
    """

    parts = urlsplit((base_url or "").strip())
    if parts.scheme.lower() != "https":
        raise AffiliateProjectionPushError(
            "affiliate runtime base URL must be https"
        )
    if parts.username or parts.password or "@" in (parts.netloc or ""):
        raise AffiliateProjectionPushError(
            "affiliate runtime base URL must not contain userinfo"
        )
    if not parts.hostname:
        raise AffiliateProjectionPushError("affiliate runtime base URL has no host")
    if parts.query or parts.fragment:
        raise AffiliateProjectionPushError(
            "affiliate runtime base URL must not carry a query or fragment"
        )
    if parts.path not in ("", "/"):
        raise AffiliateProjectionPushError(
            "affiliate runtime base URL must be an origin only (no path)"
        )
    return f"https://{parts.netloc.lower()}"


@dataclass(frozen=True)
class PreparedProjectionPush:
    """1 回の projection push の凍結済みリクエスト。secret は保持しない。"""

    method: str
    url: str
    endpoint_path: str
    body_sha256: str
    projection_snapshot_hash: str
    target_count: int
    timestamp: int
    _body: bytes = field(repr=False, compare=False)
    _headers: dict[str, str] = field(repr=False, compare=False)

    def body_bytes(self) -> bytes:
        """HTTP で送る exact bytes。hash / signature はこの bytes に対して計算済み。"""

        return self._body

    def headers(self) -> dict[str, str]:
        """送信ヘッダのコピー (``X-BFL-Signature`` を含む)。"""

        return dict(self._headers)


@dataclass(frozen=True)
class ProjectionPushResult:
    http_status: int
    schema_version: int
    projection_snapshot_hash: str
    received_count: int
    inserted_count: int
    updated_count: int
    unchanged_count: int


def prepare_projection_push(
    snapshot: AffiliateProjectionSnapshot,
    *,
    base_url: str,
    shared_secret: str,
    now: int | None = None,
) -> PreparedProjectionPush:
    """snapshot -> 署名済み prepared request。**1 回だけ serialize** して同じ bytes を使う。"""

    if not shared_secret:
        raise AffiliateProjectionPushError(
            "shared secret is required to sign a projection push"
        )
    origin = _require_https_origin(base_url)

    payload = build_projection_batch_request(snapshot)
    # ここで **一度だけ** serialize。以降この bytes をそのまま hash / sign / send する。
    body = canonical_json(payload).encode("utf-8")
    ts = int(now) if now is not None else int(time.time())
    bh = body_sha256(body)
    signature = compute_signature(
        shared_secret=shared_secret,
        method=PROJECTION_ENDPOINT_METHOD,
        path=PROJECTION_ENDPOINT_PATH,
        timestamp=ts,
        body_sha256_hex=bh,
    )
    headers = {
        "Content-Type": "application/json",
        HEADER_TIMESTAMP: str(ts),
        HEADER_CONTENT_SHA256: bh,
        HEADER_SIGNATURE: signature,
    }
    return PreparedProjectionPush(
        method=PROJECTION_ENDPOINT_METHOD,
        url=origin + PROJECTION_ENDPOINT_PATH,
        endpoint_path=PROJECTION_ENDPOINT_PATH,
        body_sha256=bh,
        projection_snapshot_hash=snapshot.projection_snapshot_hash,
        target_count=len(snapshot.targets),
        timestamp=ts,
        _body=body,
        _headers=headers,
    )


def signature_of(prepared: PreparedProjectionPush) -> str:
    """内部/テスト用: 送信予定の署名 (通常の repr/log には出さない)。"""

    return prepared._headers[HEADER_SIGNATURE]


def verify_prepared_signature(
    prepared: PreparedProjectionPush, *, shared_secret: str
) -> bool:
    """prepared の署名が exact body bytes に対して正しいか (テスト seam)。"""

    return verify_signature(
        shared_secret=shared_secret,
        method=prepared.method,
        path=prepared.endpoint_path,
        timestamp=prepared.timestamp,
        body_sha256_hex=body_sha256(prepared.body_bytes()),
        provided_signature=signature_of(prepared),
    )


def _safe_server_code(response: httpx.Response) -> str | None:
    try:
        data = response.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    code = (data.get("error") or {}).get("code") if isinstance(data.get("error"), dict) else None
    return code if isinstance(code, str) and code in _KNOWN_SERVER_CODES else None


def _validate_success(
    data: dict, prepared: PreparedProjectionPush
) -> ProjectionPushResult:
    if data.get("schema_version") != 1:
        raise AffiliateProjectionPushError(
            "projection endpoint returned an unexpected schema_version", http_status=200
        )
    if data.get("projection_snapshot_hash") != prepared.projection_snapshot_hash:
        raise AffiliateProjectionPushError(
            "projection endpoint echoed a different projection_snapshot_hash",
            http_status=200,
        )
    counts: dict[str, int] = {}
    for key in (
        "received_count",
        "inserted_count",
        "updated_count",
        "unchanged_count",
    ):
        value = data.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise AffiliateProjectionPushError(
                f"projection endpoint returned an invalid {key}", http_status=200
            )
        counts[key] = value
    if counts["received_count"] != prepared.target_count:
        raise AffiliateProjectionPushError(
            "projection endpoint received a different target count than was sent",
            http_status=200,
        )
    if prepared.target_count == 0 and any(
        counts[k] != 0
        for k in ("inserted_count", "updated_count", "unchanged_count")
    ):
        raise AffiliateProjectionPushError(
            "empty snapshot push reported non-zero write counts", http_status=200
        )
    if (
        counts["inserted_count"]
        + counts["updated_count"]
        + counts["unchanged_count"]
        != counts["received_count"]
    ):
        raise AffiliateProjectionPushError(
            "projection endpoint write counts do not sum to received_count",
            http_status=200,
        )
    return ProjectionPushResult(
        http_status=200,
        schema_version=1,
        projection_snapshot_hash=prepared.projection_snapshot_hash,
        received_count=counts["received_count"],
        inserted_count=counts["inserted_count"],
        updated_count=counts["updated_count"],
        unchanged_count=counts["unchanged_count"],
    )


def execute_projection_push(
    prepared: PreparedProjectionPush,
    *,
    transport: httpx.BaseTransport | None = None,
    verify_tls: bool = True,
) -> ProjectionPushResult:
    """prepared request を **ちょうど 1 回** POST する。auto retry しない。"""

    try:
        with httpx.Client(
            transport=transport,
            verify=verify_tls,
            timeout=_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as client:
            response = client.request(
                prepared.method,
                prepared.url,
                content=prepared.body_bytes(),  # exact bytes; no json= reserialization
                headers=prepared.headers(),
            )
    except httpx.TimeoutException as exc:
        raise AffiliateProjectionPushError(
            "request timed out; the WordPress projection outcome is unknown, "
            "do not auto-retry"
        ) from exc
    except httpx.TransportError as exc:
        raise AffiliateProjectionPushError(
            "connection failed; the WordPress projection outcome is unknown, "
            "do not auto-retry"
        ) from exc
    except httpx.HTTPError as exc:  # pragma: no cover - httpx 内部の他エラー
        raise AffiliateProjectionPushError("request failed") from exc

    if response.is_redirect:
        raise AffiliateProjectionPushError(
            "projection endpoint returned a redirect"
        )

    if response.status_code != 200:
        code = _safe_server_code(response)
        raise AffiliateProjectionPushError(
            f"projection endpoint returned HTTP {response.status_code}"
            + (f" ({code})" if code else ""),
            http_status=response.status_code,
            server_code=code,
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise AffiliateProjectionPushError(
            "projection endpoint returned a non-JSON 200 response"
        ) from exc
    if not isinstance(data, dict):
        raise AffiliateProjectionPushError(
            "projection endpoint returned an unexpected 200 response shape"
        )
    return _validate_success(data, prepared)
