"""Canonical Markdown → deterministic safe WordPress HTML (pure).

DB / network 非依存。``Article.body`` (Human 承認済み canonical Markdown) を
WordPress へ渡せる semantic HTML へ変換するだけ。source 由来の raw HTML は
すべてエスケープし、``<script>`` / ``<iframe>`` / event handler / ``javascript:``
を通さない。renderer が生成する固定要素 (mobile table wrapper) のみ HTML を許す。

renderer version: ``wordpress_html_v1``
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from html import escape as _html_escape

import mistune

RENDERER_VERSION = "wordpress_html_v1"

# renderer が生成する mobile 横スクロール wrapper (style 値は renderer 制御・source 非制御)。
_TABLE_WRAP_OPEN = '<div class="wp-table-scroll" style="overflow-x:auto">'
_TABLE_WRAP_CLOSE = "</div>"

_EXTERNAL_SCHEMES = ("http://", "https://")

# bare URL の終端。ASCII の空白・引用符・閉じ括弧、および日本語の約物・全角空白で
# 止める。``?`` ``&`` ``=`` ``#`` ``%`` 等はクエリ文字列として **許可** する (§16)。
_BARE_URL_RE = re.compile(
    r"https?://[^\s<>\"'`)\]}｜（）「」『』【】、。，．；：・…　]+"
)
_URL_TRAILING_TRIM = ".,;:!?"


def _bare_urls_to_markdown_links(markdown_text: str) -> str:
    """本文中の裸 URL を明示的な ``[url](url)`` へ変換する (renderer が anchor 化する)。

    既存の Markdown link ``](url)`` の内側は書き換えない。
    """

    def _repl(m: re.Match[str]) -> str:
        start = m.start()
        # 直前が "](" なら Markdown link target なのでそのまま。
        if start >= 2 and markdown_text[start - 2 : start] == "](":
            return m.group(0)
        url = m.group(0).rstrip(_URL_TRAILING_TRIM)
        tail = m.group(0)[len(url):]
        return f"[{url}]({url}){tail}"

    return _BARE_URL_RE.sub(_repl, markdown_text)


@dataclass
class RenderedWordPressContent:
    html: str
    html_hash: str
    renderer_version: str
    external_links: list[str] = field(default_factory=list)
    table_count: int = 0
    h1_count: int = 0
    h2_count: int = 0
    h3_count: int = 0
    image_count: int = 0


class _WordPressHTMLRenderer(mistune.HTMLRenderer):
    """source 由来 HTML をエスケープし、外部リンクに安全な rel/target を付ける。"""

    def __init__(self) -> None:
        super().__init__(escape=True)
        self.external_links: list[str] = []

    def link(self, text: str, url: str, title: str | None = None) -> str:
        low = url.strip().lower()
        # 危険スキームは href を無効化 (テキストのみ残す)。
        if low.startswith(("javascript:", "data:", "vbscript:")):
            return text
        href = _html_escape(url, quote=True)
        if low.startswith(_EXTERNAL_SCHEMES):
            self.external_links.append(url)
            # V1: 公式外部リンクには nofollow / sponsored を付けない (§9)。
            return (
                f'<a href="{href}" target="_blank" rel="noopener noreferrer">{text}</a>'
            )
        # 相対 / アンカー内部リンク: target を付けない。
        return f'<a href="{href}">{text}</a>'

    def heading(self, text: str, level: int, **attrs: object) -> str:
        # body から <h1> を絶対に出さない (post title が H1 を担当, §7)。
        lvl = max(2, min(level, 6))
        return f"<h{lvl}>{text}</h{lvl}>\n"

    def image(self, text: str, url: str, title: str | None = None) -> str:
        # V1 は本文画像なし。<img> を生成せず alt テキストのみ残す (§20)。
        return _html_escape(text)

    def block_html(self, html: str) -> str:
        return f"<p>{_html_escape(html.strip())}</p>\n"

    def inline_html(self, html: str) -> str:
        return _html_escape(html)


def _wrap_tables(html: str) -> tuple[str, int]:
    count = 0

    def _repl(m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{_TABLE_WRAP_OPEN}{m.group(0)}{_TABLE_WRAP_CLOSE}"

    wrapped = re.sub(r"<table>.*?</table>", _repl, html, flags=re.DOTALL)
    return wrapped, count


def render_wordpress_html(markdown_text: str) -> RenderedWordPressContent:
    """canonical Markdown を deterministic safe HTML へ変換する。"""

    renderer = _WordPressHTMLRenderer()
    # 裸 URL は自前で明示 link 化する (mistune の url plugin は日本語約物を
    # URL に取り込んでしまうため使わない)。
    md = mistune.create_markdown(renderer=renderer, plugins=["table"])
    body_html = md(_bare_urls_to_markdown_links(markdown_text))
    body_html, table_count = _wrap_tables(body_html)
    html = body_html.strip() + "\n"

    h1 = len(re.findall(r"<h1[ >]", html))
    h2 = len(re.findall(r"<h2[ >]", html))
    h3 = len(re.findall(r"<h3[ >]", html))
    images = len(re.findall(r"<img[ >]", html))

    return RenderedWordPressContent(
        html=html,
        html_hash=hashlib.sha256(html.encode("utf-8")).hexdigest(),
        renderer_version=RENDERER_VERSION,
        external_links=list(renderer.external_links),
        table_count=table_count,
        h1_count=h1,
        h2_count=h2,
        h3_count=h3,
        image_count=images,
    )
