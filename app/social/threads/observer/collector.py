"""外の Threads の観察を 1 回行う (T6.5B、読むだけ・件数の上限つき)。

- 出どころ (for_you / trending_topic / search / custom_feed / known_account) ごとに上限。
  1 回の実行の合計にも上限。スクロールは決まった回数まで (無限にスクロールしない)。
- ログインの画面なら、そこで止めて ``login_required`` (人がログインする)。
- 画面の形が違えば (DOM drift)、そこで止めて ``dom_unrecognized``。その実行で集めたものは
  **全部捨てる** (壊れた値を残さない)。
- スクリーンショットは任意 (出どころのページごとに、最初の状態と、スクロールしたなら最後の
  状態)。投稿ごとには撮らない。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.models.threads_observer import (
    RUN_DOM_UNRECOGNIZED,
    RUN_LOGIN_REQUIRED,
    RUN_PARTIAL,
    RUN_SUCCEEDED,
    SOURCE_CUSTOM_FEED,
    SOURCE_FOR_YOU,
    SOURCE_KNOWN_ACCOUNT,
    SOURCE_SEARCH,
    SOURCE_TRENDING_TOPIC,
)
from app.social.threads.observer import selectors as sel
from app.social.threads.observer.driver import ReadOnlyPage
from app.social.threads.observer.parser import (
    PAGE_DOM_UNRECOGNIZED,
    PAGE_LOGIN_REQUIRED,
    ExternalPostRecord,
    parse_page,
    parse_trending_topics,
)


@dataclass(frozen=True)
class CollectionPlan:
    for_you: bool = False
    trending: bool = False
    search_queries: tuple[str, ...] = ()
    custom_feeds: tuple[str, ...] = ()
    known_accounts: tuple[str, ...] = ()
    screenshots: bool = False

    def source_types(self) -> list[str]:
        out = []
        if self.for_you:
            out.append(SOURCE_FOR_YOU)
        if self.trending:
            out.append(SOURCE_TRENDING_TOPIC)
        if self.search_queries:
            out.append(SOURCE_SEARCH)
        if self.custom_feeds:
            out.append(SOURCE_CUSTOM_FEED)
        if self.known_accounts:
            out.append(SOURCE_KNOWN_ACCOUNT)
        return out


@dataclass(frozen=True)
class CollectedPost:
    source_type: str
    source_query: str | None
    record: ExternalPostRecord


@dataclass
class CollectionResult:
    started_at: datetime
    finished_at: datetime | None = None
    status: str = RUN_SUCCEEDED
    reason: str | None = None
    posts: list[CollectedPost] = field(default_factory=list)
    trending_topics: list[tuple[str, int]] = field(default_factory=list)
    rejected: int = 0
    screenshots: dict[str, str] = field(default_factory=dict)
    pages_opened: int = 0
    scrolls: int = 0
    source_types: list[str] = field(default_factory=list)
    item_limit: int = 0


class _Stop(Exception):
    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status, self.reason = status, reason


def collect(
    page: ReadOnlyPage,
    plan: CollectionPlan,
    *,
    limits: dict | None = None,
    screenshot_dir: Path | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CollectionResult:
    limits = {**sel.LIMITS, **(limits or {})}
    result = CollectionResult(started_at=clock(), source_types=plan.source_types(),
                              item_limit=int(limits["run_total"]))  # fmt: skip
    try:
        if plan.for_you:
            _feed(page, result, sel.for_you_url(), SOURCE_FOR_YOU, None,
                  limits["for_you"], limits, plan, screenshot_dir, "for_you")  # fmt: skip
        if plan.trending:
            _trending(page, result, limits, plan, screenshot_dir)
        for query in plan.search_queries:
            _feed(page, result, sel.search_url(query), SOURCE_SEARCH, query, limits["search"],
                  limits, plan, screenshot_dir, f"search-{_slug(query)}")  # fmt: skip
        for feed in plan.custom_feeds:
            _feed(page, result, sel.custom_feed_url(feed), SOURCE_CUSTOM_FEED, feed,
                  limits["custom_feed"], limits, plan, screenshot_dir,
                  f"custom-{_slug(feed)}")  # fmt: skip
        for handle in plan.known_accounts:
            _feed(page, result, sel.account_url(handle), SOURCE_KNOWN_ACCOUNT, handle,
                  limits["known_account"], limits, plan, screenshot_dir,
                  f"account-{_slug(handle)}")  # fmt: skip
    except _Stop as stop:
        result.status, result.reason = stop.status, stop.reason
        if stop.status == RUN_DOM_UNRECOGNIZED:
            result.posts.clear()  # 壊れたかもしれない実行の値は残さない
            result.trending_topics.clear()
    else:
        if result.rejected and result.status == RUN_SUCCEEDED:
            result.status = RUN_PARTIAL
            result.reason = f"{result.rejected} post card(s) did not match and were skipped"
    result.finished_at = clock()
    return result


def _room(result: CollectionResult, limits: dict) -> int:
    return max(0, int(limits["run_total"]) - len(result.posts))


def _feed(page, result, url, source_type, query, per_source, limits, plan, shot_dir, shot_name):
    want = min(int(per_source), _room(result, limits))
    if want <= 0:
        return
    page.goto(url)
    result.pages_opened += 1
    gathered: dict[str, ExternalPostRecord] = {}
    rejected = 0
    shoot = plan.screenshots and shot_dir is not None
    for attempt in range(int(limits["max_scrolls"]) + 1):
        if shoot and attempt == 0:
            # 最初に見た状態 (スクロールの前) を撮る。
            _shot(page, result, shot_dir, shot_name)
        parsed = parse_page(page.content(), limit=want)
        if parsed.status == PAGE_LOGIN_REQUIRED:
            raise _Stop(RUN_LOGIN_REQUIRED, f"{source_type}: {parsed.reason}")
        if parsed.status == PAGE_DOM_UNRECOGNIZED:
            raise _Stop(RUN_DOM_UNRECOGNIZED, f"{source_type}: {parsed.reason}")
        rejected = max(rejected, parsed.rejected)
        for record in parsed.posts:
            gathered.setdefault(record.external_post_key, record)
        if len(gathered) >= want or attempt == int(limits["max_scrolls"]):
            break
        page.scroll()
        result.scrolls += 1
    if shoot and attempt > 0:
        # スクロールしたなら、最後の状態も撮る (集めた投稿を画面と照らせるように)。
        _shot(page, result, shot_dir, f"{shot_name}-final")
    result.rejected += rejected
    for record in list(gathered.values())[:want]:
        result.posts.append(CollectedPost(source_type, query, record))


def _shot(page, result, shot_dir: Path, name: str) -> None:
    path = shot_dir / f"{name}.png"
    page.screenshot(path)
    result.screenshots[name] = str(path)


def _trending(page, result, limits, plan, shot_dir):
    page.goto(sel.trends_url())
    result.pages_opened += 1
    parsed = parse_trending_topics(page.content(), limit=int(limits["trending_topics_max"]))
    if parsed.status == PAGE_LOGIN_REQUIRED:
        raise _Stop(RUN_LOGIN_REQUIRED, f"trending: {parsed.reason}")
    if parsed.status == PAGE_DOM_UNRECOGNIZED:
        raise _Stop(RUN_DOM_UNRECOGNIZED, f"trending: {parsed.reason}")
    if plan.screenshots and shot_dir is not None:
        page.screenshot(shot_dir / "trends.png")
        result.screenshots["trends"] = str(shot_dir / "trends.png")
    for topic in parsed.trending_topics:
        before = len(result.posts)
        _feed(page, result, sel.topic_url(topic), SOURCE_TRENDING_TOPIC, topic,
              limits["trending_topic"], limits, plan, None, "")  # fmt: skip
        result.trending_topics.append((topic, len(result.posts) - before))


def _slug(text: str) -> str:
    keep = "".join(ch if ch.isalnum() else "-" for ch in text.strip().lower())
    return keep.strip("-")[:40] or "x"


__all__ = ["CollectedPost", "CollectionPlan", "CollectionResult", "collect"]
