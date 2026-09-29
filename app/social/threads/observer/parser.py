"""観察したページの HTML → 外の投稿の記録 (決定的・読むだけ)。

- 期待する形 (``selectors``) が見つからなければ、ページ全体を ``dom_unrecognized`` として止め、
  何も保存させない (値を読み替えない)。
- ログインの画面なら ``login_required`` (人がログインする)。
- 1 件の投稿の形が崩れていれば、その投稿だけを捨てる (数える)。半分以上が崩れていれば、
  ページ全体を ``dom_unrecognized`` にする。
- 見えない指標は ``None`` (0 にしない)。外の表示回数は読まない (見えない)。
- **まとまりの勘定** (T6.5B.1): ページの投稿のまとまりには 1 つずつ、決まった結果の理由
  (``CARD_*``) を付ける (``PageResult.cards``)。それとは別に、投稿の時刻のリンク
  (``/@名前/post/コード`` で ``<time>`` を含む) を数え、どのまとまりにも入っていない投稿も
  理由つきで数える。黙って消える投稿を作らない。
- **本文** (T6.5B.1): 本文の ``span[dir=auto]`` の中の、続きの投稿の印 (「1/2」) の ``div`` は
  本文ではない (2026-09-28 の画面で確認: ``div > div > [span 数, div > span 区切り, span 数]``)。
  書いた人の「1/2の確率」は本文の文字の span の中にあるので残る。
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


#: まとまりの結果の理由 (安定した ID)。
CARD_OK = "ok"
CARD_MALFORMED_NO_PERMALINK = "malformed_no_permalink"
CARD_MALFORMED_EMPTY_BODY = "malformed_empty_body"
CARD_DUPLICATE_IN_FRAME = "duplicate_in_frame"
CARD_UNSUPPORTED_NESTED = "unsupported_nested_card"
CARD_UNSUPPORTED_OUTSIDE = "unsupported_outside_container"
CARD_REASONS = (
    CARD_OK, CARD_MALFORMED_NO_PERMALINK, CARD_MALFORMED_EMPTY_BODY, CARD_DUPLICATE_IN_FRAME,
    CARD_UNSUPPORTED_NESTED, CARD_UNSUPPORTED_OUTSIDE,
)  # fmt: skip
MALFORMED_REASONS = (CARD_MALFORMED_NO_PERMALINK, CARD_MALFORMED_EMPTY_BODY)

#: 続きの投稿の印 (「1/2」) の div の文字。
_THREAD_MARKER = re.compile(r"\s*\d+\s*/\s*\d+\s*")


@dataclass(frozen=True)
class CardEval:
    """ページの中の 1 つの投稿のまとまり (または、まとまりの外の投稿) の読み取りの結果。"""

    index: int
    key: str | None
    handle: str | None
    reason: str
    record: ExternalPostRecord | None = None
    #: キーの無いまとまりを、読みをまたいで同じものと分かるための印 (文字の hash の先頭)。
    fingerprint: str | None = None


@dataclass
class PageResult:
    status: str
    posts: list[ExternalPostRecord] = field(default_factory=list)
    rejected: int = 0
    reason: str | None = None
    trending_topics: list[str] = field(default_factory=list)
    #: ページのすべての投稿のまとまりの結果 (上から順)。
    cards: list[CardEval] = field(default_factory=list)
    #: 投稿の時刻のリンクから独立に数えた、ページの投稿のキー (上から順)。
    anchor_keys: list[str] = field(default_factory=list)
    #: トピックの一覧の画面の、すべての候補 (``parse_trending_topics``)。
    topic_entries: list = field(default_factory=list)


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


def _in_nested(node: Node, card: Node) -> bool:
    """``card`` の中の、別の投稿のまとまり (引用した投稿など) の中にあるか。"""

    for ancestor in node.ancestors():
        if ancestor is card:
            return False
        if ancestor.matches(**sel.POST_CONTAINER):
            return True
    return False


def _own(card: Node, tag: str | None = None, **conditions) -> list[Node]:
    """``card`` 自身の要素だけ (中に入っている別の投稿のまとまりの要素は除く)。

    2026-09-28 の画面で確認: 引用した投稿は外側の投稿のまとまりの中に、自分のまとまり
    (いいね・返信のボタンつき) として入っている。外側の数を引用の数で読まない。
    """

    return [n for n in card.find_all(tag, **conditions) if not _in_nested(n, card)]


def _metric_values(card: Node) -> dict[str, int | None]:
    values: dict[str, int | None] = {"likes": None, "replies": None, "reposts": None,
                                      "shares": None}  # fmt: skip
    for svg in _own(card, "svg"):
        name = sel.METRIC_LABELS.get(_icon_label(svg))
        if name is None or values.get(name) is not None:
            continue
        holder = next((a for a in svg.ancestors() if a.attrs.get("role") == "button"), None)
        if holder is None:
            continue
        digits = [t.strip() for t in _texts(holder) if t.strip()]
        values[name] = next((parse_count(t) for t in digits if parse_count(t) is not None), None)
    return values


def _icon_label(svg: Node) -> str:
    """アイコンの名前: ``title`` 属性 → ``<title>`` → ``aria-label`` の順。"""

    if svg.attrs.get("title"):
        return svg.attrs["title"].strip()
    title = next((c for c in svg.children if isinstance(c, Node) and c.tag == "title"), None)
    if title is not None and title.text().strip():
        return title.text().strip()
    return svg.attrs.get("aria-label", "").strip()


def _texts(node: Node) -> list[str]:
    out: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            out.append(child)
        elif child.tag not in ("svg", "title", "script", "style"):
            out.extend(_texts(child))
    return out


def _is_thread_marker(node: Node) -> bool:
    return node.tag == "div" and bool(_THREAD_MARKER.fullmatch(node.text() or ""))


def _body_text(node: Node) -> str:
    """本文の span の文字。続きの投稿の印の div は入れない。"""

    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        elif child.tag == "br":
            parts.append("\n")
        elif child.tag in ("script", "style") or _is_thread_marker(child):
            continue
        else:
            parts.append(_body_text(child))
    return "".join(parts)


def _body(card: Node) -> str:
    lines: list[str] = []
    for span in _own(card, "span", dir="auto"):
        if any(a.tag == "a" for a in span.ancestors()):
            continue  # 名前・リンクの文字は本文に入れない
        if span.find("time") is not None:
            continue  # 見出しの「44分」(時刻のリンクを包む span) は本文ではない
        if any(a.attrs.get("role") == "button" for a in span.ancestors()):
            continue  # いいね等のボタンの中の数は本文ではない
        if any(p.tag == "span" and p.attrs.get("dir") == "auto" for p in span.ancestors()):
            continue  # 入れ子は外側で数える
        text = _body_text(span).strip()
        if text and (not lines or lines[-1] != text):
            lines.append(text)
    return "\n".join(lines)


def _media(card: Node) -> str:
    if _own(card, "video"):
        return "video"
    for img in _own(card, "img"):
        alt = img.attrs.get("alt", "")
        if "プロフィール写真" in alt or "profile picture" in alt.lower():
            continue
        return "image"
    return "none"


def _post_anchor(card: Node) -> Node | None:
    """投稿そのもののリンク: 時刻を含む permalink (無ければ最初の permalink)。"""

    links = [a for a in _own(card, "a") if _PERMALINK.match(a.attrs.get("href", ""))]
    return next((a for a in links if a.find("time") is not None), links[0] if links else None)


def _card(card: Node) -> ExternalPostRecord | None:
    return _evaluate(card, 0).record


def _evaluate(card: Node, index: int) -> CardEval:
    link = _post_anchor(card)
    if link is None:
        digest = hashlib.sha256(card.text().encode("utf-8")).hexdigest()[:16]
        return CardEval(index, None, None, CARD_MALFORMED_NO_PERMALINK, fingerprint=digest)
    match = _PERMALINK.match(link.attrs["href"])
    handle, code = match.group(1), match.group(2)
    key = f"threads:{code}"
    body = _body(card)
    if not body:
        return CardEval(index, key, handle, CARD_MALFORMED_EMPTY_BODY)
    time_node = next(iter(_own(card, "time")), None)
    posted = _parse_time(time_node.attrs.get("datetime")) if time_node is not None else None
    topic_node = next(iter(_own(card, **sel.TOPIC_LINK)), None)
    topic = topic_node.text().strip() if topic_node is not None else None
    has_link = any(
        a.attrs.get("href", "").startswith(sel.EXTERNAL_LINK_PREFIXES) for a in _own(card, "a")
    )
    metrics = _metric_values(card)
    media = _media(card)
    features = extract(body, topic=topic, media_type=media, posted_at=posted).as_dict()
    record = ExternalPostRecord(
        external_post_key=key,
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
    return CardEval(index, key, handle, CARD_OK, record)


def _anchor_keys(root: Node) -> list[tuple[str, str, Node]]:
    """投稿の時刻のリンク (まとまりとは独立に数える)。(キー, 名前, リンク)。"""

    out = []
    for a in root.find_all("a"):
        match = _PERMALINK.match(a.attrs.get("href", ""))
        if match and a.find("time") is not None:
            out.append((f"threads:{match.group(2)}", match.group(1), a))
    return out


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
    ids = {id(c) for c in cards}
    evals: list[CardEval] = []
    seen: set[str] = set()
    for index, card in enumerate(cards):
        if any(id(a) in ids for a in card.ancestors()):
            # 入れ子のまとまり (引用の中の投稿など) は読まないが、理由つきで数える。
            link = _post_anchor(card)
            match = _PERMALINK.match(link.attrs["href"]) if link is not None else None
            evals.append(CardEval(
                index, f"threads:{match.group(2)}" if match else None,
                match.group(1) if match else None, CARD_UNSUPPORTED_NESTED,
            ))  # fmt: skip
            continue
        result = _evaluate(card, index)
        if result.reason == CARD_OK and result.key in seen:
            result = CardEval(index, result.key, result.handle, CARD_DUPLICATE_IN_FRAME)
        if result.key is not None:
            seen.add(result.key)
        evals.append(result)
    # まとまりの外にある投稿 (時刻のリンクがどのまとまりにも入っていない) も数える。
    anchors = _anchor_keys(root)
    for key, handle, anchor in anchors:
        if not any(id(a) in ids for a in anchor.ancestors()):
            evals.append(CardEval(len(evals), key, handle, CARD_UNSUPPORTED_OUTSIDE))
    rejected = sum(1 for e in evals if e.reason in MALFORMED_REASONS)
    examined = sum(1 for e in evals if e.reason in (CARD_OK, *MALFORMED_REASONS))
    if examined and rejected * 2 > examined:
        return PageResult(PAGE_DOM_UNRECOGNIZED, rejected=rejected, cards=evals,
                          anchor_keys=[k for k, _, _ in anchors],
                          reason=f"{rejected} of {examined} post cards did not match "
                          f"({sel.SELECTOR_VERSION}); nothing stored")  # fmt: skip
    posts = [e.record for e in evals if e.reason == CARD_OK][:limit]
    return PageResult(PAGE_OK, posts=posts, rejected=rejected, cards=evals,
                      anchor_keys=[k for k, _, _ in anchors])


FEED_BUILT_IN = "built_in"
FEED_CUSTOM_CANDIDATE = "custom_candidate"
_FEEDISH = re.compile(r"(feed|custom|^/following/?$|^/saved/?$|^/liked/?$|^/for_you/?$)", re.I)


@dataclass(frozen=True)
class FeedLink:
    href: str
    name: str | None
    kind: str


def parse_feed_links(html: str) -> list[FeedLink]:
    """ページのフィードのリンク (組み込み / 自分で作ったものの候補)。読むだけ。

    組み込み = ``selectors.BUILT_IN_FEED_PATHS`` (おすすめ・フォロー中・保存済み・「いいね！」
    済み)。それ以外で ``feed`` / ``custom`` を含むリンクは、自分で作ったフィードの **候補**
    (形はまだ画面で確かめていない)。
    """

    root = parse_html(html)
    out: list[FeedLink] = []
    seen: set[str] = set()
    for a in root.find_all("a"):
        href = a.attrs.get("href", "")
        path = href.split("?", 1)[0]
        if not path.startswith("/") or not _FEEDISH.search(path) or href in seen:
            continue
        seen.add(href)
        name = " ".join(t.strip() for t in a.text().split("\n") if t.strip()) or None
        built_in = path.rstrip("/") in {p.rstrip("/") for p in sel.BUILT_IN_FEED_PATHS}
        out.append(FeedLink(href, name, FEED_BUILT_IN if built_in else FEED_CUSTOM_CANDIDATE))
    return out


TOPIC_OK = "ok"
TOPIC_DUPLICATE = "duplicate_topic"
TOPIC_MALFORMED = "malformed_topic"


@dataclass(frozen=True)
class TopicEntry:
    """トピックの一覧の 1 つ (画面の順)。数は画面に出ていないので持たない。"""

    rank: int
    name: str | None
    href: str
    query: str | None
    serp_type: str | None
    kind: str
    reason: str


def _topic_entry(rank: int, link: Node) -> TopicEntry:
    from urllib.parse import parse_qs, urlsplit

    href = link.attrs.get("href", "")
    params = parse_qs(urlsplit(href).query)
    query = (params.get("q") or [None])[0]
    serp_type = (params.get("serp_type") or [None])[0]
    name = " ".join(t.strip() for t in link.text().split("\n") if t.strip()) or None
    kind = sel.TOPIC_LIST_KINDS.get(serp_type or "", sel.TOPIC_LIST_KIND_UNKNOWN)
    reason = TOPIC_OK if name and query else TOPIC_MALFORMED
    return TopicEntry(rank, name, href, query, serp_type, kind, reason)


def parse_trending_topics(html: str, *, limit: int) -> PageResult:
    """トピックの一覧 (検索の最初の画面の「おすすめのトピック」)。形が違えば止める。

    候補 = 投稿のまとまりの外にある ``TOPIC_SUGGESTION_LINK`` のリンク (画面の順)。1 つずつ
    ``ok`` / ``duplicate_topic`` (同じ語) / ``malformed_topic`` (名前か語が無い) を付ける
    (``PageResult.topic_entries``)。``trending_topics`` は ``ok`` の名前の先頭 ``limit`` 件。
    左のメニューのコミュニティ (``serp_type=tags``) は候補にしない。
    """

    root = parse_html(html)
    containers = {id(c) for c in root.find_all(**sel.POST_CONTAINER)}
    links = [a for a in root.find_all(**sel.TOPIC_SUGGESTION_LINK)
             if not any(id(p) in containers for p in a.ancestors())]  # fmt: skip
    entries: list[TopicEntry] = []
    seen: set[str] = set()
    for rank, link in enumerate(links, start=1):
        entry = _topic_entry(rank, link)
        if entry.reason == TOPIC_OK and entry.query in seen:
            entry = TopicEntry(entry.rank, entry.name, entry.href, entry.query, entry.serp_type,
                               entry.kind, TOPIC_DUPLICATE)  # fmt: skip
        if entry.query:
            seen.add(entry.query)
        entries.append(entry)
    if not entries:
        if any(m in a.attrs.get("href", "") for a in root.find_all("a") for m in sel.LOGIN_MARKERS):
            return PageResult(PAGE_LOGIN_REQUIRED, reason="the page asks for a login")
        return PageResult(PAGE_DOM_UNRECOGNIZED,
                          reason=f"no topic list matched ({sel.SELECTOR_VERSION})")  # fmt: skip
    malformed = sum(1 for e in entries if e.reason == TOPIC_MALFORMED)
    if malformed * 2 > len(entries):
        return PageResult(
            PAGE_DOM_UNRECOGNIZED, topic_entries=entries,
            reason=f"{malformed} of {len(entries)} topic entries did not match",
        )  # fmt: skip
    names = [e.name for e in entries if e.reason == TOPIC_OK][:limit]
    return PageResult(PAGE_OK, trending_topics=names, topic_entries=entries)


__all__ = ["CARD_DUPLICATE_IN_FRAME", "CARD_MALFORMED_EMPTY_BODY", "CARD_MALFORMED_NO_PERMALINK",
           "CARD_OK", "CARD_REASONS", "CARD_UNSUPPORTED_NESTED", "CARD_UNSUPPORTED_OUTSIDE",
           "MALFORMED_REASONS", "PAGE_DOM_UNRECOGNIZED", "PAGE_EMPTY", "PAGE_LOGIN_REQUIRED",
           "PAGE_OK", "TOPIC_DUPLICATE", "TOPIC_MALFORMED", "TOPIC_OK", "CardEval",
           "ExternalPostRecord", "FEED_BUILT_IN", "FEED_CUSTOM_CANDIDATE", "FeedLink",
           "PageResult", "TopicEntry", "parse_count", "parse_feed_links", "parse_page",
           "parse_trending_topics"]  # fmt: skip
