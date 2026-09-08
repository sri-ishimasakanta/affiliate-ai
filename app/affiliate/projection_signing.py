"""Affiliate target projection push の HMAC-SHA256 リクエスト署名 (pure)。

将来 affiliate-ai が WordPress MU-plugin の projection endpoint へ POST する際の
署名/検証を deterministic に定義する。ここには **HTTP client も secret も無い**。
共有 secret は将来 ``wp-config.php`` + affiliate-ai ``.env`` に置く (コミットしない)。
secret は署名文字列にも payload にも入れない。
"""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_SCHEME = "v1"

# 将来 affiliate-ai が POST する WordPress MU-plugin の projection endpoint。
# 管理用途のみ。未認証の mutation として到達不能でなければならない (D-C で実装)。
PROJECTION_ENDPOINT_METHOD = "POST"
PROJECTION_ENDPOINT_PATH = "/wp-json/affiliate-ai/v1/target-projections"

HEADER_TIMESTAMP = "X-BFL-Timestamp"
HEADER_CONTENT_SHA256 = "X-BFL-Content-SHA256"
HEADER_SIGNATURE = "X-BFL-Signature"

# 許容する clock skew (秒)。将来の WordPress endpoint はこの窓外の timestamp を拒否する。
DEFAULT_MAX_SKEW_SECONDS = 300


def body_sha256(body: bytes) -> str:
    """送信する **exact な UTF-8 バイト列** の SHA-256 hex (lowercase)。

    ``projection_snapshot_hash`` (意味的 artifact identity) とは別物 (transport 完全性)。
    """

    if not isinstance(body, bytes | bytearray):
        raise TypeError("body must be bytes")
    return hashlib.sha256(bytes(body)).hexdigest()


def canonical_request_target(base_path: str, params: dict[str, str | int]) -> str:
    """署名対象に使う deterministic な request target (``path?key=v&key=v``)。

    query パラメータをキー昇順で並べ、値は文字列化して連結する。cursor
    (``since_id`` 等) を署名文字列に確実に束縛するために使う。呼び出し側は
    **実効値 (default 適用後)** を渡し、server は同じ規則で再構築して検証する。
    空 params なら ``base_path`` をそのまま返す。
    """

    if not params:
        return base_path
    ordered = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return f"{base_path}?{ordered}"


def build_signing_string(
    *, method: str, path: str, timestamp: int, body_sha256_hex: str
) -> str:
    """署名対象の canonical signing string。

    scheme / version, HTTP method, exact REST path, unix timestamp, body SHA-256 を束縛。
    """

    return "\n".join(
        [
            SIGNATURE_SCHEME,
            method.upper(),
            path,
            str(int(timestamp)),
            body_sha256_hex.lower(),
        ]
    )


def compute_signature(
    *,
    shared_secret: str,
    method: str,
    path: str,
    timestamp: int,
    body_sha256_hex: str,
) -> str:
    """HMAC-SHA256(shared_secret, signing_string) の lowercase hex。"""

    msg = build_signing_string(
        method=method,
        path=path,
        timestamp=timestamp,
        body_sha256_hex=body_sha256_hex,
    )
    return hmac.new(
        shared_secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_signature(
    *,
    shared_secret: str,
    method: str,
    path: str,
    timestamp: int,
    body_sha256_hex: str,
    provided_signature: str,
) -> bool:
    """定数時間比較で署名を検証する。"""

    expected = compute_signature(
        shared_secret=shared_secret,
        method=method,
        path=path,
        timestamp=timestamp,
        body_sha256_hex=body_sha256_hex,
    )
    return hmac.compare_digest(expected, (provided_signature or "").lower())


def timestamp_within_window(
    *, timestamp: int, now: int, max_skew_seconds: int = DEFAULT_MAX_SKEW_SECONDS
) -> bool:
    return abs(int(now) - int(timestamp)) <= max_skew_seconds


def verify_request(
    *,
    shared_secret: str,
    method: str,
    path: str,
    timestamp: int,
    now: int,
    body: bytes,
    provided_content_sha256: str,
    provided_signature: str,
    max_skew_seconds: int = DEFAULT_MAX_SKEW_SECONDS,
) -> tuple[bool, str]:
    """将来の WordPress endpoint 相当の検証を pure に再現する。

    戻り値 ``(ok, reason)``。reason は ``ok`` / ``content_sha256_mismatch`` /
    ``timestamp_outside_window`` / ``signature_mismatch``。
    """

    computed = body_sha256(body)
    if not hmac.compare_digest(computed, (provided_content_sha256 or "").lower()):
        return False, "content_sha256_mismatch"
    if not timestamp_within_window(
        timestamp=timestamp, now=now, max_skew_seconds=max_skew_seconds
    ):
        return False, "timestamp_outside_window"
    if not verify_signature(
        shared_secret=shared_secret,
        method=method,
        path=path,
        timestamp=timestamp,
        body_sha256_hex=computed,
        provided_signature=provided_signature,
    ):
        return False, "signature_mismatch"
    return True, "ok"
