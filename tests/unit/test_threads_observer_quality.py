"""T6.5B.1: 観察の質 — 本文に画面の部品を入れない・候補を残らず勘定する・元の記録は変えない。"""

from __future__ import annotations

import re

import pytest

from app.models.threads_observer import (
    RUN_ACCOUNTING_MISMATCH,
    RUN_PARTIAL,
    RUN_SUCCEEDED,
)
from app.social.threads.features import extract
from app.social.threads.observer import selectors as sel
from app.social.threads.observer.collector import (
    OUTCOME_BY_REASON,
    OUTCOMES,
    REASON_FILTERED_OVER_LIMIT,
    REASON_LATE_INSERTED,
    CollectionPlan,
    collect,
)
from app.social.threads.observer.normalize import (
    FLAG_META_AI_LABEL_REMOVED,
    FLAG_THREAD_MARKER_REMOVED,
    NORMALIZER_VERSION,
    TEXT_CLEAN,
    TEXT_CONTAMINATED,
    TEXT_UI_CHROME_REMOVED,
    normalize_body,
)
from app.social.threads.observer.parser import (
    CARD_DUPLICATE_IN_FRAME,
    CARD_MALFORMED_EMPTY_BODY,
    CARD_MALFORMED_NO_PERMALINK,
    CARD_OK,
    CARD_UNSUPPORTED_NESTED,
    CARD_UNSUPPORTED_OUTSIDE,
    parse_page,
)
from tests.support.threads_observer_pages import FakePage, card, page


def _cards(prefix: str, n: int) -> list[str]:
    return [card(prefix, f"{prefix.upper()}{i}", f"{prefix} の投稿 {i}") for i in range(n)]


# -- 本文 (DOM の段階) --------------------------------------------------------------------


@pytest.mark.parametrize("marker", ["1/2", "2/2", "1/3"])
def test_the_thread_marker_never_enters_the_body(marker: str) -> None:
    [post] = parse_page(page(card("a", "A1", "収益を出すぞ！！", likes="2", replies="1",
                                  thread_marker=marker)), limit=5).posts  # fmt: skip
    assert post.body_text == "収益を出すぞ！！"
    assert post.features["numeric_facts_count"] == 0
    assert (post.likes, post.replies) == (2, 1)


def test_an_authored_fraction_is_kept() -> None:
    [post] = parse_page(page(card("b", "B1", "1/2の確率で当たる\n確率は1/2です",
                                  thread_marker="1/2")), limit=5).posts  # fmt: skip
    assert post.body_text == "1/2の確率で当たる\n確率は1/2です"
    assert post.features["numeric_facts_count"] == extract("1/2の確率で当たる\n確率は1/2です")\
        .numeric_facts_count  # fmt: skip


def test_topic_marker_author_age_and_counts_combined_stay_out_of_the_body() -> None:
    [post] = parse_page(page(card("c", "C1", "本文の一行目\n二行目", likes="16", replies="1",
                                  topic="インサイト祭り", thread_marker="1/2")),
                        limit=5).posts  # fmt: skip
    assert post.body_text == "本文の一行目\n二行目"
    assert post.topic == "インサイト祭り"
    for chrome in ("インサイト祭り", "c", "16", "1日", "1/2"):
        assert chrome not in post.body_text.split("\n"), chrome


def test_a_single_post_and_a_second_segment() -> None:
    posts = parse_page(page(card("d", "D1", "単体の投稿"),
                            card("d", "D2", "続きの二つ目", thread_marker="2/2")),
                       limit=5).posts  # fmt: skip
    assert [p.body_text for p in posts] == ["単体の投稿", "続きの二つ目"]


# -- まとまりの結果の理由 ---------------------------------------------------------------


def test_every_card_on_a_page_gets_one_reason() -> None:
    no_link = '<div data-pressable-container="true"><span dir="auto">リンクなし</span></div>'
    empty = card("e", "E2", "")
    nested = card("e", "E3", "外側", inner=card("q", "Q1", "引用"))
    outside = ('<div><a href="/@z/post/Z9"><time datetime="2026-09-27T01:00:00Z">1日</time>'
               "</a></div>")  # fmt: skip
    html = page(card("e", "E1", "本文"), card("e", "E1", "本文"), no_link, empty, nested,
                *_cards("f", 3), outside)  # fmt: skip
    result = parse_page(html, limit=99)
    reasons = [c.reason for c in result.cards]
    assert reasons == [CARD_OK, CARD_DUPLICATE_IN_FRAME, CARD_MALFORMED_NO_PERMALINK,
                       CARD_MALFORMED_EMPTY_BODY, CARD_OK, CARD_UNSUPPORTED_NESTED,
                       CARD_OK, CARD_OK, CARD_OK, CARD_UNSUPPORTED_OUTSIDE]  # fmt: skip
    # 時刻のリンクから独立に数えたキーは、すべて理由つきのまとまりに入っている。
    assert set(result.anchor_keys) <= {c.key for c in result.cards if c.key}
    assert result.rejected == 2


