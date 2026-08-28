"""app/article/planning.py の純粋ロジックのテスト (DB / 外部 API 非依存)。"""

import pytest

from app.article.planning import (
    CANNIBALIZATION_THRESHOLD,
    COMPLIANCE_CHECKLIST,
    QUALITY_GUARDRAILS,
    ArticleType,
    build_outline,
    cannibalization_guidance,
    classify_article_type,
    comparison_axes,
    display_text,
    search_intent_summary,
    suggest_slug,
    theme_of,
    working_title,
)


@pytest.mark.parametrize(
    ("keyword", "expected"),
    [
        ("業務効率化 ツール おすすめ", ArticleType.RECOMMENDATION_ROUNDUP),
        ("AI 議事録 おすすめ", ArticleType.RECOMMENDATION_ROUNDUP),
        ("生成AI ツール 比較", ArticleType.COMPARISON_LISTICLE),
        ("ChatGPT 使い方", ArticleType.HOW_TO),
        ("AI 業務効率化 導入", ArticleType.HOW_TO),
        ("生成AI とは", ArticleType.CATEGORY_LANDING),
    ],
)
def test_classify_article_type(keyword: str, expected: ArticleType) -> None:
    assert classify_article_type(keyword).article_type is expected


def test_target_keyword_is_recommendation_roundup() -> None:
    result = classify_article_type("業務効率化 ツール おすすめ")
    assert result.article_type is ArticleType.RECOMMENDATION_ROUNDUP
    assert result.matched_marker == "おすすめ"


def test_classify_priority_howto_beats_comparison_beats_recommend() -> None:
    # 使い方(how_to) > 比較(comparison) > おすすめ(recommendation) > とは(category)
    assert (
        classify_article_type("ツール 比較 おすすめ").article_type
        is ArticleType.COMPARISON_LISTICLE
    )
    assert (
        classify_article_type("ツール 使い方 比較").article_type
        is ArticleType.HOW_TO
    )
    assert (
        classify_article_type("ツール おすすめ とは").article_type
        is ArticleType.RECOMMENDATION_ROUNDUP
    )


def test_classify_undetermined_returns_none() -> None:
    result = classify_article_type("業務効率化 ツール")
    assert result.article_type is None
    assert result.matched_marker is None


def test_theme_strips_trailing_markers_and_despaces_japanese() -> None:
    # trailing 修飾語を除去し、日本語 token 間の空白は詰める
    assert theme_of("業務効率化 ツール おすすめ") == "業務効率化ツール"
    assert theme_of("生成ai ツール 比較 おすすめ") == "生成aiツール"
    assert theme_of("おすすめ") == "おすすめ"  # 1 token は残す


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("業務効率化 ツール おすすめ", "業務効率化ツールおすすめ"),
        ("業務効率化 ツール", "業務効率化ツール"),
        ("AI 議事録 おすすめ", "AI議事録おすすめ"),
        ("生成AI ツール 比較", "生成AIツール比較"),
        # ASCII/Latin 英数字どうしの境界の空白は維持する
        ("ChatGPT Plus 料金", "ChatGPT Plus料金"),
        ("Notion AI 料金", "Notion AI料金"),
        ("  余分な   空白  ", "余分な空白"),
        ("", ""),
    ],
)
def test_display_text_despaces_japanese_keeps_latin_spacing(raw: str, expected: str) -> None:
    assert display_text(raw) == expected
    # casefold しない (原文の大小を保つ)
    assert display_text("ChatGPT Plus 料金") == "ChatGPT Plus料金"


def test_working_title_deterministic_and_despaced() -> None:
    kw = "業務効率化 ツール おすすめ"
    t1 = working_title(kw, ArticleType.RECOMMENDATION_ROUNDUP)
    assert t1 == working_title(kw, ArticleType.RECOMMENDATION_ROUNDUP)
    assert t1 == "業務効率化ツールおすすめ｜選び方と目的別おすすめ比較"
    assert "業務効率化ツール" in t1  # 不要な空白なし
    assert working_title(kw, None) != t1  # 未確定は別文言


