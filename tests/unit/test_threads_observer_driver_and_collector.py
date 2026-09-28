"""T6.5B: 読むだけの操作・件数の上限・止まり方。"""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from app.models.threads_observer import (
    RUN_DOM_UNRECOGNIZED,
    RUN_LOGIN_REQUIRED,
    RUN_PARTIAL,
    RUN_SUCCEEDED,
    SOURCE_FOR_YOU,
    SOURCE_KNOWN_ACCOUNT,
    SOURCE_SEARCH,
    SOURCE_TRENDING_TOPIC,
)
from app.social.threads.observer import selectors as sel
from app.social.threads.observer.collector import CollectionPlan, collect
from app.social.threads.observer.driver import ObserverError, PlaywrightPage, check_url
from tests.support.threads_observer_pages import (
    FakePage,
    card,
    drifted_page,
    login_page,
    page,
    trends_page,
)

FORBIDDEN = ("click", "tap", "type", "fill", "press", "check", "select_option", "like",
             "reply", "follow", "repost", "quote", "post", "publish", "send", "dm",
             "set_input_files", "evaluate", "keyboard")  # fmt: skip


def _cards(prefix: str, n: int, start: int = 0) -> list[str]:
    return [card(prefix, f"{prefix.upper()}{i}", f"{prefix} の投稿 {i}")
            for i in range(start, start + n)]  # fmt: skip


# -- 読むだけの操作 --------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["https://www.threads.com/", "https://www.threads.com/search?q=AI&serp_type=tags",
     "https://www.threads.net/@alice"],
)  # fmt: skip
def test_threads_read_pages_are_allowed(url: str) -> None:
    assert check_url(url) == url


@pytest.mark.parametrize(
    "url",
    ["http://www.threads.com/", "https://evil.example/", "https://bizfluxlab.com/go/x",
     "https://www.threads.com/login", "https://www.threads.com/intent/post?text=x",
     "https://www.threads.com/accounts/edit"],
)  # fmt: skip
def test_other_urls_are_refused(url: str) -> None:
    with pytest.raises(ObserverError):
        check_url(url)


def test_the_browser_page_has_no_interaction_methods() -> None:
    public = {name for name, _ in inspect.getmembers(PlaywrightPage) if not name.startswith("_")}
    assert public == {"goto", "scroll", "content", "screenshot", "wait_for_human", "close"}
    for name in public:
        assert not any(name == word or name.startswith(f"{word}_") for word in FORBIDDEN), name


def test_playwright_is_not_imported_by_the_observer_modules() -> None:
    code = "; ".join(
        [
            "import sys",
            "import app.social.threads.observer.driver",
            "import app.social.threads.observer.collector",
            "import app.services.threads_observer_service",
            "import app.services.threads_trend_analysis_service",
            "print(any(m.startswith('playwright') for m in sys.modules))",
        ]
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=Path(__file__).resolve().parents[2], check=True)  # fmt: skip
    assert out.stdout.strip() == "False"


# -- 件数の上限 -------------------------------------------------------------------------


def test_for_you_is_capped_and_scrolling_is_bounded() -> None:
    # 1 画面に 4 件しか無く、スクロールしても増えない → 決まった回数で止まる。
    fake = FakePage({sel.for_you_url(): page(*_cards("a", 4))})
    result = collect(fake, CollectionPlan(for_you=True))
    assert len(result.posts) == 4
    assert fake.scrolls == sel.LIMITS["max_scrolls"]
    assert result.status == RUN_SUCCEEDED


def test_an_endless_feed_stops_at_the_limit() -> None:
    frames = [page(*_cards("b", 6 * (i + 1))) for i in range(50)]
    fake = FakePage({sel.for_you_url(): frames})
    result = collect(fake, CollectionPlan(for_you=True))
    assert len(result.posts) == sel.LIMITS["for_you"] == 20
    assert fake.scrolls <= sel.LIMITS["max_scrolls"]


def test_the_run_total_caps_every_source() -> None:
    pages = {sel.search_url(q): page(*_cards(q, 10)) for q in ("x", "y", "z")}
    fake = FakePage(pages)
    result = collect(fake, CollectionPlan(search_queries=("x", "y", "z")),
                     limits={"run_total": 15})  # fmt: skip
    assert len(result.posts) == 15
    assert [p.source_query for p in result.posts].count("z") == 0
    assert len(fake.visited) == 2  # 上限に達したら次のページは開かない


def test_search_and_known_account_sources() -> None:
    fake = FakePage({sel.search_url("生成AI"): page(*_cards("s", 3)),
                     sel.account_url("alice"): page(*_cards("alice", 12))})  # fmt: skip
    result = collect(fake, CollectionPlan(search_queries=("生成AI",), known_accounts=("alice",)))
    kinds = [p.source_type for p in result.posts]
    assert kinds.count(SOURCE_SEARCH) == 3
    assert kinds.count(SOURCE_KNOWN_ACCOUNT) == sel.LIMITS["known_account"] == 10
    assert result.source_types == [SOURCE_SEARCH, SOURCE_KNOWN_ACCOUNT]


def test_trending_topics_are_followed_within_limits() -> None:
    topics = [f"T{i}" for i in range(8)]
    pages = {sel.trends_url(): trends_page(*topics)}
    pages.update({sel.topic_url(t): page(*_cards(t.lower(), 9)) for t in topics})
    fake = FakePage(pages)
    result = collect(fake, CollectionPlan(trending=True))
    assert [name for name, _ in result.trending_topics] == topics[:5]
    assert all(sample == sel.LIMITS["trending_topic"] for _, sample in result.trending_topics)
    assert {p.source_type for p in result.posts} == {SOURCE_TRENDING_TOPIC}


# -- 止まり方 ---------------------------------------------------------------------------


def test_login_required_stops_without_going_further() -> None:
    fake = FakePage({sel.for_you_url(): login_page()})
    result = collect(fake, CollectionPlan(for_you=True, search_queries=("x",)))
    assert result.status == RUN_LOGIN_REQUIRED
    assert result.posts == []
    assert fake.visited == [sel.for_you_url()]


def test_dom_drift_discards_everything_collected_in_the_run() -> None:
    fake = FakePage({sel.for_you_url(): page(*_cards("a", 3)),
                     sel.search_url("x"): drifted_page()})  # fmt: skip
    result = collect(fake, CollectionPlan(for_you=True, search_queries=("x",)))
    assert result.status == RUN_DOM_UNRECOGNIZED
    assert result.posts == []
    assert "search" in result.reason


def test_a_skipped_card_makes_the_run_partial() -> None:
    broken = '<div data-pressable-container="true"><span dir="auto">x</span></div>'
    fake = FakePage({sel.for_you_url(): page(broken, *_cards("a", 3))})
    result = collect(fake, CollectionPlan(for_you=True))
    assert result.status == RUN_PARTIAL
    assert result.rejected == 1
    assert len(result.posts) == 3


def test_screenshots_are_optional_and_per_page(tmp_path: Path) -> None:
    fake = FakePage({sel.for_you_url(): page(*_cards("a", 2))})
    collect(fake, CollectionPlan(for_you=True), screenshot_dir=tmp_path)
    assert fake.screenshots == []
    result = collect(fake, CollectionPlan(for_you=True, screenshots=True), screenshot_dir=tmp_path)
    assert fake.screenshots == [tmp_path / "for_you.png"]
    assert result.screenshots == {"for_you": str(tmp_path / "for_you.png")}
    assert result.posts[0].source_type == SOURCE_FOR_YOU