# -- 候補の勘定 (collector) --------------------------------------------------------------


def _summary(result) -> dict:
    return result.accounting_summary()


def test_accounting_equality_with_the_limit() -> None:
    fake = FakePage({sel.for_you_url(): page(*_cards("g", 7))})
    result = collect(fake, CollectionPlan(for_you=True), limits={"for_you": 5})
    summary = _summary(result)
    assert result.status == RUN_SUCCEEDED
    assert len(result.posts) == 5
    assert summary["candidate_cards"] == 7 == sum(summary["by_outcome"].values())
    assert summary["by_reason"] == {"accepted": 5, REASON_FILTERED_OVER_LIMIT: 2}
    assert summary["equality_holds"] and summary["complete"]
    sequence = summary["sources"][0]["sequence"]
    assert [e["key"] for e in sequence] == [f"threads:G{i}" for i in range(7)]
    assert all(e["outcome"] in OUTCOMES and e["reason"] in OUTCOME_BY_REASON for e in sequence)


def test_the_same_card_seen_in_later_frames_is_one_candidate() -> None:
    frames = [page(*_cards("h", 2)), page(*_cards("h", 4)), page(*_cards("h", 6))]
    fake = FakePage({sel.for_you_url(): frames})
    result = collect(fake, CollectionPlan(for_you=True), limits={"for_you": 5})
    summary = _summary(result)
    assert summary["candidate_cards"] == 6
    assert summary["by_reason"] == {"accepted": 5, REASON_FILTERED_OVER_LIMIT: 1}


def test_virtualized_cards_are_counted_but_not_lost() -> None:
    frames = [page(*_cards("v", 3)), page(*[card("v", f"V{i}", f"本文{i}") for i in (2, 3, 4)])]
    fake = FakePage({sel.for_you_url(): frames})
    result = collect(fake, CollectionPlan(for_you=True), limits={"for_you": 5})
    source = _summary(result)["sources"][0]
    assert source["virtualized_out"] == 2
    assert source["complete"] and len(result.posts) == 5


def test_a_card_inserted_above_after_the_read_has_a_reason() -> None:
    """2026-09-28 に診断で見た形: 読んだ後、読んだ投稿の間に投稿が差し込まれる。"""

    before = page(*_cards("i", 6))
    cards = _cards("i", 6)
    after = page(*cards[:2], card("late", "LATE1", "後から差し込まれた"), *cards[2:])
    fake = FakePage({sel.for_you_url(): [before, before, after]})
    fake.advance_on_wait = True
    result = collect(fake, CollectionPlan(for_you=True), limits={"for_you": 3})
    summary = _summary(result)
    assert [p.record.external_post_key for p in result.posts] == [
        "threads:I0", "threads:I1", "threads:I2"]  # fmt: skip
    assert summary["by_reason"][REASON_LATE_INSERTED] == 1
    assert summary["complete"] is True
    assert result.status == RUN_PARTIAL and REASON_LATE_INSERTED in result.reason


def test_a_card_appearing_below_the_read_range_is_not_a_candidate() -> None:
    before = page(*_cards("j", 3))
    after = page(*_cards("j", 3), card("tail", "T1", "下に増えた"))
    fake = FakePage({sel.for_you_url(): [before, before, after]})
    fake.advance_on_wait = True
    result = collect(fake, CollectionPlan(for_you=True), limits={"for_you": 3})
    assert _summary(result)["candidate_cards"] == 3
    assert result.status == RUN_SUCCEEDED


def test_a_silent_skip_makes_the_run_fail_closed() -> None:
    # 投稿のまとまりの中に、別の投稿の時刻のリンクが入っている (まとまりが無い投稿)。
    hidden = ('<div><a href="/@x/post/HIDDEN1"><time datetime="2026-09-27T01:00:00Z">1日</time>'
              "</a></div>")  # fmt: skip
    fake = FakePage({sel.for_you_url(): page(card("k", "K1", "本文", inner=hidden),
                                             *_cards("m", 3))})  # fmt: skip
    result = collect(fake, CollectionPlan(for_you=True))
    assert result.status == RUN_ACCOUNTING_MISMATCH
    assert result.posts == []
    source = _summary(result)["sources"][0]
    assert source["unaccounted_anchor_keys"] == ["threads:HIDDEN1"]
    assert source["complete"] is False


def test_duplicates_are_explicit_and_do_not_fail_the_run() -> None:
    fake = FakePage({sel.for_you_url(): page(card("n", "N1", "本文"), card("n", "N1", "本文"),
                                             *_cards("o", 2))})  # fmt: skip
    result = collect(fake, CollectionPlan(for_you=True), limits={"for_you": 5})
    summary = _summary(result)
    assert summary["by_outcome"]["duplicate"] == 1
    assert summary["candidate_cards"] == 4 and summary["complete"]
    assert len(result.posts) == 3


