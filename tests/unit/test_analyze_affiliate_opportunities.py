"""scripts/analyze_affiliate_opportunities.py の分析ロジック (純粋部分) の unit テスト。

DB は使わない。affiliate_opportunity の採点は行わない (このフェーズでは分析のみ)。
"""

import csv
from pathlib import Path

import pytest

from scripts.analyze_affiliate_opportunities import (
    ProgramFacts,
    _bucket_counts,
    _write_csv,
    analyze_keyword,
    csv_fieldnames,
    load_keywords,
)


def _prog(
    program_id: int,
    *,
    name: str | None = None,
    provider: str | None = "direct",
    category: str | None = "ai",
    commission_type: str | None = None,
    commission_value: float | None = None,
    currency: str | None = None,
    terms: tuple[str, ...] = (),
) -> ProgramFacts:
    return ProgramFacts(
        program_id=program_id,
        name=name or f"Prog{program_id}",
        provider=provider,
        category=category,
        commission_type=commission_type,
        commission_value=commission_value,
        currency=currency,
        match_terms=terms,
    )


# -- matching rules --------------------------------------------------
def test_japanese_substring_match() -> None:
    programs = [_prog(1, terms=("議事録", "AI 議事録"))]
    result = analyze_keyword("AI 議事録 おすすめ", programs)
    assert result.matched_program_count == 1
    assert set(result.matched[0].matched_terms) == {"議事録", "AI 議事録"}


def test_ascii_boundary_match_and_false_positive() -> None:
    programs = [_prog(1, name="Make", terms=("Make",))]
    assert analyze_keyword("Make 料金", programs).matched_program_count == 1
    assert analyze_keyword("MAKE 料金", programs).matched_program_count == 1  # casefold
    assert analyze_keyword("maker 比較", programs).matched_program_count == 0  # boundary
    assert analyze_keyword("makers エコ", programs).matched_program_count == 0


def test_ascii_term_not_matched_inside_other_word() -> None:
    programs = [_prog(1, terms=("RPA",))]
    assert analyze_keyword("RPA 導入", programs).matched_program_count == 1
    assert analyze_keyword("grpative について", programs).matched_program_count == 0


def test_nfkc_normalization() -> None:
    programs = [_prog(1, terms=("AI 議事録",))]
    # 全角英字 + 全角スペース
    assert analyze_keyword("ＡＩ　議事録　おすすめ", programs).matched_program_count == 1


def test_no_match_keeps_empty_analysis() -> None:
    programs = [_prog(1, terms=("議事録",))]
    result = analyze_keyword("ChatGPT 料金", programs)
    assert result.matched_program_count == 0
    assert result.matched == []
    assert result.matched_terms == []
    assert result.distinct_provider_count == 0


def test_multiple_programs_matched() -> None:
    programs = [
        _prog(1, provider="a", terms=("議事録",)),
        _prog(2, provider="b", terms=("AI 議事録",)),
        _prog(3, provider="c", terms=("文字起こし",)),  # no match
    ]
    result = analyze_keyword("AI 議事録 比較", programs)
    assert result.matched_program_count == 2
    assert result.matched_program_ids == [1, 2]
    assert result.distinct_provider_count == 2


def test_shared_provider_direct_counts_programs_but_one_provider() -> None:
    programs = [
        _prog(1, provider="direct", terms=("業務効率化",)),
        _prog(2, provider="direct", terms=("業務効率化",)),
        _prog(3, provider="direct", terms=("業務効率化",)),
    ]
    result = analyze_keyword("業務効率化 ツール おすすめ", programs)
    assert result.matched_program_count == 3
    assert result.distinct_provider_count == 1
    assert result.active_providers == ["direct"]


# -- commission aggregation ----------------------------------------
def test_commission_fixed_and_percentage_kept_separate() -> None:
    programs = [
        _prog(1, terms=("議事録",), commission_type="fixed", commission_value=25, currency="USD"),
        _prog(2, terms=("議事録",), commission_type="percentage", commission_value=30),
        _prog(3, terms=("議事録",), commission_type="percentage", commission_value=10),
        _prog(4, terms=("議事録",)),  # commission なし
    ]
    result = analyze_keyword("AI 議事録", programs)
    assert result.matched_program_count == 4
    assert result.commission_data_count == 3
    assert result.fixed_commission_count == 1
    assert result.percentage_commission_count == 2
    assert result.best_percentage_commission_value == 30.0
    assert result.best_fixed_by_currency == {"USD": 25.0}


