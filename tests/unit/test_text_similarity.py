"""app/keyword/text_similarity.py の unit テスト (pure、標準ライブラリのみ)。"""

import pytest

from app.keyword.text_similarity import (
    character_bigram_dice,
    sequence_similarity,
    text_similarity,
)


# -- character_bigram_dice ------------------------------------------
def test_bigram_exact_equal_is_one() -> None:
    assert character_bigram_dice("ai議事録", "ai議事録") == 1.0


def test_bigram_both_empty_is_one() -> None:
    assert character_bigram_dice("", "") == 1.0


def test_bigram_one_empty_is_zero() -> None:
    assert character_bigram_dice("ai議事録", "") == 0.0
    assert character_bigram_dice("", "ai議事録") == 0.0


def test_bigram_no_overlap_is_zero() -> None:
    assert character_bigram_dice("abcd", "wxyz") == 0.0


def test_bigram_partial_overlap() -> None:
    # "abcd" -> {ab,bc,cd} (3) / "abcdef" -> {ab,bc,cd,de,ef} (5) / overlap 3
    value = character_bigram_dice("abcd", "abcdef")
    assert value == pytest.approx(2 * 3 / (3 + 5))


def test_bigram_single_char_unequal_is_zero() -> None:
    # 1 文字 -> bigram 作れない。不一致なら 0.0 (SequenceMatcher で救済)
    assert character_bigram_dice("a", "b") == 0.0


def test_bigram_single_char_equal_is_one() -> None:
    assert character_bigram_dice("a", "a") == 1.0


def test_bigram_in_unit_range() -> None:
    for a, b in [("ai議事録おすすめ", "ai議事録比較"), ("chatgpt", "rpa"), ("x", "xy")]:
        assert 0.0 <= character_bigram_dice(a, b) <= 1.0


# -- sequence_similarity -----------------------------------------
def test_sequence_exact_equal_is_one() -> None:
    assert sequence_similarity("ai議事録", "ai議事録") == 1.0


def test_sequence_different_is_low() -> None:
    assert sequence_similarity("chatgpt料金", "rpa比較") < 0.3


# -- text_similarity -------------------------------------------
def test_similarity_is_max_of_two_methods() -> None:
    result = text_similarity("AI 議事録", "AI 議事録 おすすめ")
    assert result.similarity == max(result.bigram_dice, result.sequence_matcher)
    assert 0.0 <= result.similarity <= 1.0


def test_similarity_exact_equal_is_one() -> None:
    result = text_similarity("AI 議事録", "AI 議事録")
    assert result.similarity == 1.0
    assert result.bigram_dice == 1.0
    assert result.sequence_matcher == 1.0


def test_similarity_nfkc_casefold_whitespace() -> None:
    # 全角英字 + 全角スペース + 連続空白 + 大文字 -> 完全一致扱い
    assert text_similarity("ＡＩ　議事録", "  ai   議事録  ").similarity == 1.0
    assert text_similarity("ChatGPT", "chatgpt").similarity == 1.0


def test_similarity_completely_different_is_low() -> None:
    assert text_similarity("ChatGPT 料金", "RPA 比較").similarity < 0.3
    assert text_similarity("AI 議事録 おすすめ", "生成AI 法人 導入").similarity < 0.35


def test_similarity_deterministic() -> None:
    a, b = "AI 議事録 おすすめ", "AI 議事録 比較"
    assert text_similarity(a, b) == text_similarity(a, b)


def test_similarity_whitespace_removed_before_compare() -> None:
    # "AI 議事録" と "AI議事録" は空白除去後に同一
    assert text_similarity("AI 議事録", "AI議事録").similarity == 1.0