def test_suggest_slug_deterministic_and_type_token() -> None:
    s1 = suggest_slug("業務効率化 ツール おすすめ", ArticleType.RECOMMENDATION_ROUNDUP)
    s2 = suggest_slug("業務効率化 ツール おすすめ", ArticleType.RECOMMENDATION_ROUNDUP)
    assert s1 == s2 == "業務効率化-ツール-おすすめ-roundup"
    # family keyword は本体で既に区別される
    assert suggest_slug("業務効率化 ツール 無料", None) != s1
    # ascii only の keyword
    assert suggest_slug("ChatGPT 使い方", ArticleType.HOW_TO) == "chatgpt-使い方-howto"


def test_suggest_slug_avoids_collision_with_is_taken() -> None:
    taken = {"業務効率化-ツール-おすすめ-roundup"}
    got = suggest_slug(
        "業務効率化 ツール おすすめ",
        ArticleType.RECOMMENDATION_ROUNDUP,
        is_taken=lambda s: s in taken,
    )
    assert got == "業務効率化-ツール-おすすめ-roundup-2"


def test_roundup_outline_structure() -> None:
    sections = build_outline("業務効率化 ツール おすすめ", ArticleType.RECOMMENDATION_ROUNDUP)
    levels = [s.level for s in sections]
    headings = " / ".join(s.heading for s in sections)
    assert levels[0] == "H1"
    assert "intro" in levels
    assert levels.count("H2") >= 5
    assert "H3" in levels
    assert "選び方" in headings
    assert "比較" in headings
    assert "目的別おすすめ" in headings
    assert "よくある質問" in headings
    assert "まとめ" in headings
    # 日本語 theme 内の不要な空白がない
    assert "業務効率化ツールとは" in headings
    assert "業務効率化ツールの選び方" in headings
    assert "おすすめ業務効率化ツール比較" in headings
    assert "業務効率化 ツール" not in headings
    # 本文そのものは持たない (purpose / required_elements のみ)
    for s in sections:
        assert isinstance(s.required_elements, tuple)


def test_outline_undetermined_type_requests_human_review() -> None:
    sections = build_outline("業務効率化 ツール", None)
    assert len(sections) == 1
    assert "article_type" in sections[0].required_elements[0]


def test_comparison_axes_flags_future_research() -> None:
    axes = dict(comparison_axes())
    assert axes["料金（月額 / 年額）"] == "future_research_required"
    assert axes["無料プランの有無"] == "future_research_required"
    assert axes["カテゴリ（カタログ分類）"] == "catalog"
    # 推測値で埋めない: 大半が future_research_required
    assert sum(1 for v in axes.values() if v == "future_research_required") >= 8


def test_compliance_and_guardrails_fixed_content() -> None:
    joined_c = " ".join(COMPLIANCE_CHECKLIST)
    assert "PR" in joined_c and "誇大" in joined_c and "出典" in joined_c or "公式" in joined_c
    joined_g = " ".join(QUALITY_GUARDRAILS)
    assert "1 keyword = 1" in joined_g
    assert "cannibalization" in joined_g
    assert "primary source" in joined_g
    assert "approved 後" in joined_g
    assert "一括" in joined_g


def test_search_intent_summary() -> None:
    assert "推薦" in search_intent_summary("x おすすめ", ArticleType.RECOMMENDATION_ROUNDUP)
    assert "確定できない" in search_intent_summary("x", None)


def test_cannibalization_guidance_intent_differentiation() -> None:
    # target 相当: originality 低 + most_similar = 無料系
    guidance = cannibalization_guidance(
        "業務効率化 ツール おすすめ",
        ArticleType.RECOMMENDATION_ROUNDUP,
        27.27,
        "業務効率化 ツール 無料",
    )
    assert "業務効率化 ツール 無料" in guidance
    assert "推薦" in guidance
    assert "無料" in guidance
    assert "内部リンク" in guidance
    assert "編集者" in guidance


def test_cannibalization_guidance_high_originality_is_soft() -> None:
    guidance = cannibalization_guidance(
        "何か とは", ArticleType.CATEGORY_LANDING, 80.0, "別の keyword"
    )
    assert "重大な重複は検出されていない" in guidance
    assert CANNIBALIZATION_THRESHOLD == 40.0


def test_planning_output_never_contains_tracking_url() -> None:
    # pure module は URL / tracking を一切扱わない
    import app.article.planning as mod

    src = mod.__file__
    text = open(src, encoding="utf-8").read().lower()
    assert "tracking_url" not in text
    assert "http://" not in text and "https://" not in text
