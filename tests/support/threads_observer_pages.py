"""T6.5B の試験用: Threads の Web 画面に似せた HTML と、読むだけの偽のページ。

形は 2026-09-28 のパイロットで確かめた本物の画面の入れ子に合わせてある
(``threads-web-verified-2026-09-28-v1``。本物をもとにした合成の fixture は
``tests/fixtures/threads_observer/``)。画面が変わったら selectors と一緒にここも直す。
"""

from __future__ import annotations

from html import escape
from pathlib import Path

from app.social.threads.observer.driver import check_url

LIKE, REPLY, REPOST, SHARE = "「いいね！」", "返信", "再投稿", "シェアする"


def metric(label: str, value: str | None) -> str:
    """本物の形: svg の ``title`` 属性 + 同じボタンの中の ``span[dir=auto]`` の数 (0 は空)。"""

    number = f"<div><span>{escape(value)}</span></div>" if value is not None else ""
    return (f'<div role="button" tabindex="0"><div><div><svg role="img" title="{label}">'
            f'<title>{label}</title></svg><span dir="auto">{number}</span>'
            "</div></div></div>")  # fmt: skip


def card(
    handle: str,
    code: str,
    body: str,
    *,
    likes: str | None = "0",
    replies: str | None = "0",
    reposts: str | None = "0",
    shares: str | None = "0",
    posted: str | None = "2026-09-27T01:00:00.000Z",
    topic: str | None = None,
    image: bool = False,
    video: bool = False,
    link: str | None = None,
    inner: str = "",
    thread_marker: str | None = None,
    pinned: bool = False,
    continuation: str | None = None,
    stats_card: bool = False,
    meta_ai: bool = False,
    inline_gifs: int = 0,
    image_src: str = "https://cdn.example/x.jpg",
    media_link: bool = False,
    link_preview_image: bool = False,
) -> str:
    """``thread_marker="1/2"``: 本物の形の続きの投稿の印を、最後の行の本文の span に入れる。

    2026-09-29 に見た形 (T6.5B.3a):
    ``pinned`` = 見出しの上の「ピン留め済み」の行・``continuation`` = 指標の列の下の
    「他1件を見る」の行・``stats_card`` = 本文の後の本物の ``<button>`` の閲覧数のカード・
    ``meta_ai`` = 最初の行の先頭のアイコンつき ``/@meta.ai`` の札・``inline_gifs`` = 最初の行の
    中の GIF のスタンプ・``media_link`` = 添付の画像を ``/post/<code>/media`` のリンクで包む・
    ``link_preview_image`` = 外のリンクの見出しの画像。
    """

    parts = body.split("\n")
    marker = ""
    if thread_marker:
        first, second = thread_marker.split("/")
        marker = (f"<div><div><span>{first}</span><div><span>/</span></div>"
                  f"<span>{second}</span></div></div>")  # fmt: skip
    label = ('<div><span><div><a href="/@meta.ai" role="link" tabindex="0"><span>'
             '<svg aria-hidden="true"></svg>meta.ai</span></a></div></span></div>'
             if meta_ai else "")  # fmt: skip
    gifs = "".join(f'<img alt="" aria-hidden="true" height="24" src="https://media1.giphy.com/{n}.gif">'
                   for n in range(inline_gifs))  # fmt: skip
    lines = "".join(
        f'<span dir="auto">{label if i == 0 else ""}<span>{escape(line)}</span>'
        f'{gifs if i == 0 else ""}{marker if i == len(parts) - 1 else ""}</span>'
        for i, line in enumerate(parts)
    )
    time_html = f'<time datetime="{posted}" title="2026年9月27日">1日</time>' if posted else ""
    topic_html = (
        f'<a href="/search?q={escape(topic)}&amp;serp_type=tags"><span>{escape(topic)}</span></a>'
        if topic else ""
    )  # fmt: skip
    media = ""
    if image:
        # 本物の形: 添付の画像は、画像を開くボタンの中の <picture> (2026-09-29 に確認)。
        picture = (f'<picture><img alt="写真の説明はありません。" src="{escape(image_src)}">'
                   "</picture>")  # fmt: skip
        if media_link:
            picture = f'<a href="/@{handle}/post/{code}/media" role="link">{picture}</a>'
        media += f'<div><div role="button"><div>{picture}</div></div></div>'
    if stats_card:
        media += ('<div><button><div><div><img alt="" aria-hidden="true" src="https://cdn.example/'
                  'card.jpg"></div><div><span dir="auto">閲覧数</span><span dir="auto"><span>'
                  '99万</span></span><div><span dir="auto">30日</span></div><span dir="auto">'
                  "08/30 - 2026/09/28</span></div></div></button></div>")  # fmt: skip
    if link_preview_image:
        media += ('<div><a href="https://l.threads.com/?u=https%3A%2F%2Fexample.invalid" '
                  'role="link">'
                  '<div><img alt="" src="https://cdn.example/preview.jpg"></div>'
                  "<span>example.invalid</span></a></div>")  # fmt: skip
    if video:
        media += "<video></video>"
    link_html = (f'<a href="https://l.threads.com/?u={escape(link)}"><span>{escape(link)}</span></a>'
                 if link else "")  # fmt: skip
    pinned_html = ('<div><div><div><svg aria-label=""></svg></div><div><span dir="auto">'
                   "ピン留め済み</span></div></div></div>") if pinned else ""  # fmt: skip
    continuation_html = (
        f'<div><div><img alt="{handle}のプロフィール写真" src="p.jpg"></div>'
        f'<span dir="auto"><span>{escape(continuation)}</span></span></div>'
    ) if continuation else ""  # fmt: skip
    return (
        f'<div data-pressable-container="true">{pinned_html}<div>'
        f'<a href="/@{handle}"><img alt="{handle}のプロフィール写真" src="p.jpg"></a>'
        f'<a href="/@{handle}"><span dir="auto">{handle}</span></a>'
        f'{topic_html}<span dir="auto"><a href="/@{handle}/post/{code}">{time_html}</a></span>'
        "</div>"
        f"<div>{lines}</div>{media}{link_html}{inner}"
        f"<div>{metric(LIKE, likes)}{metric(REPLY, replies)}{metric(REPOST, reposts)}"
        f"{metric(SHARE, shares)}</div>{continuation_html}</div>"
    )


