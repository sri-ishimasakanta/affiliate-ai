"""T6.5B: 検索の画面 (「上位検索結果」) の読み取り。本物の入れ子をもとにした合成の fixture。"""

from __future__ import annotations

from pathlib import Path

from app.models.threads_observer import SOURCE_SEARCH, SOURCE_TRENDING_TOPIC
from app.social.threads.observer import selectors as sel
from app.social.threads.observer.collector import (
    REASON_FILTERED_OVER_LIMIT,
    CollectionPlan,
    collect,
)
from app.social.threads.observer.normalize import TEXT_CLEAN, normalize_body
from app.social.threads.observer.parser import CARD_DUPLICATE_IN_FRAME, CARD_OK, parse_page
from tests.support.threads_observer_pages import FakePage, card, page

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "threads_observer" / (
    "search_verified_2026-09-28.html"
)
SEARCH_CHROME = ("上位検索結果", "最近", "プロフィール", "検索")


def _html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_the_observed_search_cards_are_read_like_for_you_cards() -> None:
    result = parse_page(_html(), limit=10)
    assert result.status == "ok" and result.rejected == 0
    assert [c.reason for c in result.cards] == [CARD_OK, CARD_OK, CARD_OK]
    plain, topic, blank = result.posts
    assert plain.author_handle == "search_user_a"
    assert plain.permalink == f"{sel.BASE_URL}/@search_user_a/post/SRCHAAA1"
    assert plain.post_timestamp.isoformat() == "2026-09-28T11:24:21+00:00"
    assert (plain.likes, plain.replies) == (12, 3)
    assert plain.topic is None
    assert topic.topic == "生成AIテスト"
    # 数が出ていない → None (0 と決めつけない)。
    assert (blank.likes, blank.replies) == (None, None)
    for post in result.posts:
        assert (post.reposts, post.shares, post.quotes) == (None, None, None)


def test_search_chrome_and_labels_never_enter_the_body() -> None:
    for post in parse_page(_html(), limit=10).posts:
        lines = post.body_text.split("\n")
        for chrome in SEARCH_CHROME:
            assert chrome not in lines, chrome
        assert post.author_handle not in post.body_text
        assert "生成AIテスト" not in lines  # トピックの札は本文ではない
        assert normalize_body(post.body_text).text_quality == TEXT_CLEAN


def test_the_query_text_inside_the_body_is_kept() -> None:
    # 検索語は本文の中の普通の文字 (強調の印は無い) → そのまま残る。
    first = parse_page(_html(), limit=10).posts[0]
    assert first.body_text.startswith("生成AIで業務を効率化した話")


def test_search_accounting_records_the_search_surface_version() -> None:
    fake = FakePage({sel.search_url("生成AI"): _html()})
    result = collect(fake, CollectionPlan(search_queries=("生成AI",)))
    summary = result.accounting_summary()
    [source] = summary["sources"]
    assert source["source_type"] == SOURCE_SEARCH and source["source_query"] == "生成AI"
    assert source["surface_selector_version"] == "threads-search-verified-2026-09-28-v1"
    assert source["surface_verified"] is True
    assert summary["candidate_cards"] == 3 == sum(summary["by_outcome"].values())
    assert summary["complete"] is True
    assert {p.source_type for p in result.posts} == {SOURCE_SEARCH}
    assert fake.visited == [sel.search_url("生成AI")]


def test_a_repeated_search_result_is_an_explicit_duplicate() -> None:
    doubled = parse_page(page(card("dup", "DUP1", "同じ投稿"), card("dup", "DUP1", "同じ投稿")),
                         limit=10)  # fmt: skip
    assert [c.reason for c in doubled.cards] == [CARD_OK, CARD_DUPLICATE_IN_FRAME]
    fake = FakePage({sel.search_url("生成AI"): page(card("dup", "DUP1", "同じ投稿"),
                                                   card("dup", "DUP1", "同じ投稿"))})  # fmt: skip
    summary = collect(fake, CollectionPlan(search_queries=("生成AI",))).accounting_summary()
    assert summary["by_outcome"] == {"accepted": 1, "duplicate": 1}
    assert summary["complete"] is True


def test_the_search_limit_counts_accepted_results_only() -> None:
    cards = [card("s", f"S{i}", f"生成AIの投稿 {i}") for i in range(8)]
    fake = FakePage({sel.search_url("生成AI"): page(*cards)})
    result = collect(fake, CollectionPlan(search_queries=("生成AI",)), limits={"search": 5})
    summary = result.accounting_summary()
    assert len(result.posts) == 5
    assert summary["by_reason"] == {"accepted": 5, REASON_FILTERED_OVER_LIMIT: 3}


def test_surface_verification_is_recorded_per_surface() -> None:
    assert sel.SELECTOR_VERSION == "threads-web-verified-2026-09-28-v2"  # 全体の版は変えない
    assert sel.surface_verification(SOURCE_SEARCH)["verified"] is True
    assert "search_tabs_outside_cards" in sel.surface_verification(SOURCE_SEARCH)["verified_fields"]
    # 2026-09-28: トピックの一覧と 1 つのトピックの投稿も確認済み (別の版)。
    assert sel.surface_verification(SOURCE_TRENDING_TOPIC)["version"] == (
        "threads-topic-posts-verified-2026-09-28-v1")  # fmt: skip
    assert sel.surface_verification("trending_list")["version"] == (
        "threads-topic-list-verified-2026-09-28-v1")  # fmt: skip
    for other in ("custom_feed",):
        assert sel.surface_verification(other) == {"version": None, "verified": False,
                                                   "verified_fields": ()}  # fmt: skip
    assert "search_page" not in sel.UNVERIFIED_SURFACES
    for still in ("custom_feed_page", "meta_ai_label_dom", "quoted_post_scoping",
                  "global_trending_ranking", "profile_replies_tab"):  # fmt: skip
        assert still in sel.UNVERIFIED_SURFACES
