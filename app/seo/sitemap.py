"""XML sitemap の **read-only** な取得と展開 (C5.1)。

sitemap の場所は **推測しない** -- robots.txt の ``Sitemap:`` 宣言を第一の情報源
とし、見つからないときだけ WordPress 既定の ``/wp-sitemap.xml`` を試す。

sitemap index を 1 段だけ展開する (WordPress core の構造は index -> 子 sitemap)。
ネストした index を無限に辿ることはしない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

import httpx

from app.seo.robots_rules import parse_robots_txt

_TIMEOUT_SECONDS = 30.0
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_LASTMOD_RE = re.compile(
    r"<url>(?:(?!</url>).)*?<loc>\s*([^<\s]+)\s*</loc>(?:(?!</url>).)*?"
    r"<lastmod>\s*([^<\s]+)\s*</lastmod>",
    re.IGNORECASE | re.DOTALL,
)
_WORDPRESS_DEFAULT_SITEMAP = "/wp-sitemap.xml"


@dataclass(frozen=True)
class SitemapDocument:
    url: str
    status: int | None
    is_index: bool
    locs: tuple[str, ...] = ()
    error: str | None = None


@dataclass
class SitemapInventory:
    """サイト全体の sitemap 構造と、そこに載っている URL。"""

    entry_point: str | None = None
    entry_point_source: str | None = None
    documents: list[SitemapDocument] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    lastmod: dict[str, str] = field(default_factory=dict)
    robots_sitemaps: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _fetch(client: httpx.Client, url: str) -> tuple[int | None, str, str | None]:
    try:
        response = client.get(url, follow_redirects=True)
    except Exception as exc:  # noqa: BLE001 - 取得失敗そのものが結果
        return None, "", type(exc).__name__
    return response.status_code, response.text, None


def _read_document(client: httpx.Client, url: str) -> SitemapDocument:
    status, text, error = _fetch(client, url)
    if error is not None or status != 200:
        return SitemapDocument(url=url, status=status, is_index=False, error=error)
    locs = tuple(_LOC_RE.findall(text))
    return SitemapDocument(
        url=url,
        status=status,
        is_index="<sitemapindex" in text.lower(),
        locs=locs,
    )


def _lastmods(client: httpx.Client, url: str) -> dict[str, str]:
    status, text, error = _fetch(client, url)
    if error is not None or status != 200:
        return {}
    return {loc: mod for loc, mod in _LASTMOD_RE.findall(text)}


def discover_sitemaps(
    site_base_url: str, *, client: httpx.Client | None = None
) -> SitemapInventory:
    """robots.txt から sitemap を発見し、index を 1 段展開して URL を集める。"""

    owns_client = client is None
    http = client or httpx.Client(timeout=_TIMEOUT_SECONDS, follow_redirects=True)
    inventory = SitemapInventory()
    try:
        base = site_base_url.rstrip("/") + "/"
        status, text, error = _fetch(http, urljoin(base, "/robots.txt"))
        if error is None and status == 200:
            inventory.robots_sitemaps = list(parse_robots_txt(text).sitemaps)
        if inventory.robots_sitemaps:
            inventory.entry_point = inventory.robots_sitemaps[0]
            inventory.entry_point_source = "robots.txt"
        else:
            inventory.entry_point = urljoin(base, _WORDPRESS_DEFAULT_SITEMAP)
            inventory.entry_point_source = "wordpress default (robots.txt declared none)"

        root = _read_document(http, inventory.entry_point)
        inventory.documents.append(root)
        if root.error is not None or root.status != 200:
            inventory.errors.append(f"sitemap entry point unreadable: {inventory.entry_point}")
            return inventory

        if root.is_index:
            for child_url in root.locs:
                child = _read_document(http, child_url)
                inventory.documents.append(child)
                if child.error is not None or child.status != 200:
                    inventory.errors.append(f"child sitemap unreadable: {child_url}")
                    continue
                inventory.urls.extend(child.locs)
                inventory.lastmod.update(_lastmods(http, child_url))
        else:
            inventory.urls.extend(root.locs)
            inventory.lastmod.update(_lastmods(http, inventory.entry_point))
        return inventory
    finally:
        if owns_client:
            http.close()
