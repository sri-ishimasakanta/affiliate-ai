"""T6.5B: フィード (組み込み / 自分で作ったもの) と、確かめていない画面を保存しないこと。

2026-09-29 の確認: このアカウントには、自分で作ったフィードへのリンクが無い (組み込みだけ)。
"""

from __future__ import annotations

import json

from app.models.threads_observer import SOURCE_CUSTOM_FEED
from app.social.threads.observer import selectors as sel
from app.social.threads.observer.collector import CollectionPlan, collect
from app.social.threads.observer.normalize import TEXT_CLEAN, normalize_body
from app.social.threads.observer.parser import (
    FEED_BUILT_IN,
    FEED_CUSTOM_CANDIDATE,
    parse_feed_links,
)
from scripts import observe_threads
from tests.support.threads_observer_pages import FakePage, card, page

#: 2026-09-29 に見た左のメニューの形 (リンクの部分だけ)。
SIDEBAR = (
    '<nav><a role="link" href="/for_you"><span>おすすめ</span></a>'
    '<a role="link" href="/following"><span>フォロー中</span></a>'
    '<div><span>他のフィード</span><div role="button"><span>編集</span></div></div>'
    '<a role="link" href="/following/"><span>フォロー中</span></a>'
    '<a role="link" href="/saved/"><span>保存済み</span></a>'
    '<a role="link" href="/liked/"><span>「いいね！」済み</span></a>'
    '<div role="button"><span>表示を増やす</span></div>'
    '<a role="link" href="/search?q=aithreads&amp;serp_type=tags&amp;tag_id=1">'
    "<span>AI Threads</span></a></nav>"
)


def _html(extra: str = "") -> str:
    return f"<html><body>{SIDEBAR}{extra}<main></main></body></html>"


def test_only_built_in_feeds_on_the_observed_sidebar() -> None:
    links = parse_feed_links(_html())
    assert {link.href for link in links} == {"/for_you", "/following", "/following/", "/saved/",
                                             "/liked/"}  # fmt: skip
    assert {link.kind for link in links} == {FEED_BUILT_IN}
    # 「表示を増やす」「編集」はボタン (リンクではない) → フィードに数えない。
    assert not any(link.name in ("表示を増やす", "編集") for link in links)
    # コミュニティ (serp_type=tags) はフィードではない。
    assert not any("serp_type" in link.href for link in links)


def test_a_custom_feed_link_would_be_a_candidate_not_a_built_in() -> None:
    extra = '<a role="link" href="/custom_feed/abc123"><span>AIの話題</span></a>'
    links = {link.href: link for link in parse_feed_links(_html(extra))}
    assert links["/custom_feed/abc123"].kind == FEED_CUSTOM_CANDIDATE
    assert links["/custom_feed/abc123"].name == "AIの話題"
    assert links["/saved/"].kind == FEED_BUILT_IN


def test_the_custom_feed_is_recorded_as_unavailable_for_this_account() -> None:
    availability = sel.SURFACE_AVAILABILITY["custom_feed"]
    assert availability["available"] is False
    assert availability["checked_at"] == "2026-09-29"
    assert "/saved/" in availability["built_in_feeds_observed"]
    assert sel.surface_verification(SOURCE_CUSTOM_FEED)["verified"] is False
    # 組み込みのフィードは、自分で作ったフィードの代わりとして「確かめた」にしない。
    assert "custom_feed" not in sel.SURFACE_VERIFICATION


def test_the_feed_name_is_never_the_post_topic() -> None:
    """出どころ (custom_feed)・フィードの名前 / ID・投稿のトピックは別のもの。"""

    fake = FakePage({sel.custom_feed_url("AIの話題"): page(
        card("a", "A1", "フィードの中の投稿", likes="3", replies="1"),
        card("b", "B1", "トピックつきの投稿", topic="インサイト祭り", likes=None, replies=None),
    )})  # fmt: skip
    result = collect(fake, CollectionPlan(custom_feeds=("AIの話題",)))
    by_key = {p.record.external_post_key: p for p in result.posts}
    assert {p.source_type for p in result.posts} == {SOURCE_CUSTOM_FEED}
    assert {p.source_query for p in result.posts} == {"AIの話題"}
    assert by_key["threads:A1"].record.topic is None
    assert by_key["threads:B1"].record.topic == "インサイト祭り"
    for post in result.posts:
        assert "AIの話題" not in post.record.body_text
        assert post.record.features["topic"] != "AIの話題"
        assert normalize_body(post.record.body_text).text_quality == TEXT_CLEAN
    assert (by_key["threads:B1"].record.likes, by_key["threads:B1"].record.replies) == (None, None)
    [source] = result.accounting_summary()["sources"]
    assert source["complete"] and source["surface_verified"] is False


def test_custom_feed_accounting_and_limit() -> None:
    cards = [card("f", f"F{i}", f"本文{i}") for i in range(7)]
    fake = FakePage({sel.custom_feed_url("feed1"): page(*cards)})
    result = collect(fake, CollectionPlan(custom_feeds=("feed1",)), limits={"custom_feed": 5})
    summary = result.accounting_summary()
    assert len(result.posts) == 5
    assert summary["by_reason"] == {"accepted": 5, "filtered_over_limit": 2}
    assert summary["candidate_cards"] == 7 and summary["complete"]


def _factory(fake):
    def build(**kwargs):
        build.opened = True
        return fake

    build.opened = False
    return build


def test_an_unverified_surface_is_never_stored(capsys) -> None:
    build = _factory(FakePage({}))
    for args in (["--custom-feed", "feed1"], ["--account", "someone"]):
        assert observe_threads.main(args, page_factory=build) == observe_threads.EXIT_USAGE
        assert "not verified" in capsys.readouterr().out
    assert build.opened is False  # ブラウザを開いていない


def test_an_unverified_surface_can_still_be_dry_run(capsys) -> None:
    fake = FakePage({sel.custom_feed_url("feed1"): page(card("g", "G1", "本文"))})
    build = _factory(fake)
    code = observe_threads.main(["--custom-feed", "feed1", "--dry-run"], page_factory=build)
    assert code == observe_threads.EXIT_OK
    summary = json.loads(capsys.readouterr().out)
    assert summary["stored"] is False
    assert summary["surfaces"] == [{"source_type": "custom_feed", "surface_selector_version": None,
                                    "surface_verified": False}]  # fmt: skip
