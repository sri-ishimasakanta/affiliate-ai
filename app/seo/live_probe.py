"""公開ページの **read-only** な技術的インデックス可能性の観測 (C5.1)。

1 URL につき HTTP GET を 1 回だけ行い、判定に必要な事実を取り出す。ページを
変更する手段は持たない (GET 以外のメソッドを出さない)。

取り出す事実:

- リダイレクトチェーンと最終 URL / 最終ステータス
- ``X-Robots-Tag`` レスポンスヘッダ
- ``<link rel="canonical">``
- ``<meta name="robots">`` (および ``googlebot``)
- ``<title>`` / ``<meta name="description">``
- ``<h1>`` の個数と最初のテキスト
- 本文らしさの粗い指標 (可読テキスト長)

HTML の解析は stdlib の :class:`html.parser.HTMLParser` で行う。判定 (healthy か
どうか) はこの module では行わない -- :mod:`app.seo.indexability` の責務。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

import httpx

_TIMEOUT_SECONDS = 30.0
_NOINDEX_RE = re.compile(r"\bnone\b|\bnoindex\b", re.IGNORECASE)
_TAG_RE = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_STRIP_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass
class _HeadFacts:
    canonical: str | None = None
    robots: str | None = None
    googlebot: str | None = None
    title: str | None = None
    description: str | None = None
    h1_count: int = 0
    h1_first: str | None = None


class _HeadParser(HTMLParser):
    """head のメタ情報と h1 を拾う最小のパーサ。未知のタグは無視する。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.facts = _HeadFacts()
        self._in_title = False
        self._in_h1 = False
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "link" and "canonical" in a.get("rel", "").lower():
            self.facts.canonical = a.get("href") or None
        elif tag == "meta":
            name = a.get("name", "").lower()
            if name == "robots":
                self.facts.robots = a.get("content")
            elif name == "googlebot":
                self.facts.googlebot = a.get("content")
            elif name == "description" and self.facts.description is None:
                self.facts.description = a.get("content")
        elif tag == "title" and self.facts.title is None:
            self._in_title = True
            self._buffer = []
        elif tag == "h1":
            self.facts.h1_count += 1
            self._in_h1 = self.facts.h1_first is None
            if self._in_h1:
                self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._in_title:
            self.facts.title = "".join(self._buffer).strip() or None
            self._in_title = False
        elif tag == "h1" and self._in_h1:
            self.facts.h1_first = "".join(self._buffer).strip() or None
            self._in_h1 = False

    def handle_data(self, data: str) -> None:
        if self._in_title or self._in_h1:
            self._buffer.append(data)


@dataclass(frozen=True)
class LivePageProbe:
    """1 URL の観測結果 (immutable)。判定は含まない。"""

    requested_url: str
    final_url: str | None
    final_status: int | None
    redirect_chain: tuple[tuple[int, str], ...] = ()
    x_robots_tag: str | None = None
    canonical: str | None = None
    robots_meta: str | None = None
    googlebot_meta: str | None = None
    title: str | None = None
    meta_description: str | None = None
    h1_count: int = 0
    h1_first: str | None = None
    text_length: int = 0
    error: str | None = None
    notes: tuple[str, ...] = field(default=())

    @property
    def redirected(self) -> bool:
        return bool(self.redirect_chain)

    @property
    def declares_noindex(self) -> bool:
        """robots meta / googlebot meta / X-Robots-Tag のいずれかが noindex か。"""

        for value in (self.robots_meta, self.googlebot_meta, self.x_robots_tag):
            if value and _NOINDEX_RE.search(value):
                return True
        return False


def visible_text_length(html: str) -> int:
    """script/style を除いた可読テキストのおおよその長さ。"""

    return len(_WS_RE.sub("", _STRIP_RE.sub("", _TAG_RE.sub("", html or ""))))


def probe_url(url: str, *, client: httpx.Client | None = None) -> LivePageProbe:
    """1 URL を GET して観測する。例外は投げず、失敗は ``error`` に載せる。"""

    owns_client = client is None
    http = client or httpx.Client(timeout=_TIMEOUT_SECONDS, follow_redirects=True)
    try:
        response = http.get(url, follow_redirects=True)
    except Exception as exc:  # noqa: BLE001 - 観測失敗そのものが結果
        if owns_client:
            http.close()
        return LivePageProbe(
            requested_url=url,
            final_url=None,
            final_status=None,
            error=type(exc).__name__,
        )
    try:
        chain = tuple((r.status_code, str(r.url)) for r in response.history)
        parser = _HeadParser()
        body = response.text if "html" in response.headers.get("content-type", "") else ""
        try:
            parser.feed(body)
        except Exception:  # noqa: BLE001 - 壊れた HTML でも観測は続ける
            pass
        return LivePageProbe(
            requested_url=url,
            final_url=str(response.url),
            final_status=response.status_code,
            redirect_chain=chain,
            x_robots_tag=response.headers.get("x-robots-tag"),
            canonical=parser.facts.canonical,
            robots_meta=parser.facts.robots,
            googlebot_meta=parser.facts.googlebot,
            title=parser.facts.title,
            meta_description=parser.facts.description,
            h1_count=parser.facts.h1_count,
            h1_first=parser.facts.h1_first,
            text_length=visible_text_length(body),
        )
    finally:
        if owns_client:
            http.close()
