"""app/keyword/equivalence.py — 日本語 phrase の空白差だけを吸収する比較専用 key。"""

from __future__ import annotations

import pytest

from app.keyword.equivalence import contains_japanese, equivalence_key
from app.keyword.normalizers.site_relevance import normalize_keyword


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("タスク 管理 ツール", "タスク管理 ツール"),
        ("タスク 管理 ツール", "タスク管理ツール"),
        ("AI 議事録 おすすめ", "AI議事録おすすめ"),
        ("Make 料金", "Make料金"),
        ("AI 議事録 おすすめ", "ai 議事 録 おすすめ"),  # Google Ads が返す分かち書き
        ("ＡＩ　議事録", "ai議事録"),  # 全角英数 / 全角空白 (NFKC)
        ("業務効率化   ツール", "業務効率化ツール"),
        ("ChatGPT Plus 料金", "chatgpt plus料金"),
    ],
)
def test_japanese_phrases_are_equivalent_across_whitespace_differences(a: str, b: str) -> None:
    assert equivalence_key(a) == equivalence_key(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("タスク管理 ツール", "タスク管理 アプリ"),  # 空白以外の差は吸収しない (fuzzy にしない)
        ("Make 料金", "Make 無料"),
        ("AI 議事録 おすすめ", "AI 議事録 比較"),
        ("業務効率化 ツール", "業務効率化"),
    ],
)
def test_genuinely_different_phrases_stay_different(a: str, b: str) -> None:
    assert equivalence_key(a) != equivalence_key(b)


def test_english_only_phrases_keep_normal_whitespace_semantics() -> None:
    assert equivalence_key("google meet") == "google meet"
    assert equivalence_key("google meet") != equivalence_key("googlemeet")
    assert equivalence_key("Google   Meet") == equivalence_key("google meet")  # 通常の空白正規化
    assert equivalence_key("CRM Software") == normalize_keyword("CRM Software")
    assert equivalence_key("n8n cloud") != equivalence_key("n8ncloud")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("タスク管理", True),  # カタカナ + 漢字
        ("ひらがな", True),
        ("議事録", True),
        ("々", True),
        ("ｶﾀｶﾅ", True),  # 半角カナ (NFKC で全角化)
        ("Make 料金", True),
        ("google meet", False),
        ("crm 2026", False),
        ("한국어", False),  # ハングルは日本語ではない
        ("", False),
    ],
)
def test_contains_japanese(text: str, expected: bool) -> None:
    assert contains_japanese(text) is expected


def test_existing_normalization_is_unchanged_and_display_text_is_not_touched() -> None:
    assert normalize_keyword("タスク 管理 ツール") == "タスク 管理 ツール"  # 空白は除去されない
    original = "タスク 管理 ツール"
    equivalence_key(original)
    assert original == "タスク 管理 ツール"