def test_a_malformed_card_is_counted_once_across_frames() -> None:
    broken = '<div data-pressable-container="true"><span dir="auto">x</span></div>'
    fake = FakePage({sel.for_you_url(): page(broken, *_cards("p", 3))})
    result = collect(fake, CollectionPlan(for_you=True), limits={"for_you": 5})
    assert _summary(result)["by_outcome"]["malformed"] == 1
    assert result.rejected == 1


def test_the_limit_counts_accepted_posts_from_the_encountered_stream() -> None:
    """``--limit-total 5`` = 読んだ候補の流れから受け入れた最大 5 件 (画面の先頭 5 件ではない)"""

    broken = '<div data-pressable-container="true"><span dir="auto">x</span></div>'
    fake = FakePage({sel.for_you_url(): page(broken, *_cards("r", 6))})
    result = collect(fake, CollectionPlan(for_you=True), limits={"run_total": 5})
    assert len(result.posts) == 5
    summary = _summary(result)
    assert summary["by_reason"] == {"accepted": 5, CARD_MALFORMED_NO_PERMALINK: 1,
                                    REASON_FILTERED_OVER_LIMIT: 1}  # fmt: skip


# -- 分析用の本文 (履歴の行を書き換えない) --------------------------------------------------


def test_the_legacy_thread_marker_is_removed_for_analysis_only() -> None:
    raw = "noteで安定した収益を出すぞ！！ 1/2"
    result = normalize_body(raw)
    assert result.raw_body == raw  # 元の本文はそのまま
    assert result.analysis_body == "noteで安定した収益を出すぞ！！"
    assert result.normalization_flags == (FLAG_THREAD_MARKER_REMOVED,)
    assert result.text_quality == TEXT_UI_CHROME_REMOVED
    assert result.normalization_version == NORMALIZER_VERSION
    assert result.raw_body_hash != result.normalized_body_hash
    assert extract(raw).numeric_facts_count == 2
    assert extract(result.analysis_body).numeric_facts_count == 0


@pytest.mark.parametrize("raw", ["meta.ai 今現在、どんな投稿が伸びる？", "meta.ai\n今現在の話"])
def test_the_meta_ai_label_is_removed_for_analysis_only(raw: str) -> None:
    result = normalize_body(raw)
    assert not result.analysis_body.startswith("meta.ai")
    assert result.analysis_body.startswith("今現在")
    assert result.normalization_flags == (FLAG_META_AI_LABEL_REMOVED,)


@pytest.mark.parametrize(
    "raw",
    ["確率は1/2です", "1/2の確率で当たる", "今日はmeta.aiで調べた", "meta.aiについて", "半分 1/2"],
)
def test_authored_text_is_not_touched(raw: str) -> None:
    result = normalize_body(raw)
    assert result.analysis_body == raw
    assert result.text_quality == TEXT_CLEAN
    assert result.normalization_flags == ()


def test_normalization_is_deterministic_and_idempotent() -> None:
    raw = "meta.ai 本文 1/2"
    once = normalize_body(raw)
    assert once == normalize_body(raw)
    assert normalize_body(once.analysis_body).analysis_body == once.analysis_body


def test_a_body_that_is_only_chrome_is_contaminated() -> None:
    assert normalize_body("meta.ai ").text_quality == TEXT_CONTAMINATED


def test_body_extraction_never_uses_a_broad_regex() -> None:
    # 本文の途中の「数/数」や「meta.ai」を落とす置き換えは、parser にも normalizer にも無い。
    [post] = parse_page(page(card("s", "S1", "A案 1/2、B案 1/2。meta.ai も試した")),
                        limit=5).posts  # fmt: skip
    assert post.body_text == "A案 1/2、B案 1/2。meta.ai も試した"
    assert normalize_body(post.body_text).analysis_body == post.body_text
    assert re.search(r"\d+/\d+", post.body_text)


def test_a_quoted_post_does_not_lend_its_metrics_to_the_outer_post() -> None:
    """2026-09-28 の dry-run で見た形: 外側 ♡5 💬2、引用した投稿 ♡5 💬8。"""

    quoted = card("quoted", "Q1", "引用された投稿", likes="5", replies="8", reposts="2",
                  topic="引用のトピック", image=True, thread_marker="1/3")
    outer = card("outer", "O1", "よし。\n四の五の言わずにやる。", likes="5", replies="2",
                 reposts="1", inner=quoted)
    result = parse_page(page(outer), limit=5)
    [post] = result.posts
    assert post.external_post_key == "threads:O1"
    assert (post.likes, post.replies) == (5, 2)
    assert post.body_text == "よし。\n四の五の言わずにやる。"
    assert post.topic is None and post.media_type == "none"
    assert [c.reason for c in result.cards] == [CARD_OK, CARD_UNSUPPORTED_NESTED]


def test_the_outer_post_without_counts_stays_none_even_if_the_quote_has_counts() -> None:
    quoted = card("quoted", "Q2", "引用", likes="40", replies="7")
    outer = card("outer", "O2", "外側", likes=None, replies=None, inner=quoted)
    [post] = parse_page(page(outer), limit=5).posts
    assert (post.likes, post.replies) == (None, None)