def test_commission_currency_not_mixed() -> None:
    programs = [
        _prog(1, terms=("議事録",), commission_type="fixed", commission_value=25, currency="USD"),
        _prog(2, terms=("議事録",), commission_type="fixed", commission_value=3000, currency="JPY"),
        _prog(3, terms=("議事録",), commission_type="fixed", commission_value=40, currency="USD"),
    ]
    result = analyze_keyword("AI 議事録", programs)
    # USD と JPY を統合しない。currency 別に最大値。
    assert result.best_fixed_by_currency == {"USD": 40.0, "JPY": 3000.0}
    best_value, best_currency = result.best_fixed_commission
    assert (best_value, best_currency) == (3000.0, "JPY")  # 生の最大値 (横断比較不能)


def test_zero_match_has_empty_commission() -> None:
    result = analyze_keyword("RPA 導入", [_prog(1, terms=("議事録",))])
    assert result.commission_data_count == 0
    assert result.best_fixed_by_currency == {}
    assert result.best_percentage_commission_value is None


# -- load_keywords ------------------------------------------------
def test_load_keywords_dedup_trim_drop_empty() -> None:
    result = load_keywords(["  AI 議事録 ", "AI 議事録", "", "  ", "ChatGPT 料金"], None)
    assert result == ["AI 議事録", "ChatGPT 料金"]


def test_load_keywords_from_input_csv(tmp_path: Path) -> None:
    path = tmp_path / "kw.csv"
    path.write_text("keyword\nAI 議事録\nChatGPT 料金\nAI 議事録\n", encoding="utf-8")
    result = load_keywords(["ChatGPT 料金"], path)  # CLI + CSV マージ + 重複除去
    assert result == ["ChatGPT 料金", "AI 議事録"]


def test_load_keywords_input_csv_missing_column(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("term\nx\n", encoding="utf-8")
    with pytest.raises(ValueError, match="keyword"):
        load_keywords(None, path)


# -- CSV output --------------------------------------------------
def test_write_csv_columns_and_values(tmp_path: Path) -> None:
    programs = [
        _prog(4, name="Descript", provider="PartnerStack", terms=("議事録",),
              commission_type="fixed", commission_value=25, currency="USD"),
        _prog(11, name="Fireflies", provider="direct", terms=("AI 議事録",),
              commission_type="percentage", commission_value=10),
        _prog(20, name="JpyProg", provider="direct", terms=("議事録",),
              commission_type="fixed", commission_value=5000, currency="JPY"),
    ]
    analyses = [
        analyze_keyword("AI 議事録 おすすめ", programs),
        analyze_keyword("RPA 導入", programs),  # zero match は残す
    ]
    out = tmp_path / "analysis.csv"
    _write_csv(out, analyses)

    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    fieldnames = csv_fieldnames(analyses)
    assert "best_fixed_USD" in fieldnames
    assert "best_fixed_JPY" in fieldnames  # currency をまとめない
    assert "best_fixed_by_currency" in fieldnames

    matched, zero = rows
    assert matched["keyword"] == "AI 議事録 おすすめ"
    assert matched["matched_program_count"] == "3"
    assert matched["distinct_provider_count"] == "2"
    assert matched["fixed_commission_count"] == "2"
    assert matched["percentage_commission_count"] == "1"
    assert matched["best_percentage_commission_value"] == "10.0"
    assert matched["best_fixed_USD"] == "25.0"
    assert matched["best_fixed_JPY"] == "5000.0"
    assert "Descript" in matched["matched_program_names"]
    assert matched["active_providers"] == "PartnerStack | direct" or (
        matched["active_providers"] == "direct | PartnerStack"
    )

    assert zero["keyword"] == "RPA 導入"
    assert zero["matched_program_count"] == "0"
    assert zero["best_fixed_USD"] == ""
    assert zero["matched_program_names"] == ""


def test_bucket_counts() -> None:
    assert _bucket_counts([0, 0, 1, 2, 3, 7, 3]) == (2, 1, 1, 3)