def layout_of(*boxes: tuple[str, str, float, float], height: float = 1000) -> dict:
    """``(handle, code, top, bottom)`` の並び → ページが返す位置の形 (画面の中の座標)。"""

    return {"viewport": {"width": 1280, "height": height},
            "cards": [{"top": top, "bottom": bottom, "hrefs": [f"/@{handle}/post/{code}"]}
                      for handle, code, top, bottom in boxes]}  # fmt: skip


def page(*cards: str) -> str:
    return f"<html><body><main>{''.join(cards)}</main></body></html>"


def login_page() -> str:
    return ('<html><body><main><h1>Threads</h1><a href="/login?show_choice_screen=false">'
            "ログイン</a></main></body></html>")  # fmt: skip


def drifted_page() -> str:
    """画面の形が変わった (投稿のまとまりの印が無い)。"""

    return ('<html><body><main><article><a href="/@alice/post/ZZZ1">x</a>'
            "<p>本文</p></article></main></body></html>")  # fmt: skip


SUGGESTION_SERP = "search_nullstate_topic_for_you"


def suggestion_url(topic: str) -> str:
    """一覧のトピックのリンク先 (2026-09-28 に画面で見た形)。"""

    from urllib.parse import quote

    from app.social.threads.observer import selectors as sel

    return f"{sel.BASE_URL}/search?q={quote(topic)}&serp_type={SUGGESTION_SERP}"


def trends_page(*topics: str, sidebar: bool = True) -> str:
    """検索の最初の画面: 左のメニューのコミュニティ (一覧ではない) + おすすめのトピックのリンク。"""

    from urllib.parse import quote

    nav = ('<a role="link" href="/search?q=aithreads&amp;serp_type=tags&amp;tag_id=1">'
           "<span>AI Threads</span></a>") if sidebar else ""  # fmt: skip
    links = "".join(
        f'<a role="link" href="/search?q={quote(t)}&amp;serp_type={SUGGESTION_SERP}">'
        f"<span>{escape(t)}</span></a>"
        for t in topics
    )
    return f"<html><body><main>{nav}<div>{links}</div></main></body></html>"


class FakePage:
    """読むだけの偽のページ。URL ごとに、スクロールのたびに次の HTML を返す。

    押す・書く操作は持たない (本物の ``PlaywrightPage`` と同じ)。開いた URL は本物と同じく
    ``check_url`` を通す。``layouts`` (T6.5B.4): URL ごとに、HTML と同じ順の投稿のまとまりの位置
    (``layout_of``)。無ければ ``layout()`` は ``None`` (位置が読めないページ)。
    """

    def __init__(self, pages: dict[str, list[str] | str], *, default: str | None = None,
                 layouts: dict[str, list[dict] | dict] | None = None) -> None:  # fmt: skip
        self.pages = {k: ([v] if isinstance(v, str) else list(v)) for k, v in pages.items()}
        self.layouts = {k: ([v] if isinstance(v, dict) else list(v))
                        for k, v in (layouts or {}).items()}  # fmt: skip
        self.default = default
        self.visited: list[str] = []
        self.scrolls = 0
        self.screenshots: list[Path] = []
        self.closed = False
        self.waits = 0
        #: True なら「待つ」たびに次の HTML を返す (描いた後の差し込みの再現)。
        self.advance_on_wait = False
        self._url: str | None = None
        self._index = 0

    def goto(self, url: str) -> None:
        check_url(url)
        self.visited.append(url)
        self._url = url
        self._index = 0

    def scroll(self) -> None:
        self.scrolls += 1
        self._index += 1

    def wait(self, ms: int) -> None:
        self.waits += 1
        if self.advance_on_wait:
            self._index += 1

    def content(self) -> str:
        frames = self.pages.get(self._url or "")
        if frames is None:
            if self.default is None:
                raise AssertionError(f"unexpected url {self._url}")
            return self.default
        return frames[min(self._index, len(frames) - 1)]

    def screenshot(self, path: Path) -> None:
        self.screenshots.append(path)

    def layout(self) -> dict | None:
        frames = self.layouts.get(self._url or "")
        if not frames:
            return None
        return frames[min(self._index, len(frames) - 1)]

    def close(self) -> None:
        self.closed = True
