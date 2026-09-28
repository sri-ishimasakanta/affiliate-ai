"""観察したページの HTML → 外の投稿の記録 (決定的・読むだけ)。

- 期待する形 (``selectors``) が見つからなければ、ページ全体を ``dom_unrecognized`` として止め、
  何も保存させない (値を読み替えない)。
- ログインの画面なら ``login_required`` (人がログインする)。
- 1 件の投稿の形が崩れていれば、その投稿だけを捨てる (数える)。半分以上が崩れていれば、
  ページ全体を ``dom_unrecognized`` にする。
- 見えない指標は ``None`` (0 にしない)。外の表示回数は読まない (見えない)。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime

from app.social.threads.features import extract
from app.social.threads.observer import selectors as sel
from app.social.threads.observer.dom import Node, parse_html

PAGE_OK = "ok"
PAGE_LOGIN_REQUIRED = "login_required"
PAGE_DOM_UNRECOGNIZED = "dom_unrecognized"
PAGE_EMPTY = "empty"

_PERMALINK = re.compile(sel.PERMALINK_PATTERN)
_COUNT = re.compile(r"^\s*([\d.,]+)\s*([万千KkMm]?)\s*$")


@dataclass(frozen=True)
class ExternalPostRecord:
    external_post_key: str
    author_handle: str | None
    permalink: str
    post_timestamp: datetime | None
    body_text: str
    body_hash: str
    topic: str | None
    media_type: str | None
    has_link: bool
    likes: int | None
    replies: int | None
    reposts: int | None
    quotes: int | None
    shares: int | None
    features: dict


@dataclass
class PageResult:
    status: str
    posts: list[ExternalPostRecord] = field(default_factory=list)
    rejected: int = 0
    reason: str | None = None
    trending_topics: list[str] = field(default_factory=list)


def parse_count(text: str | None) -> int | None:
    """「1.2万」「3,456」「1.2K」→ 数。読めなければ ``None`` (0 にしない)。"""

    if text is None:
        return None
    match = _COUNT.match(text.replace(" ", " "))
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    unit = match.group(2)
    scale = {"万": 10_000, "千": 1_000, "K": 1_000, "k": 1_000, "M": 1_000_000,
             "m": 1_000_000}.get(unit, 1)  # fmt: skip
    return int(round(number * scale))


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _metric_values(card: Node) -> dict[str, int | None]:
    values: dict[str, int | None] = {"likes": None, "replies": None, "reposts": None,
                                      "shares": None}  # fmt: skip
    for svg in card.find_all("svg"):
        name = sel.METRIC_LABELS.get(svg.attrs.get("aria-label", ""))
        if name is None or values.get(name) is not None:
            continue
        holder = next((a for a in svg.ancestors() if a.attrs.get("role") == "button"), None)
        if holder is None:
            continue
        digits = [t.strip() for t in _texts(holder) if t.strip()]
        values[name] = next((parse_count(t) for t in digits if parse_count(t) is not None), None)
    return values


def _texts(node: Node) -> list[str]:
    out: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            out.append(child)
        elif child.tag not in ("svg", "title", "script", "style"):
            out.extend(_texts(child))
    return out


def _body(card: Node) -> str:
    lines: list[str] = []
    for span in card.find_all("span", dir="auto"):
        if any(a.tag == "a" for a in span.ancestors()):
            continue  # 名前・リンクの文字は本文に入れない
        if any(p.tag == "span" and p.attrs.get("dir") == "auto" for p in span.ancestors()):
            continue  # 入れ子は外側で数える
        text = span.text().strip()
        if text and (not lines or lines[-1] != text):
            lines.append(text)
    return "\n".join(lines)


def _media(card: Node) -> str:
    if card.find("video") is not None:
        return "video"
    for img in card.find_all("img"):
        alt = img.attrs.get("alt", "")
        if "プロフィール写真" in alt or "profile picture" in alt.lower():
            continue
        return "image"
    return "none"


def _card(card: Node) -> ExternalPostRecord | None:
    link = next((a for a in card.find_all("a")
                 if _PERMALINK.match(a.attrs.get("href", ""))), None)  # fmt: skip
    if link is None:
        return None
    match = _PERMALINK.match(link.attrs["href"])
    handle, code = match.group(1), match.group(2)
    body = _body(card)
    if not body:
        return None
    time_node = card.find("time")
    posted = _parse_time(time_node.attrs.get("datetime")) if time_node is not None else None
    topic_node = card.find(**sel.TOPIC_LINK)
    topic = topic_node.text().strip() if topic_node is not None else None
    has_link = any(
        a.attrs.get("href", "").startswith(sel.EXTERNAL_LINK_PREFIXES) for a in card.find_all("a")
    )
    metrics = _metric_values(card)
    media = _media(card)
    features = extract(body, topic=topic, media_type=media, posted_at=posted).as_dict()
    return ExternalPostRecord(
        external_post_key=f"threads:{code}",
        author_handle=handle,
        permalink=f"{sel.BASE_URL}/@{handle}/post/{code}",
        post_timestamp=posted,
        body_text=body,
        body_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        topic=topic,
        media_type=media,
        has_link=has_link,
        likes=metrics["likes"],
        replies=metrics["replies"],
        reposts=metrics["reposts"],
        quotes=None,  # 画面に別に出ない。推測しない。
        shares=metrics["shares"],
        features=features,
    )


def parse_page(html: str, *, limit: int) -> PageResult:
    """1 ページの投稿を最大 ``limit`` 件取り出す。形が違えば止める。"""

    root = parse_html(html)
    cards = root.find_all(**sel.POST_CONTAINER)
    if not cards:
        if any(a.attrs.get("href", "").find(m) >= 0 for a in root.find_all("a")
               for m in sel.LOGIN_MARKERS):  # fmt: skip
            return PageResult(PAGE_LOGIN_REQUIRED, reason="the page asks for a login")
        if root.find("main") is not None or root.find("body") is not None:
            return PageResult(PAGE_DOM_UNRECOGNIZED,
                              reason=f"no post container matched ({sel.SELECTOR_VERSION})")
        return PageResult(PAGE_EMPTY, reason="empty page")  # fmt: skip
    # 入れ子のまとまり (引用の中の投稿など) は外側だけを使う。
    outer = [c for c in cards if not any(a in cards for a in c.ancestors())]
    posts: list[ExternalPostRecord] = []
    rejected = 0
    seen: set[str] = set()
    for card in outer:
        if len(posts) >= limit:
            break
        record = _card(card)
        if record is None:
            rejected += 1
            continue
        if record.external_post_key in seen:
            continue
        seen.add(record.external_post_key)
        posts.append(record)
    examined = len(posts) + rejected
    if examined and rejected * 2 > examined:
        return PageResult(PAGE_DOM_UNRECOGNIZED, rejected=rejected,
                          reason=f"{rejected} of {examined} post cards did not match "
                          f"({sel.SELECTOR_VERSION}); nothing stored")  # fmt: skip
    return PageResult(PAGE_OK, posts=posts, rejected=rejected)


def parse_trending_topics(html: str, *, limit: int) -> PageResult:
    """トレンドのトピックの名前 (最大 ``limit``)。形が違えば止める。"""

    root = parse_html(html)
    names: list[str] = []
    for node in root.find_all(**sel.TOPIC_LINK):
        name = node.text().strip()
        if name and name not in names:
            names.append(name)
        if len(names) >= limit:
            break
    if not names:
        if any(m in a.attrs.get("href", "") for a in root.find_all("a") for m in sel.LOGIN_MARKERS):
            return PageResult(PAGE_LOGIN_REQUIRED, reason="the page asks for a login")
        return PageResult(PAGE_DOM_UNRECOGNIZED,
                          reason=f"no trending topic matched ({sel.SELECTOR_VERSION})")  # fmt: skip
    return PageResult(PAGE_OK, trending_topics=names)


__all__ = ["PAGE_DOM_UNRECOGNIZED", "PAGE_EMPTY", "PAGE_LOGIN_REQUIRED", "PAGE_OK",
           "ExternalPostRecord", "PageResult", "parse_count", "parse_page",
           "parse_trending_topics"]  # fmt: skip
