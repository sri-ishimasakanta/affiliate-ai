"""T6.5B: トピックの一覧 (検索の最初の画面の「おすすめのトピック」) と、1 つのトピックの投稿。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ThreadsExternalPost, ThreadsTrendingTopic
from app.models.threads_observer import SOURCE_TRENDING_TOPIC
from app.services.threads_observer_service import record_run
from app.social.threads.observer import selectors as sel
from app.social.threads.observer.collector import CollectionPlan, collect
from app.social.threads.observer.normalize import TEXT_CLEAN, normalize_body
from app.social.threads.observer.parser import (
    PAGE_DOM_UNRECOGNIZED,
    PAGE_LOGIN_REQUIRED,
    TOPIC_DUPLICATE,
    TOPIC_MALFORMED,
    TOPIC_OK,
    parse_page,
    parse_trending_topics,
)
from tests.support.threads_observer_pages import (
    FakePage,
    card,
    login_page,
    page,
    suggestion_url,
    trends_page,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "threads_observer"
LIST_FIXTURE = FIXTURES / "topic_list_verified_2026-09-28.html"
OBSERVED = ["ハンドメイド", "AI", "インスタで繋がろう", "旅行", "転職", "note"]


def _list_html() -> str:
    return LIST_FIXTURE.read_text(encoding="utf-8")


# -- トピックの一覧 -----------------------------------------------------------------------


def test_the_observed_topic_list_is_read_in_screen_order() -> None:
    result = parse_trending_topics(_list_html(), limit=5)
    assert result.status == "ok"
    assert result.trending_topics == OBSERVED[:5]
    entries = result.topic_entries
    assert [e.name for e in entries[:6]] == OBSERVED
    assert [e.rank for e in entries] == list(range(1, len(entries) + 1))
    first = entries[0]
    assert first.query == "ハンドメイド"
    assert first.serp_type == "search_nullstate_topic_for_you"
    assert first.kind == "topic_for_you"
    assert first.href.startswith("/search?q=") and "serp_type=search_nullstate_topic" in first.href


def test_duplicate_and_empty_topic_entries_have_reasons() -> None:
    reasons = [e.reason for e in parse_trending_topics(_list_html(), limit=5).topic_entries]
    assert reasons == [TOPIC_OK] * 6 + [TOPIC_DUPLICATE, TOPIC_MALFORMED]


def test_ui_chrome_is_never_a_topic() -> None:
    names = {e.name for e in parse_trending_topics(_list_html(), limit=99).topic_entries}
    for chrome in ("コミュニティ0", "コミュニティ1", "フォローのおすすめ", "フォローする", "検索",
                   "おすすめのアカウントの紹介文"):  # fmt: skip
        assert chrome not in names, chrome
    # 左のメニューのコミュニティ (serp_type=tags) は一覧ではない。
    assert parse_trending_topics(trends_page(sidebar=True), limit=5).status == PAGE_DOM_UNRECOGNIZED


def test_the_topic_list_page_is_not_a_post_page() -> None:
    # おすすめのアカウントのまとまりには投稿の時刻のリンクが無い → 投稿として読まない。
    assert parse_page(_list_html(), limit=9).status == PAGE_DOM_UNRECOGNIZED


def test_the_topic_list_login_page() -> None:
    assert parse_trending_topics(login_page(), limit=5).status == PAGE_LOGIN_REQUIRED


def test_topic_list_accounting_and_limit() -> None:
    fake = FakePage({sel.trends_url(): _list_html()}, default=page())
    result = collect(fake, CollectionPlan(trending=True), limits={"trending_topics_follow": 1})
    topics = result.topic_accounting
    assert topics["candidate_topics"] == 8 == sum(topics["by_outcome"].values())
    assert topics["by_reason"] == {"accepted": 5, "filtered_over_limit": 1,
                                   TOPIC_DUPLICATE: 1, TOPIC_MALFORMED: 1}  # fmt: skip
    assert topics["complete"] is True
    assert topics["surface_selector_version"] == "threads-topic-list-verified-2026-09-28-v1"
    assert [e["rank"] for e in topics["sequence"]] == list(range(1, 9))


# -- 1 つのトピックの投稿 --------------------------------------------------------------------


def _topic_pages(first_has_posts: bool = True) -> dict:
    pages = {sel.trends_url(): trends_page("ハンドメイド", "AI", "旅行")}
    pages[suggestion_url("ハンドメイド")] = (
        page(card("a", "A1", "ハンドメイドの作品", likes="1743", replies="205"),
             card("b", "B1", "はじめまして", topic="はじめましてThreads", likes="4", replies="1"),
             card("c", "C1", "反応の無い投稿", likes=None, replies=None, thread_marker="1/2"))
        if first_has_posts else page()
    )  # fmt: skip
    pages[suggestion_url("AI")] = page(card("d", "D1", "AIの話", likes="3", replies="2"))
    return pages


def test_one_topic_is_followed_from_its_own_link() -> None:
    fake = FakePage(_topic_pages())
    result = collect(fake, CollectionPlan(trending=True), limits={"trending_topics_follow": 1})
    assert fake.visited == [sel.trends_url(), suggestion_url("ハンドメイド")]
    assert [p.record.external_post_key for p in result.posts] == [
        "threads:A1", "threads:B1", "threads:C1"]  # fmt: skip
    assert {p.source_type for p in result.posts} == {SOURCE_TRENDING_TOPIC}
    assert {p.source_query for p in result.posts} == {"ハンドメイド"}
    [source] = result.accounting_summary()["sources"]
    assert source["complete"] and source["surface_selector_version"] == (
        "threads-topic-posts-verified-2026-09-28-v1")  # fmt: skip
    followed = [e for e in result.topic_accounting["sequence"] if e["followed"]]
    assert [(e["name"], e["posts"]) for e in followed] == [("ハンドメイド", 3)]


def test_the_page_topic_is_not_copied_onto_posts() -> None:
    """ページのトピック (「ハンドメイド」) ≠ 投稿のトピック。投稿に出ているものだけを持つ。"""

    fake = FakePage(_topic_pages())
    result = collect(fake, CollectionPlan(trending=True), limits={"trending_topics_follow": 1})
    topics = {p.record.external_post_key: p.record.topic for p in result.posts}
    assert topics == {"threads:A1": None, "threads:B1": "はじめましてThreads",
                      "threads:C1": None}  # fmt: skip
    assert all(p.record.features["topic"] == topics[p.record.external_post_key]
               for p in result.posts)  # fmt: skip


def test_topic_post_bodies_are_clean_and_blanks_stay_none() -> None:
    fake = FakePage(_topic_pages())
    result = collect(fake, CollectionPlan(trending=True), limits={"trending_topics_follow": 1})
    by_key = {p.record.external_post_key: p.record for p in result.posts}
    assert by_key["threads:C1"].body_text == "反応の無い投稿"  # 「1/2」は本文に入らない
    assert (by_key["threads:C1"].likes, by_key["threads:C1"].replies) == (None, None)
    assert (by_key["threads:A1"].likes, by_key["threads:A1"].replies) == (1743, 205)
    for record in by_key.values():
        assert "ハンドメイド" not in record.body_text.split("\n")[0][:0] + record.body_text[:0]
        assert normalize_body(record.body_text).text_quality == TEXT_CLEAN


def test_an_empty_topic_page_fails_closed_instead_of_being_skipped() -> None:
    # 投稿が 1 件も無いページは、画面の形が変わったページと区別できない → 実行を止める。
    fake = FakePage(_topic_pages(first_has_posts=False))
    result = collect(fake, CollectionPlan(trending=True), limits={"trending_topics_follow": 1})
    assert result.status == "dom_unrecognized"
    assert result.posts == []
    assert suggestion_url("AI") not in fake.visited


def test_topics_are_followed_in_screen_order_up_to_the_follow_limit() -> None:
    pages = _topic_pages()
    pages[suggestion_url("ハンドメイド")] = page(card("a", "A1", "本文"), card("a", "A1", "本文"))
    fake = FakePage(pages)
    result = collect(fake, CollectionPlan(trending=True),
                     limits={"trending_topics_follow": 2, "trending_topic": 1})  # fmt: skip
    sequence = {e["name"]: e for e in result.topic_accounting["sequence"]}
    assert (sequence["ハンドメイド"]["followed"], sequence["ハンドメイド"]["posts"]) == (True, 1)
    assert (sequence["AI"]["followed"], sequence["AI"]["posts"]) == (True, 1)
    assert sequence["旅行"]["followed"] is False


def test_topic_posts_use_the_common_card_parser() -> None:
    html = page(card("x", "X1", "本文", topic="はじめましてThreads"))
    assert parse_page(html, limit=5).posts[0].topic == "はじめましてThreads"


def test_follow_topics_can_only_lower_the_limit() -> None:
    from scripts import observe_threads

    fake = FakePage(_topic_pages())

    def build(**kwargs):
        return fake

    with pytest.raises(SystemExit):
        observe_threads.main(["--trending", "--follow-topics", "0", "--dry-run"],
                             page_factory=build)  # fmt: skip


# -- 保存の形 (手元の DB だけ) -------------------------------------------------------------


def test_the_storage_model_represents_the_topic_list_and_posts_separately(
    session: Session,
) -> None:
    fake = FakePage(_topic_pages())
    moment = datetime(2026, 9, 28, 15, 0, tzinfo=UTC)
    result = collect(fake, CollectionPlan(trending=True), limits={"trending_topics_follow": 1},
                     clock=lambda: moment)  # fmt: skip
    run = record_run(session, result)
    rows = {t.topic_name: t for t in session.scalars(select(ThreadsTrendingTopic))}
    assert set(rows) == {"ハンドメイド"}  # たどったトピックだけ (見本の投稿の数つき)
    assert rows["ハンドメイド"].source_type == "topic_for_you"  # 「トレンド」とは言わない
    assert rows["ハンドメイド"].sample_post_count == 3
    assert rows["ハンドメイド"].first_seen_at is not None
    assert rows["ハンドメイド"].last_run_id == run.id
    listing = run.artifacts_json["candidate_accounting"]["topic_list"]["sequence"]
    assert [(e["rank"], e["name"], e["kind"]) for e in listing[:3]] == [
        (1, "ハンドメイド", "topic_for_you"), (2, "AI", "topic_for_you"),
        (3, "旅行", "topic_for_you")]  # fmt: skip
    posts = session.scalars(select(ThreadsExternalPost)).all()
    assert {p.topic for p in posts} == {None, "はじめましてThreads"}
