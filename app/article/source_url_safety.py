"""公式 Source URL の安全性検証 (pure)。

- https のみ / userinfo 禁止 / 長さ制限
- credential / secret 系 query は **reject** (除去して保存しない)
- tracking (utm_*, ref, aff, partner, clickid, gclid など) は安全に除去して canonicalize
- 既知の tracking / redirect ホストは公式 Source として reject
- 現在の AffiliateProgram.tracking_url そのもの (canonical 一致) も reject。
  ベンダーの公式ページと tracking URL が同じホストに同居することは普通にあるため、
  ホスト全体ではなく URL 単位で判定する (ホスト単位で弾くと、そのベンダーの公式
  料金ページを根拠として保存できなくなる)。
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_MAX_LEN = 1024

# 除去して保存してよい tracking パラメータ (キー完全一致 / 接頭辞)。
_TRACKING_PARAM_PREFIXES = ("utm_",)
_TRACKING_PARAM_KEYS = frozenset(
    {
        "ref",
        "aff",
        "affiliate",
        "partner",
        "partnerid",
        "clickid",
        "irclickid",
        "gclid",
        "fbclid",
        "mc_cid",
        "mc_eid",
        "cid",
    }
)

# 含まれていたら reject する credential 系パラメータ。
_CREDENTIAL_PARAM_KEYS = frozenset(
    {"token", "api_key", "apikey", "key", "secret", "password", "passwd", "access_token"}
)

# 既知の tracking / affiliate redirect ホスト (部分一致)。
_KNOWN_TRACKING_HOST_FRAGMENTS = (
    "pxf.io",
    "sjv.io",
    "s0i.co",
    "go.partnerstack.com",
    "impact.com",
    "prf.hn",
    "avantlink",
    "awin1.com",
    "linksynergy.com",
    "shareasale.com",
    "clickbank.net",
    "bit.ly",
    "trk.",
    "track.",
    "click.",
)


class UrlSafetyError(ValueError):
    """URL が公式 Source として不適 (credential 混入・非 https・tracking host など)。"""


def _is_tracking_key(key: str) -> bool:
    low = key.lower()
    return low in _TRACKING_PARAM_KEYS or any(
        low.startswith(p) for p in _TRACKING_PARAM_PREFIXES
    )


def _canonicalize(parts, query_pairs) -> str:
    kept = [(k, v) for k, v in query_pairs if not _is_tracking_key(k)]
    return urlunparse(
        (
            "https",
            (parts.netloc or "").lower(),
            parts.path or "/",
            parts.params,
            urlencode(kept, doseq=True),
            "",  # fragment は落とす
        )
    )


def canonicalize_tracking_url(url: str) -> str | None:
    """tracking URL を比較用の canonical 形にする。解釈できなければ ``None``。"""

    raw = (url or "").strip()
    if not raw:
        return None
    try:
        parts = urlparse(raw)
        if not parts.hostname:
            return None
        return _canonicalize(parts, parse_qsl(parts.query, keep_blank_values=True))
    except ValueError:
        return None


def validate_and_canonicalize(
    url: str, *, blocked_urls: frozenset[str] = frozenset()
) -> str:
    """安全なら canonical 化した URL を返す。危険なら :class:`UrlSafetyError`。

    ``blocked_urls`` は canonical 化済みの affiliate tracking URL 集合。
    """

    raw = (url or "").strip()
    if not raw or len(raw) > _MAX_LEN:
        raise UrlSafetyError("url is empty or too long")

    parts = urlparse(raw)
    if parts.scheme.lower() != "https":
        raise UrlSafetyError("only https URLs are allowed")
    if parts.username or parts.password or "@" in (parts.netloc or ""):
        raise UrlSafetyError("URL must not contain userinfo/credentials")
    host = (parts.hostname or "").lower()
    if not host:
        raise UrlSafetyError("URL has no host")

    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    for key, _value in query_pairs:
        if key.lower() in _CREDENTIAL_PARAM_KEYS:
            raise UrlSafetyError(f"URL query contains a credential parameter: {key}")

    if any(frag in host for frag in _KNOWN_TRACKING_HOST_FRAGMENTS):
        raise UrlSafetyError("host looks like a tracking / redirect host")

    canonical = _canonicalize(parts, query_pairs)
    if canonical in blocked_urls:
        raise UrlSafetyError("url is an affiliate tracking url")
    return canonical
