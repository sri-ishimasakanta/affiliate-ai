"""公開中継 (WordPress MU-plugin) との HTTP client (C8.8)。

**新しい認証方式は作らない。** 既存の affiliate runtime と同じ HMAC-SHA256 契約
(:mod:`app.affiliate.projection_signing`) をそのまま使う。共有 secret は
``wp-config.php`` と ``.env`` にだけ置き、DB にもログにも載せない。

この client がやり取りするのは 4 つだけ:

- ``create``   -- 中継にレビューセッションを 1 つ作る (認証必須)
- ``pending``  -- 人が下した決定を取りに行く (認証必須)
- ``ack``      -- 反映済みとして消費を通知する (認証必須)
- ``revoke``   -- セッションを失効させる (認証必須)

公開側のレビューページと決定 POST は **人のブラウザ** が使う経路であって、この
client は通らない。

capability は ``create`` の応答として **返ってこない**。ローカルで生成し、digest
だけを中継へ渡す。生の値がこの経路を往復することはない。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.affiliate.projection_signing import (
    HEADER_CONTENT_SHA256,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    body_sha256,
    compute_signature,
)
from app.affiliate.runtime_http import known_server_code, require_https_origin

ROUTE_SESSIONS = "/wp-json/affiliate-ai/v1/approval-sessions"
ROUTE_PENDING = "/wp-json/affiliate-ai/v1/approval-decisions"
ROUTE_ACK = "/wp-json/affiliate-ai/v1/approval-decisions/ack"
ROUTE_REVOKE = "/wp-json/affiliate-ai/v1/approval-sessions/revoke"

#: 中継が返しうる既知のエラーコード (raw body は決して読まない)。
KNOWN_ERROR_CODES = frozenset(
    {
        "secret_not_configured",
        "signature_mismatch",
        "content_sha256_mismatch",
        "timestamp_outside_window",
        "invalid_payload",
        "session_not_found",
        "session_not_pending",
        "session_expired",
        "already_decided",
        "already_consumed",
    }
)

_TIMEOUT_SECONDS = 20.0


class RelayError(Exception):
    """中継とのやり取りの失敗。**capability も secret も含めない。**"""

    def __init__(self, reason: str) -> None:
        super().__init__(f"approval relay error: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class RelayDecision:
    """中継が保持している、人が下した決定 1 件。"""

    relay_session_id: str
    subject_type: str
    subject_id: int
    subject_hash: str
    subject_version: int
    decision: str
    decision_reason: str | None
    decided_at: str

    @classmethod
    def from_payload(cls, payload: dict) -> RelayDecision:
        try:
            return cls(
                relay_session_id=str(payload["relay_session_id"]),
                subject_type=str(payload["subject_type"]),
                subject_id=int(payload["subject_id"]),
                subject_hash=str(payload["subject_hash"]),
                subject_version=int(payload["subject_version"]),
                decision=str(payload["decision"]),
                decision_reason=(
                    str(payload["decision_reason"])
                    if payload.get("decision_reason") not in (None, "")
                    else None
                ),
                decided_at=str(payload["decided_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RelayError(f"malformed decision envelope: {type(exc).__name__}") from None


class ApprovalRelayClient:
    def __init__(self, settings, *, http_client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._http = http_client

    # -- public ---------------------------------------------------------------
    def create_session(
        self,
        *,
        relay_session_id: str,
        subject_type: str,
        subject_id: int,
        subject_hash: str,
        subject_version: int,
        capability_digest: str,
        expires_at: str,
        snapshot: dict,
    ) -> dict:
        """中継にレビューセッションを作る。**生の capability は送らない。**"""

        return self._post(
            ROUTE_SESSIONS,
            {
                "relay_session_id": relay_session_id,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "subject_hash": subject_hash,
                "subject_version": subject_version,
                "capability_digest": capability_digest,
                "expires_at": expires_at,
                "snapshot": snapshot,
            },
        )

    def fetch_decisions(self) -> list[RelayDecision]:
        """人が下した決定を取りに行く (read-only)。"""

        payload = self._get(ROUTE_PENDING)
        items = payload.get("decisions") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise RelayError("malformed decision list")
        return [RelayDecision.from_payload(item) for item in items]

    def acknowledge(self, *, relay_session_id: str, local_outcome: str) -> dict:
        """反映済みとして消費を通知する (冪等)。"""

        return self._post(
            ROUTE_ACK,
            {"relay_session_id": relay_session_id, "local_outcome": local_outcome},
        )

    def revoke(self, *, relay_session_id: str, reason: str) -> dict:
        return self._post(ROUTE_REVOKE, {"relay_session_id": relay_session_id, "reason": reason})

    # -- internals ------------------------------------------------------------
    def _secret(self) -> str:
        secret = getattr(self._settings, "affiliate_runtime_shared_secret", None) or ""
        if not secret.strip():
            raise RelayError("the shared relay secret is not configured")
        return secret

    def _origin(self) -> str:
        return require_https_origin(
            getattr(self._settings, "wordpress_base_url", "") or "", error_cls=RelayError
        )

    def _headers(self, *, method: str, path: str, body: bytes) -> dict[str, str]:
        timestamp = int(time.time())
        digest = body_sha256(body)
        signature = compute_signature(
            shared_secret=self._secret(),
            method=method,
            path=path,
            timestamp=timestamp,
            body_sha256_hex=digest,
        )
        return {
            HEADER_TIMESTAMP: str(timestamp),
            HEADER_CONTENT_SHA256: digest,
            HEADER_SIGNATURE: signature,
            "Content-Type": "application/json",
        }

    def _post(self, path: str, payload: dict[str, Any]) -> dict:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self._request("POST", path, body=body)

    def _get(self, path: str) -> dict:
        return self._request("GET", path, body=b"")

    def _request(self, method: str, path: str, *, body: bytes) -> dict:
        url = f"{self._origin()}{path}"
        headers = self._headers(method=method, path=path, body=body)
        owns = self._http is None
        http = self._http or httpx.Client(timeout=_TIMEOUT_SECONDS)
        try:
            response = http.request(method, url, content=body or None, headers=headers)
        except Exception as exc:  # noqa: BLE001 - 応答本文は載せない
            raise RelayError(f"transport failure: {type(exc).__name__}") from None
        finally:
            if owns:
                http.close()

        if response.status_code >= 400:
            code = known_server_code(response, KNOWN_ERROR_CODES)
            raise RelayError(code or f"HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise RelayError("malformed relay response") from None
        return data if isinstance(data, dict) else {}


def build_review_url(settings, *, relay_session_id: str, capability: str) -> str:
    """レビュー URL。**capability は fragment に置く**。

    fragment は HTTP リクエストに送られないので、アクセスログにも Referer にも
    残らない。path に入るのはセッション識別子だけで、これ自体は secret ではない。
    """

    origin = require_https_origin(
        getattr(settings, "wordpress_base_url", "") or "", error_cls=RelayError
    )
    return f"{origin}/bfl-approval/{relay_session_id}#{capability}"


def redact_review_url(url: str) -> str:
    """ログ/履歴へ出す前に fragment を落とす。"""

    return (url or "").split("#", 1)[0] + "#[redacted-capability]" if "#" in (url or "") else url
