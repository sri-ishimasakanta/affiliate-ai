"""Affiliate redirect の opaque token 生成・形式検証 (pure)。

token は将来の ``/go/{token}`` の path segment。CSPRNG・128bit 以上・URL-safe・
非連番で、article id / program id / slug / provider / destination / hostname から
一切導出しない。DB ID を露出しない。
"""

from __future__ import annotations

import re
import secrets

# secrets.token_urlsafe(16) -> 128bit / 22 文字 / [A-Za-z0-9_-]。
_TOKEN_ENTROPY_BYTES = 16

# 受理する path segment の形 (存在確認はしない)。長さに将来の余地を持たせる。
TOKEN_PATTERN = re.compile(r"\A[A-Za-z0-9_-]{16,64}\Z")


def generate_token() -> str:
    """128bit CSPRNG の URL-safe な opaque token を返す。"""

    return secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)


def is_well_formed_token(value: object) -> bool:
    """``/go/{token}`` の path segment として受理可能な形か (DB 参照はしない)。"""

    return isinstance(value, str) and TOKEN_PATTERN.match(value) is not None
