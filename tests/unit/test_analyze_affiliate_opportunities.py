"""scripts/analyze_affiliate_opportunities.py の分析ロジック (純粋部分) の unit テスト。

DB は使わない。affiliate_opportunity の採点は行わない (このフェーズでは分析のみ)。
"""

import csv
from pathlib import Path

import pytest

from app.keyword.affiliate_matching import match_programs
from scripts.analyze_affiliate_opportunities import (
    FIT_CSV_COLUMNS,
    ProgramFacts,
    _bucket_counts,
    _print_program_details,
    _print_summary,
    _write_csv,
    analyze_keyword,
    csv_fieldnames,
    fit_gap_warnings,
    load_keywords,
    render_table,
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


# ==========================================================================
# C2.5.4: strong / weak tier の報告 (追加のみ。matched = legacy の covered は不変)
# ==========================================================================
_LEGACY_CSV_COLUMNS = [
    "keyword",
    "matched_program_count",
    "distinct_provider_count",
    "commission_data_count",
    "fixed_commission_count",
    "percentage_commission_count",
    "best_percentage_commission_value",
    "best_fixed_by_currency",
    "matched_program_ids",
    "matched_program_names",
    "active_providers",
    "matched_terms",
]
_TIER_CSV_COLUMNS = [
    "strong_program_count",
    "weak_program_count",
    "strong_program_names",
    "weak_program_names",
    "no_strong_affiliate_match",
    "alias_only_strong_program_names",
]


def _tier_programs() -> list[ProgramFacts]:
    return [
        _prog(1, name="Make", terms=("Make", "業務効率化")),
        _prog(2, name="HubSpot", terms=("HubSpot", "CRM")),
        _prog(3, name="Pipedrive", terms=("Pipedrive", "CRM")),
    ]


def test_analysis_reports_strong_and_weak_without_changing_the_legacy_match() -> None:
    programs = _tier_programs()
    for keyword in ("Make 料金", "crm", "hubspot crm", "make sure", "ハブスポット"):
        assert analyze_keyword(keyword, programs).matched == match_programs(keyword, programs)
    make = analyze_keyword("Make 料金", programs)
    assert make.matched_program_count == 1
    assert (make.strong_program_count, make.weak_program_count) == (1, 0)
    assert make.strong_program_names == ["Make"] and make.no_strong_affiliate_match is False
    crm = analyze_keyword("crm", programs)
    assert crm.matched_program_count == 2  # legacy の covered
    assert (crm.strong_program_count, crm.weak_program_count) == (0, 2)
    assert crm.weak_program_names == ["HubSpot", "Pipedrive"]
    assert crm.no_strong_affiliate_match is True
    both = analyze_keyword("hubspot crm", programs)
    assert (both.strong_program_names, both.weak_program_names) == (["HubSpot"], ["Pipedrive"])


def test_analysis_marks_make_idioms_as_not_strong_but_still_legacy_matched() -> None:
    programs = _tier_programs()
    sure = analyze_keyword("make sure", programs)
    assert sure.matched_program_count == 1  # legacy の covered は変えない
    assert sure.strong_program_count == 0 and sure.weak_program_count == 1
    assert sure.no_strong_affiliate_match is True


def test_analysis_alias_only_strong_is_not_added_to_the_legacy_match() -> None:
    result = analyze_keyword("ハブスポット とは", _tier_programs())
    assert result.matched_program_count == 0 and result.matched == []
    assert result.strong_program_names == ["HubSpot"] and result.no_strong_affiliate_match is False
    assert result.alias_only_strong_program_names == ["HubSpot"]  # 明示的に識別できる
    # legacy でも match する program は alias-only ではない
    both = analyze_keyword("hubspot crm ハブスポット", _tier_programs())
    assert both.alias_only_strong_program_names == []


def test_csv_appends_tier_columns_and_keeps_the_existing_columns_in_order(tmp_path: Path) -> None:
    analyses = [analyze_keyword(k, _tier_programs()) for k in ("Make 料金", "crm", "chatgpt")]
    fields = csv_fieldnames(analyses)
    assert fields[: len(_LEGACY_CSV_COLUMNS)] == _LEGACY_CSV_COLUMNS
    # C2.5.4 の tier 列の後ろに C2.5.7 の fit 列 (legacy / tier 列の位置は後方互換のため不変)
    tail = _TIER_CSV_COLUMNS + list(FIT_CSV_COLUMNS)
    assert fields[-len(tail) :] == tail
    out = tmp_path / "a.csv"
    _write_csv(out, analyses)
    rows = {r["keyword"]: r for r in csv.DictReader(out.open(encoding="utf-8", newline=""))}
    assert rows["Make 料金"]["matched_program_count"] == "1"
    assert rows["Make 料金"]["strong_program_count"] == "1"
    assert rows["Make 料金"]["no_strong_affiliate_match"] == "False"
    assert rows["crm"]["matched_program_count"] == "2"
    assert rows["crm"]["strong_program_count"] == "0"
    assert rows["crm"]["weak_program_names"] == "HubSpot | Pipedrive"
    assert rows["crm"]["alias_only_strong_program_names"] == ""
    assert rows["crm"]["no_strong_affiliate_match"] == "True"
    assert rows["chatgpt"]["weak_program_count"] == "0"
    assert rows["chatgpt"]["no_strong_affiliate_match"] == "True"


def test_table_and_summary_lead_with_the_fit_policy(capsys) -> None:
    analyses = [analyze_keyword(k, _tier_programs()) for k in ("Make 料金", "crm", "chatgpt")]
    table = render_table(analyses)
    header = table.splitlines()[0].split()
    assert header == [
        "keyword",
        "coverage",
        "aff_opp",
        "eligible",
        "strong",
        "core",
        "loose",
        "unreviewed",
        "legacy_any",
    ]
    rows = {line.split()[0]: line.split() for line in table.splitlines()[2:]}
    assert rows["crm"][1:2] == ["core_weak"] and rows["chatgpt"][1:2] == ["none"]
    _print_summary(analyses)
    out = capsys.readouterr().out
    assert "eligible = strong OR weak+core" in out
    assert "keywords_monetizable           : 2" in out
    assert "strong                         : 1" in out
    assert "core_weak (no brand, core fit) : 1" in out
    assert "context_only (loose/unreviewed): 0" in out
    assert "no_match                       : 1" in out
    assert "keywords_with_alias_only_strong (not in the legacy match): 0" in out
    # legacy の any-term 集計は残るが、収益化の意味ではないと明示される
    assert "NOT monetization" in out
    assert "keywords_with_matches   : 2" in out


def test_program_details_show_tier_fit_eligibility_and_alias_only_programs(capsys) -> None:
    _print_program_details([analyze_keyword("hubspot crm ハブスポット", _tier_programs())])
    out = capsys.readouterr().out
    # strong は fit に依存せず適格 (同時に踏んだ generic term の fit は参考情報として出る)
    assert "tier=strong fit=core | scoring_eligible=True primary_eligible=True" in out
    assert "tier=weak fit=core | scoring_eligible=True primary_eligible=True" in out  # Pipedrive
    assert "[alias-only strong" not in out  # legacy でも match している program は通常行に出る
    _print_program_details([analyze_keyword("ハブスポット", _tier_programs())])
    assert "HubSpot [alias-only strong, not in the legacy match]" in capsys.readouterr().out
    _print_program_details([analyze_keyword("業務効率化 ツール", _tier_programs())])
    out = capsys.readouterr().out
    assert "tier=weak fit=loose | scoring_eligible=False primary_eligible=False" in out
    assert "loose=業務効率化" in out and "coverage=context_only" in out


# ==========================================================================
# C2.5.7: brand tier x fit (strong OR weak+core が収益化の対象)
# ==========================================================================
def _fit_programs() -> list[ProgramFacts]:
    return [
        _prog(1, name="HubSpot", provider="Impact", terms=("HubSpot", "CRM", "業務効率化"),
              commission_type="percentage", commission_value=30),
        _prog(2, name="Pipedrive", provider="PartnerStack", terms=("Pipedrive", "CRM"),
              commission_type="percentage", commission_value=20),
        _prog(3, name="Acme Notes", terms=("Acme", "ノート術")),  # fit config に無い program
    ]


def test_coverage_classes_and_eligibility_follow_the_fit_policy() -> None:
    programs = _fit_programs()
    strong = analyze_keyword("HubSpot 料金", programs)
    assert strong.coverage_class == "strong" and strong.affiliate_opportunity > 0
    core = analyze_keyword("crm 比較", programs)
    assert core.coverage_class == "core_weak"
    assert core.core_weak_program_names == ["HubSpot", "Pipedrive"]
    assert core.scoring_eligible_program_names == ["HubSpot", "Pipedrive"]
    assert core.primary_eligible_program_names == ["HubSpot", "Pipedrive"]
    loose = analyze_keyword("業務効率化 ツール", programs)
    assert loose.coverage_class == "context_only"
    assert loose.loose_weak_program_names == ["HubSpot"] and loose.eligible == []
    assert loose.affiliate_opportunity == 0.0
    assert loose.matched_program_count == 1  # legacy any-term は match 扱いのまま (後方互換の列)
    unrev = analyze_keyword("ノート術 まとめ", programs)
    assert unrev.coverage_class == "context_only"
    assert unrev.unreviewed_weak_program_names == ["Acme Notes"]
    assert unrev.affiliate_opportunity == 0.0 and unrev.primary_eligible_program_names == []
    assert analyze_keyword("chatgpt", programs).coverage_class == "none"


def test_affiliate_opportunity_column_uses_the_production_formula_over_eligible_only() -> None:
    from app.keyword.affiliate_tiers import match_catalog, scoring_programs
    from app.keyword.normalizers.affiliate_opportunity import calculate_affiliate_opportunity

    programs = _fit_programs()
    for keyword in ("crm 比較", "crm 業務効率化", "HubSpot 料金", "業務効率化", "ノート術"):
        expected = calculate_affiliate_opportunity(
            scoring_programs(match_catalog(keyword, programs))
        ).normalized_value
        assert analyze_keyword(keyword, programs).affiliate_opportunity == expected, keyword
    # loose の term を足しても値は変わらない (weak の重みは無い)
    assert (
        analyze_keyword("crm 業務効率化", programs).affiliate_opportunity
        == analyze_keyword("crm", programs).affiliate_opportunity
    )


def test_analysis_shares_the_production_japanese_spacing_match() -> None:
    programs = [_prog(1, name="HubSpot", terms=("HubSpot", "顧客管理"))]
    split = analyze_keyword("顧客 管理 ツール", programs)
    assert split.coverage_class == "core_weak"


def test_csv_fit_columns(tmp_path: Path) -> None:
    import json

    keywords = ("crm 業務効率化", "ノート術", "chatgpt")
    analyses = [analyze_keyword(k, _fit_programs()) for k in keywords]
    out = tmp_path / "fit.csv"
    _write_csv(out, analyses)
    rows = {r["keyword"]: r for r in csv.DictReader(out.open(encoding="utf-8", newline=""))}
    crm = rows["crm 業務効率化"]
    assert crm["coverage_class"] == "core_weak"
    assert crm["scoring_eligible_program_count"] == "2"
    assert crm["primary_eligible_program_names"] == "HubSpot | Pipedrive"
    assert crm["core_weak_program_names"] == "HubSpot | Pipedrive"
    assert float(crm["affiliate_opportunity"]) > 0
    details = {d["name"]: d for d in json.loads(crm["match_details"])}
    assert details["HubSpot"]["core_terms"] == ["CRM"]
    assert details["HubSpot"]["loose_terms"] == ["業務効率化"]
    assert details["HubSpot"]["brand_tier"] == "weak" and details["HubSpot"]["fit"] == "core"
    unrev = rows["ノート術"]
    assert unrev["coverage_class"] == "context_only"
    assert unrev["unreviewed_weak_program_names"] == "Acme Notes"
    assert unrev["scoring_eligible_program_count"] == "0"
    assert float(unrev["affiliate_opportunity"]) == 0.0
    assert rows["chatgpt"]["coverage_class"] == "none" and rows["chatgpt"]["match_details"] == "[]"


def test_fit_gap_warnings_for_unrepresented_programs_and_terms() -> None:
    programs = [
        _prog(1, name="HubSpot", terms=("HubSpot", "CRM", "新しい term")),  # term だけ未分類
        _prog(2, name="Acme Notes", terms=("Acme", "ノート術")),  # program ごと未掲載
        _prog(3, name="Pipedrive", terms=("Pipedrive", "CRM")),  # 網羅済み
    ]
    warnings = fit_gap_warnings(programs)
    assert len(warnings) == 2
    assert "'Acme Notes' is not in affiliate_match_fit.json" in warnings[1]
    assert "ノート術" in warnings[1] and "score 0, never primary" in warnings[1]
    assert "'HubSpot' has generic terms not in" in warnings[0] and "新しい term" in warnings[0]
    assert "Acme" not in warnings[1].split(":")[-1]  # own-name term は brand tier 扱いで対象外
    # 未掲載 program の generic term は core にならない (fail-closed)
    result = analyze_keyword("ノート術 入門", programs)
    assert result.unreviewed_weak_program_names == ["Acme Notes"] and result.eligible == []
