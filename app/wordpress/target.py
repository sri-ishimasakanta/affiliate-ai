"""WordPress ターゲットサイトの canonical 化と target-bound identity hash (pure)。

DB / network / 認証情報 非依存。3C-5C の ``request_identity_hash`` は base URL を
含まないため、外部 write の前に「どの WordPress 設置先か」も凍結する。
credential (username / app password) は一切扱わない・出力しない。
"""

from __future__ import annotations

import hashlib
from urllib.parse import urlsplit

from app.article.draft_input_canonical import canonical_json
from app.exceptions import WordPressTargetError

_ALLOWED_SCHEMES = ("http", "https")


def canonicalize_wordpress_base_url(raw: str) -> str:
    """WordPress の base URL を正規化する。

    - scheme は http / https のみ
    - userinfo (``user:pass@``) 禁止
    - query / fragment 禁止
    - hostname 必須
    - 末尾 ``/`` のみ除去 (正当なサブディレクトリ path は保持)
    - ``/wp-json`` 等の API path は base URL に含めない
    """

    if not isinstance(raw, str) or not raw.strip():
        raise WordPressTargetError("WORDPRESS_BASE_URL is empty")
    value = raw.strip()
    parts = urlsplit(value)

    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise WordPressTargetError(
            f"unsupported scheme {parts.scheme!r} (http/https only)"
        )
    if parts.username or parts.password or "@" in (parts.netloc or ""):
        raise WordPressTargetError("userinfo (user:pass@) is not allowed in base URL")
    if not parts.hostname:
        raise WordPressTargetError("hostname is required")
    if parts.query:
        raise WordPressTargetError("query string is not allowed in base URL")
    if parts.fragment:
        raise WordPressTargetError("fragment is not allowed in base URL")

    scheme = parts.scheme.lower()
    host = parts.hostname.lower()
    netloc = f"{host}:{parts.port}" if parts.port else host
    path = (parts.path or "").rstrip("/")
    if path.rstrip("/").lower().endswith("/wp-json"):
        raise WordPressTargetError("base URL must not include the /wp-json path")

    return f"{scheme}://{netloc}{path}"


def compute_target_request_identity_hash(
    *, request_identity_hash: str, target_base_url: str
) -> str:
    """already-approved request identity を exact な WordPress 設置先へ束縛する hash。

    credential / timestamp / run id / response / status lifecycle / idempotency key は
    含めない。
    """

    canonical = canonical_json(
        {
            "request_identity_hash": request_identity_hash,
            "target_base_url": target_base_url,
        }
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
