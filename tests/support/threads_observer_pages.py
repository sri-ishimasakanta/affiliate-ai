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
) -> str:
    lines = "".join(f'<span dir="auto"><span>{escape(line)}</span></span>'
                    for line in body.split("\n"))  # fmt: skip
    time_html = f'<time datetime="{posted}" title="2026年9月27日">1日</time>' if posted else ""
    topic_html = (
        f'<a href="/search?q={escape(topic)}&amp;serp_type=tags"><span>{escape(topic)}</span></a>'
        if topic else ""
    )  # fmt: skip
    media = ""
    if image:
        media += '<img alt="写真の説明はありません。" src="https://cdn.example/x.jpg">'
    if video:
        media += "<video></video>"
    link_html = (f'<a href="https://l.threads.com/?u={escape(link)}"><span>{escape(link)}</span></a>'
                 if link else "")  # fmt: skip
    return (
        '<div data-pressable-container="true"><div>'
        f'<a href="/@{handle}"><img alt="{handle}のプロフィール写真" src="p.jpg"></a>'
        f'<a href="/@{handle}"><span dir="auto">{handle}</span></a>'
        f'{topic_html}<span dir="auto"><a href="/@{handle}/post/{code}">{time_html}</a></span>'
        "</div>"
        f"<div>{lines}</div>{media}{link_html}{inner}"
        f"<div>{metric(LIKE, likes)}{metric(REPLY, replies)}{metric(REPOST, reposts)}"
        f"{metric(SHARE, shares)}</div></div>"
    )


def page(*cards: str) -> str:
    return f"<html><body><main>{''.join(cards)}</main></body></html>"


def login_page() -> str:
    return ('<html><body><main><h1>Threads</h1><a href="/login?show_choice_screen=false">'
            "ログイン</a></main></body></html>")  # fmt: skip


def drifted_page() -> str:
    """画面の形が変わった (投稿のまとまりの印が無い)。"""

    return ('<html><body><main><article><a href="/@alice/post/ZZZ1">x</a>'
            "<p>本文</p></article></main></body></html>")  # fmt: skip


def trends_page(*topics: str) -> str:
    links = "".join(f'<a href="/search?q={escape(t)}&amp;serp_type=tags"><span>{escape(t)}</span>'
                    "</a>" for t in topics)  # fmt: skip
    return f"<html><body><main>{links}</main></body></html>"


class FakePage:
    """読むだけの偽のページ。URL ごとに、スクロールのたびに次の HTML を返す。

    押す・書く操作は持たない (本物の ``PlaywrightPage`` と同じ)。開いた URL は本物と同じく
    ``check_url`` を通す。
    """

    def __init__(self, pages: dict[str, list[str] | str], *, default: str | None = None) -> None:
        self.pages = {k: ([v] if isinstance(v, str) else list(v)) for k, v in pages.items()}
        self.default = default
        self.visited: list[str] = []
        self.scrolls = 0
        self.screenshots: list[Path] = []
        self.closed = False
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

    def content(self) -> str:
        frames = self.pages.get(self._url or "")
        if frames is None:
            if self.default is None:
                raise AssertionError(f"unexpected url {self._url}")
            return self.default
        return frames[min(self._index, len(frames) - 1)]

    def screenshot(self, path: Path) -> None:
        self.screenshots.append(path)

    def close(self) -> None:
        self.closed = True
