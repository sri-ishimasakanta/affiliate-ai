"""T6.5B: 観察したページの読み取り (fail closed・見えない値は None)。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.social.threads.observer import selectors as sel
from app.social.threads.observer.parser import (
    PAGE_DOM_UNRECOGNIZED,
    PAGE_EMPTY,
    PAGE_LOGIN_REQUIRED,
    PAGE_OK,
    parse_count,
    parse_page,
    parse_trending_topics,
)
from tests.support.threads_observer_pages import card, drifted_page, login_page, page, trends_page


@pytest.mark.parametrize(
    ("text", "value"),
    [("71", 71), ("3,456", 3456), ("1.2万", 12000), ("3千", 3000), ("1.2K", 1200),
     ("2M", 2_000_000), ("0", 0)],
)  # fmt: skip
def test_counts_are_read(text: str, value: int) -> None:
    assert parse_count(text) == value


@pytest.mark.parametrize("text", [None, "", "いいね", "--", "1.2億"])
def test_unreadable_counts_are_none_not_zero(text) -> None:
    assert parse_count(text) is None


def test_a_post_card_is_read() -> None:
    html = page(card("alice", "ABC1", "一行目\n二行目？", likes="1.2万", replies="12",
                     reposts="3", shares="1", topic="生成AI", image=True,
                     link="https://example.com/x"))  # fmt: skip
    result = parse_page(html, limit=10)
    assert result.status == PAGE_OK
    [post] = result.posts
    assert post.external_post_key == "threads:ABC1"
    assert post.author_handle == "alice"
    assert post.permalink == f"{sel.BASE_URL}/@alice/post/ABC1"
    assert post.body_text == "一行目\n二行目？"
    assert post.topic == "生成AI"
    assert post.media_type == "image"
    assert post.has_link is True
    assert (post.likes, post.replies) == (12000, 12)
    # 再投稿・共有は画面の数の意味を確かめていないので読まない。引用は画面に別に出ない。
    assert (post.reposts, post.shares, post.quotes) == (None, None, None)
    assert post.post_timestamp is not None
    assert post.features["cta_class"] == "question"
    # 名前・リンクの文字は本文に入らない。
    assert "alice" not in post.body_text


def test_missing_metrics_stay_none() -> None:
    html = page(card("bob", "B1", "本文", likes=None, replies="2", reposts=None, shares=None))
    [post] = parse_page(html, limit=10).posts
    assert post.likes is None
    assert post.replies == 2
    assert post.reposts is None and post.shares is None


def test_the_profile_picture_is_not_media_and_video_is_detected() -> None:
    [plain] = parse_page(page(card("c", "C1", "本文")), limit=5).posts
    [clip] = parse_page(page(card("c", "C2", "本文", video=True)), limit=5).posts
    assert plain.media_type == "none"
    assert clip.media_type == "video"


def test_limit_and_duplicates() -> None:
    cards = [card("d", f"D{i}", f"本文{i}") for i in range(8)]
    result = parse_page(page(*cards, cards[0]), limit=5)
    assert [p.external_post_key for p in result.posts] == [f"threads:D{i}" for i in range(5)]
    again = parse_page(page(cards[0], cards[0], cards[1]), limit=10)
    assert len(again.posts) == 2


def test_a_quoted_post_inside_a_card_is_not_a_second_post() -> None:
    quoted = card("q", "Q1", "引用された投稿")
    html = page(card("e", "E1", "外側の投稿", inner=quoted))
    result = parse_page(html, limit=10)
    assert [p.external_post_key for p in result.posts] == ["threads:E1"]


def test_login_page_requires_a_human() -> None:
    result = parse_page(login_page(), limit=10)
    assert result.status == PAGE_LOGIN_REQUIRED
    assert result.posts == []


def test_dom_drift_fails_closed() -> None:
    result = parse_page(drifted_page(), limit=10)
    assert result.status == PAGE_DOM_UNRECOGNIZED
    assert result.posts == []
    assert sel.SELECTOR_VERSION in result.reason


def test_mostly_broken_cards_fail_the_whole_page() -> None:
    broken = '<div data-pressable-container="true"><span dir="auto">リンクなし</span></div>'
    result = parse_page(page(broken, broken, card("f", "F1", "本文")), limit=10)
    assert result.status == PAGE_DOM_UNRECOGNIZED
    assert result.posts == []
    assert result.rejected == 2


def test_one_broken_card_among_many_is_skipped_and_counted() -> None:
    broken = '<div data-pressable-container="true"><span dir="auto">リンクなし</span></div>'
    cards = [card("g", f"G{i}", f"本文{i}") for i in range(3)]
    result = parse_page(page(broken, *cards), limit=10)
    assert result.status == PAGE_OK
    assert result.rejected == 1
    assert len(result.posts) == 3


def test_a_card_without_body_is_rejected() -> None:
    empty_body = card("h", "H1", "")
    result = parse_page(page(empty_body, card("h", "H2", "本文")), limit=10)
    assert result.rejected == 1


def test_empty_document() -> None:
    assert parse_page("", limit=5).status == PAGE_EMPTY


def test_trending_topics() -> None:
    result = parse_trending_topics(trends_page("A", "B", "B", "C", "D", "E", "F"), limit=5)
    assert result.status == PAGE_OK
    assert result.trending_topics == ["A", "B", "C", "D", "E"]
    assert parse_trending_topics(login_page(), limit=5).status == PAGE_LOGIN_REQUIRED
    assert parse_trending_topics(drifted_page(), limit=5).status == PAGE_DOM_UNRECOGNIZED


def test_selectors_record_exactly_what_the_pilot_verified() -> None:
    assert sel.SELECTOR_VERIFIED is True
    assert sel.SELECTOR_VERSION == "threads-web-verified-2026-09-29-v3"
    assert set(sel.METRIC_LABELS.values()) == set(sel.VERIFIED_METRICS) == {"likes", "replies"}
    for name in ("views", "quotes", "reposts", "shares"):
        assert name not in sel.METRIC_LABELS.values()
    assert {"reposts", "shares", "quotes"} <= set(sel.UNVERIFIED_SURFACES)
    assert not set(sel.VERIFIED_SURFACES) & set(sel.UNVERIFIED_SURFACES)


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "threads_observer" / (
    "for_you_verified_2026-09-28.html"
)


def test_the_exact_observed_card_shape() -> None:
    """パイロットで見た本物の入れ子 (文字・名前・コードは合成) を読む。"""

    result = parse_page(FIXTURE.read_text(encoding="utf-8"), limit=10)
    assert result.status == PAGE_OK and result.rejected == 0
    topic, blank, image = result.posts
    assert topic.external_post_key == "threads:CODEAAA1"
    assert topic.author_handle == "user_topic"
    assert topic.permalink == f"{sel.BASE_URL}/@user_topic/post/CODEAAA1"
    assert topic.post_timestamp.isoformat() == "2026-09-28T12:05:46+00:00"
    assert topic.topic == "テストトピック"
    assert (topic.likes, topic.replies) == (6, 1)
    # 数が出ていない (0 は空で表示される) → None。0 と決めつけない。
    assert (blank.likes, blank.replies) == (None, None)
    assert blank.topic is None and blank.media_type == "none"
    assert (image.media_type, image.likes, image.replies) == ("image", 1, 1)
    for post in result.posts:
        assert (post.reposts, post.shares, post.quotes) == (None, None, None)
        # 見出しの相対時刻・ボタンの数・名前・トピックは本文に入らない。
        lines = post.body_text.split("\n")
        assert not any(re.fullmatch(r"\d+(分|時間|日)", line) for line in lines), lines
        assert not any(line.isdigit() for line in lines), lines
        assert post.author_handle not in post.body_text
        assert "テストトピック" not in post.body_text


def test_relative_time_and_counts_never_enter_the_body() -> None:
    [post] = parse_page(page(card("z", "Z1", "本文の一行目\n二行目", likes="16", replies="1",
                                  reposts="2")), limit=5).posts  # fmt: skip
    assert post.body_text == "本文の一行目\n二行目"


def test_an_unlabelled_or_unknown_icon_is_not_read_as_a_metric() -> None:
    html = page(card("y", "Y1", "本文", likes="4").replace('title="「いいね！」"', 'title="x"')
                .replace("<title>「いいね！」</title>", "<title>x</title>"))  # fmt: skip
    [post] = parse_page(html, limit=5).posts
    assert post.likes is None
