"""プロジェクト状態の報告から秘密を落とす最終防壁 (pure)。

既存の 2 つの仕組みを重ねて使う (作り直さない):

- :func:`app.operations.notifications.sanitize_payload`: 秘密になりうるキー (token / secret /
  password / credential / authorization / api_key / webhook / smtp など) を丸ごと伏せ、
  文字列の中の ``/go/`` の tracking URL を伏せる。
- :func:`app.social.threads.errors.redact`: access_token などのクエリ・JSON・Bearer を伏せる。

ここでは値の形でさらに伏せる: URL や DSN に埋め込まれた ``user:password@``、Bearer / Basic の
ヘッダ値、Meta / Threads の access token らしい長い値。SHA-256 などの 16 進の digest は
報告の根拠として使うので伏せない。
"""

from __future__ import annotations

import re
from typing import Any

from app.operations.notifications import sanitize_payload
from app.social.threads.errors import redact as redact_tokens

REDACTED = "[redacted]"
_EXTRA_KEY_PARTS = ("cookie", "dsn", "app_password", "session_key", "passphrase", "signing_key")
_VALUE_PATTERNS = (
    # scheme://user:password@host → scheme://[redacted]@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1" + REDACTED + "@"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}"), r"\1 " + REDACTED),
    # Meta / Threads の access token (EAA… / THAA… / IGQV… の長い英数字)
    (re.compile(r"\b(?:EAA|THAA|IGQV)[A-Za-z0-9_\-]{20,}"), REDACTED),
)


def redact_text(value: str) -> str:
    out = redact_tokens(value)
    for pattern, replacement in _VALUE_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def _walk(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if any(part in str(key).lower() for part in _EXTRA_KEY_PARTS):
                out[str(key)] = REDACTED
            else:
                out[str(key)] = _walk(item)
        return out
    if isinstance(value, list | tuple):
        return [_walk(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact(report: Any) -> Any:
    """報告の全体に最終防壁をかける (キー → 値の順)。"""

    return _walk(sanitize_payload(report))
