"""Affiliate destination URL の安全性検証 (pure・**非改変**)。

Human が :attr:`AffiliateProgram.tracking_url` に入力した exact 文字列を検証するだけで、
一切書き換えない:

* canonical 化しない / query を並べ替えない / rebuild しない
* tracking / affiliate パラメータを除去しない
* percent-encoding を変えない
* fragment を落とさない / path を変えない

(:mod:`app.article.source_url_safety` は公式 Source 用で canonicalize する — 別方針。)

セキュリティ判定のためだけに ``destination_host`` を別途 IDNA-ascii / lowercase へ
正規化して返す。判定は必ず正規化済みホスト名の **完全一致** で行い、
``endswith`` / 部分一致 / prefix 一致は使わない。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

# AffiliateProgram.tracking_url は String(1024)。
_MAX_LEN = 1024

# 自サイト (open redirect / redirect loop / self-deal 防止)。正規化済み exact host。
SELF_HOSTS = frozenset({"bizfluxlab.com", "www.bizfluxlab.com"})

# https 以外は全拒否。代表的な危険スキームを明示的に持つ (メッセージ用)。
_KNOWN_UNSAFE_SCHEMES = frozenset(
    {"http", "javascript", "data", "file", "blob", "vbscript", "ftp", "mailto", "tel"}
)


class AffiliateDestinationError(ValueError):
    """affiliate destination URL が不適。メッセージに URL 本体は含めない。"""


@dataclass(frozen=True)
class DestinationFacts:
    """検証済み destination の事実。``destination_url`` は入力そのまま (無改変)。"""

    destination_url: str
    destination_host: str


def normalize_host(hostname: str | None) -> str:
    """parsed hostname を IDNA-ascii・小文字へ正規化する。安全に正規化できなければ raise。"""

    h = (hostname or "").strip()
    if not h:
        raise AffiliateDestinationError("destination URL has no host")
    if h.endswith("."):
        raise AffiliateDestinationError(
            "destination host must not end with a trailing dot"
        )
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in h):
        raise AffiliateDestinationError("destination host contains illegal characters")

    try:
        ascii_host = h.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        try:
            ascii_host = h.encode("ascii").decode("ascii")
        except UnicodeError:
            raise AffiliateDestinationError(
                "destination host cannot be normalized to a safe ASCII hostname"
            ) from None

    low = ascii_host.lower()
    if not low or "/" in low or "@" in low or ":" in low or " " in low:
        raise AffiliateDestinationError("destination host is invalid after normalization")
    return low


def validate_destination_url(raw: object) -> DestinationFacts:
    """VALIDATE ONLY。合格なら入力 exact 文字列 + 正規化 host を返す。書き換えない。"""

    if not isinstance(raw, str) or not raw:
        raise AffiliateDestinationError("destination URL is empty")
    if len(raw) > _MAX_LEN:
        raise AffiliateDestinationError("destination URL is too long")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        raise AffiliateDestinationError(
            "destination URL contains whitespace or control characters"
        )

    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme != "https":
        if scheme == "" or scheme in _KNOWN_UNSAFE_SCHEMES:
            raise AffiliateDestinationError(
                f"destination URL scheme must be https (got {scheme or 'none'})"
            )
        raise AffiliateDestinationError("destination URL scheme must be https")

    if parts.username or parts.password:
        raise AffiliateDestinationError("destination URL must not contain userinfo")
    if "@" in (parts.netloc or ""):
        raise AffiliateDestinationError("destination URL netloc must not contain '@'")
    if parts.hostname is None:
        raise AffiliateDestinationError("destination URL has no host")

    try:
        port = parts.port
    except ValueError:
        raise AffiliateDestinationError("destination URL has an invalid port") from None
    if port is not None and port != 443:
        raise AffiliateDestinationError("destination URL port must be absent or 443")

    host = normalize_host(parts.hostname)
    if host in SELF_HOSTS:
        raise AffiliateDestinationError("destination URL must not point back at this site")

    return DestinationFacts(destination_url=raw, destination_host=host)
