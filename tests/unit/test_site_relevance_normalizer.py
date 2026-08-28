"""SiteRelevanceNormalizer V1 の unit テスト (完全ローカル・DB/SDK/FastAPI 非依存)。"""

import pytest

from app.keyword.normalizers.site_relevance import (
    NORMALIZER_NAME,
    NORMALIZER_VERSION,
    SITE_PROFILE_NAME,
    SITE_PROFILE_VERSION,
    calculate_site_relevance,
    normalize_keyword,
)


def _value(keyword: str) -> float:
    return calculate_site_relevance(keyword).normalized_value


# -- spec で固定された既知ケース -------------------------------------
@pytest.mark.parametrize(
    ("keyword", "expected"),
    [
        ("ChatGPT 料金", 80.0),                    # CORE_THEME のみ
        ("ChatGPT 使い方", 80.0),                  # commercial 語で変わらない
        ("AI 議事録 おすすめ", 90.0),              # CORE_THEME + ADJACENT_USE_CASE
        ("Notion AI 料金", 90.0),                  # RELEVANT_TOOL + CORE_THEME
        ("生成AI 法人 導入", 90.0),                # CORE_THEME + business context
        ("AI 業務効率化", 100.0),                  # CORE + BUSINESS_PRODUCTIVITY + context
        ("Zapier 料金", 75.0),                     # RELEVANT_TOOL のみ
        ("議事録 自動作成 ツール", 90.0),          # ADJACENT + CORE (自動作成)
        ("議事録", 60.0),                          # ADJACENT_USE_CASE のみ
        ("業務効率化", 80.0),                      # BUSINESS_PRODUCTIVITY + context
        ("一般的な ビジネス 用語", 20.0),          # relevant なし / out-of-scope なし
        ("鶏肉 レシピ", 0.0),                      # topic なし + out-of-scope
        ("東京 観光", 0.0),                        # topic なし + out-of-scope
        ("AI 旅行", 80.0),                         # topic あり -> out-of-scope で 0 にしない
    ],
)
def test_known_site_relevance_values(keyword: str, expected: float) -> None:
    assert _value(keyword) == expected


def test_commercial_intent_words_do_not_change_score() -> None:
    for kw in ("ChatGPT 料金", "ChatGPT 比較", "ChatGPT おすすめ", "ChatGPT 無料", "ChatGPT とは"):
        assert _value(kw) == 80.0
    assert _value("ChatGPT 料金") == _value("ChatGPT 使い方")


# -- 内訳の検証 --------------------------------------------------------
def test_result_breakdown_multi_group_and_context() -> None:
    result = calculate_site_relevance("AI 業務効率化")
    assert set(result.matched_groups) == {"CORE_THEME", "BUSINESS_PRODUCTIVITY"}
    assert "ai" in result.matched_terms
    assert "業務効率化" in result.matched_terms
    assert "業務" in result.business_context_terms
    assert result.base_score == 80.0
    assert result.multi_group_bonus == 10.0
    assert result.business_context_bonus == 10.0
    assert result.normalized_value == 100.0
    assert result.out_of_scope_terms == ()
    assert result.profile_name == SITE_PROFILE_NAME == "ai_business_automation"
    assert result.profile_version == SITE_PROFILE_VERSION == "v1"
    assert result.normalizer_name == NORMALIZER_NAME == "site_relevance"
    assert result.normalizer_version == NORMALIZER_VERSION == "v1"


def test_result_no_match_is_20_no_bonuses() -> None:
    result = calculate_site_relevance("一般的な ビジネス 用語")
    assert result.matched_groups == ()
    assert result.matched_terms == ()
    assert result.base_score == 20.0
    assert result.multi_group_bonus == 0.0
    assert result.business_context_bonus == 0.0
    assert result.normalized_value == 20.0


def test_result_out_of_scope_zero() -> None:
    result = calculate_site_relevance("鶏肉 レシピ")
    assert result.matched_groups == ()
    assert result.out_of_scope_terms == ("レシピ",)
    assert result.normalized_value == 0.0


def test_out_of_scope_ignored_when_topic_present() -> None:
    result = calculate_site_relevance("AI 旅行")
    assert result.matched_groups == ("CORE_THEME",)
    assert result.out_of_scope_terms == ("旅行",)  # 記録はするが 0 にはしない
    assert result.normalized_value == 80.0


def test_single_group_no_multi_bonus() -> None:
    result = calculate_site_relevance("議事録")
    assert result.matched_groups == ("ADJACENT_USE_CASE",)
    assert result.multi_group_bonus == 0.0
    assert result.normalized_value == 60.0


def test_business_context_bonus_alone_on_productivity_group() -> None:
    result = calculate_site_relevance("業務効率化")
    assert result.matched_groups == ("BUSINESS_PRODUCTIVITY",)
    assert result.business_context_bonus == 10.0  # "業務"
    assert result.multi_group_bonus == 0.0
    assert result.normalized_value == 80.0


def test_clamp_at_100() -> None:
    # base 80 + multi 10 + context 10 = 100 (超えても clamp)
    assert _value("AI 業務効率化 社内 会議") == 100.0


# -- keyword normalization ------------------------------------------
@pytest.mark.parametrize(
    "keyword",
    [
        "AI 議事録 おすすめ",
        "ai 議事録 おすすめ",
        "ＡＩ　議事録　おすすめ",          # 全角英字 + 全角スペース
        "  AI   議事録   おすすめ  ",       # 連続/前後空白
        "Ai 議事録 おすすめ",
    ],
)
def test_normalization_equivalence(keyword: str) -> None:
    assert _value(keyword) == 90.0


def test_normalize_keyword_pure() -> None:
    assert normalize_keyword("  ＡＩ　　議事録  ") == "ai 議事録"
    assert normalize_keyword("ChatGPT") == "chatgpt"
    assert normalize_keyword("Power  Automate") == "power automate"


def test_fullwidth_digits_normalized() -> None:
    # "n8n" 全角 -> 半角
    assert calculate_site_relevance("ｎ８ｎ 使い方").matched_terms == ("n8n",)


# -- ASCII false positive 回避 -----------------------------------
def test_make_matches_but_maker_does_not() -> None:
    assert "make" in calculate_site_relevance("Make 料金").matched_terms
    assert calculate_site_relevance("maker 向け サービス").matched_groups == ()
    assert _value("maker 向け サービス") == 20.0


def test_ai_not_matched_inside_other_words() -> None:
    for kw in ("domain 取得", "email マーケティング", "maintain 方法", "chair 選び"):
        assert calculate_site_relevance(kw).matched_groups == ()


def test_rpa_boundary() -> None:
    assert "rpa" in calculate_site_relevance("RPA 導入").matched_terms
    assert calculate_site_relevance("grpative なにか").matched_groups == ()


def test_deterministic() -> None:
    assert calculate_site_relevance("AI 議事録 比較") == calculate_site_relevance(
        "AI 議事録 比較"
    )


def test_matched_terms_are_subset_of_keyword_not_full_vocab() -> None:
    result = calculate_site_relevance("Notion AI 料金")
    # match したものだけ (全 vocabulary をコピーしない)
    assert set(result.matched_terms) <= {"notion", "ai"}
    assert "zapier" not in result.matched_terms
