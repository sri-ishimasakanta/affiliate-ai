"""T6.5B: 知っているアカウントのプロフィール (既定の「スレッド」のタブ)。

本物の入れ子をもとにした合成の fixture (2026-09-29 の dry-run)。見ているアカウント
(source_query)・投稿のまとまりに出ている投稿者・投稿のトピックは別のもの。
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Base, ThreadsExternalObservation, ThreadsExternalPost
from app.models.threads_observer import SOURCE_KNOWN_ACCOUNT
from app.social.threads.observer import selectors as sel
from app.social.threads.observer.collector import CollectionPlan, collect
from app.social.threads.observer.normalize import TEXT_CLEAN, normalize_body
from app.social.threads.observer.parser import CARD_OK, parse_page
from scripts import observe_threads
from tests.support.threads_observer_pages import FakePage, card, page

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "threads_observer" / (
    "known_account_verified_2026-09-29.html"
)
PROFILE_CHROME = (
    "表示名 | プロフィールの見出し", "自己紹介の一行目", "自己紹介の二行目",
    "プロフィールのトピック", "プロフィールの札", "フォロワー142人・最近の閲覧数49万回",
    "フォローする", "メッセージ", "スレッド", "返信", "メディア", "再投稿",
)  # fmt: skip


def _html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_the_observed_profile_cards_are_read_with_the_common_parser() -> None:
    result = parse_page(_html(), limit=10)
    assert result.status == "ok" and [c.reason for c in result.cards] == [CARD_OK] * 3
    plain, topic, blank = result.posts
    assert {p.author_handle for p in result.posts} == {"profile_owner"}
    assert plain.permalink == f"{sel.BASE_URL}/@profile_owner/post/PROFAAA1"
    assert plain.post_timestamp.isoformat() == "2026-09-28T13:15:34+00:00"
    assert (plain.likes, plain.replies) == (21, 2)
    assert (blank.likes, blank.replies) == (None, None)
    for post in result.posts:
        assert (post.reposts, post.shares, post.quotes) == (None, None, None)


def test_profile_ui_never_enters_post_bodies() -> None:
    for post in parse_page(_html(), limit=10).posts:
        lines = post.body_text.split("\n")
        for chrome in PROFILE_CHROME:
            assert chrome not in lines, chrome
        assert "フォロワー" not in post.body_text
        assert normalize_body(post.body_text).text_quality == TEXT_CLEAN


def test_the_profile_topic_chip_is_not_a_post_topic() -> None:
    plain, topic, blank = parse_page(_html(), limit=10).posts
    # プロフィールの札 (まとまりの外) は投稿に付けない。投稿のトピックは、そのまとまりのものだけ。
    assert plain.topic is None and blank.topic is None
    assert topic.topic == "投稿のトピック"
    assert all(p.topic != "プロフィールのトピック" for p in (plain, topic, blank))


def test_source_account_and_post_author_stay_separate() -> None:
    """見ているアカウントと投稿者を同じにしない (再投稿・引用ではずれることがある)。"""

    fake = FakePage({sel.account_url("profile_owner"): page(
        card("profile_owner", "OWN1", "本人の投稿", likes="3", replies="1"),
        card("someone_else", "OTH1", "画面に別の投稿者として出ている投稿", likes="9", replies="2"),
    )})  # fmt: skip
    result = collect(fake, CollectionPlan(known_accounts=("profile_owner",)))
    by_key = {p.record.external_post_key: p for p in result.posts}
    assert {p.source_type for p in result.posts} == {SOURCE_KNOWN_ACCOUNT}
    assert {p.source_query for p in result.posts} == {"profile_owner"}
    assert by_key["threads:OWN1"].record.author_handle == "profile_owner"
    assert by_key["threads:OTH1"].record.author_handle == "someone_else"  # 書き換えない


def test_the_account_is_never_the_post_topic_or_body() -> None:
    fake = FakePage({sel.account_url("profile_owner"): page(
        card("profile_owner", "OWN2", "トピックの無い投稿"),
    )})  # fmt: skip
    [post] = collect(fake, CollectionPlan(known_accounts=("profile_owner",))).posts
    assert post.record.topic is None
    assert post.record.features["topic"] is None
    assert "profile_owner" not in post.record.body_text


def test_thread_segments_on_a_profile() -> None:
    posts = parse_page(page(card("o", "T1", "続きの一つ目", thread_marker="1/2"),
                            card("o", "T2", "続きの二つ目", thread_marker="2/2")),
                       limit=5).posts  # fmt: skip
    assert [p.body_text for p in posts] == ["続きの一つ目", "続きの二つ目"]
    assert all(p.features["numeric_facts_count"] == 0 for p in posts)


def test_known_account_accounting_and_limit() -> None:
    cards = [card("o", f"K{i}", f"本文{i}") for i in range(8)]
    fake = FakePage({sel.account_url("profile_owner"): page(*cards)})
    result = collect(fake, CollectionPlan(known_accounts=("profile_owner",)),
                     limits={"run_total": 5})  # fmt: skip
    summary = result.accounting_summary()
    assert len(result.posts) == 5
    assert summary["by_reason"] == {"accepted": 5, "filtered_over_limit": 3}
    assert summary["candidate_cards"] == 8 and summary["complete"]
    [source] = summary["sources"]
    assert source["surface_selector_version"] == "threads-known-account-verified-2026-09-29-v1"
    assert source["surface_verified"] is True


def test_known_account_storage_is_eligible_only_after_verification(capsys) -> None:
    assert sel.surface_verification(SOURCE_KNOWN_ACCOUNT)["verified"] is True
    assert sel.surface_verification("custom_feed")["verified"] is False
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    fake = FakePage({sel.account_url("profile_owner"): page(card("profile_owner", "S1", "本文"))})
    code = observe_threads.main(["--account", "profile_owner"], session_factory=factory,
                                page_factory=lambda **kwargs: fake)  # fmt: skip
    assert code == observe_threads.EXIT_OK
    assert json.loads(capsys.readouterr().out)["stored"] is True
    with factory() as session:
        [obs] = session.scalars(select(ThreadsExternalObservation)).all()
        assert obs.source_type == SOURCE_KNOWN_ACCOUNT and obs.source_query == "profile_owner"
        assert session.scalars(select(ThreadsExternalPost)).one().author_handle == "profile_owner"
    # 確かめていない画面を含むと、保存は断る。
    refused = observe_threads.main(["--account", "profile_owner", "--custom-feed", "f"],
                                   session_factory=factory, page_factory=lambda **kwargs: fake)
    assert refused == observe_threads.EXIT_USAGE
