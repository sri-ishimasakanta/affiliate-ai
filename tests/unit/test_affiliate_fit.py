"""Affiliate match-term FIT (core / loose / unreviewed) と brand tier との独立性 (C2.5.7)。"""

import json
from copy import deepcopy

import pytest

from app.affiliate.catalog_hygiene import load_hygiene_config
from app.keyword.affiliate_fit import (
    DEFAULT_FIT_CONFIG_PATH,
    FIT_CORE,
    FIT_LOOSE,
    FIT_UNREVIEWED,
    FitConfigError,
    aggregate_fit,
    default_fit_config,
    parse_fit_config,
)
from app.keyword.affiliate_matching import ProgramFacts
from app.keyword.affiliate_tiers import (
    TIER_STRONG,
    TIER_WEAK,
    is_own_name_term,
    match_catalog,
    scoring_programs,
)


def _facts(pid: int, name: str, terms: list[str], provider: str = "direct") -> ProgramFacts:
    return ProgramFacts(
        program_id=pid,
        name=name,
        provider=provider,
        category=None,
        commission_type="percentage",
        commission_value=20.0,
        currency=None,
        match_terms=tuple(terms),
    )


def _raw() -> dict:
    return json.loads(DEFAULT_FIT_CONFIG_PATH.read_text(encoding="utf-8"))


def _by_program(raw: dict) -> dict[str, list[dict]]:
    return {p["program"]: p["terms"] for p in raw["programs"]}


# ============================================================ the committed classification
def test_committed_config_classifies_every_active_catalog_term() -> None:
    raw = _raw()
    programs = _by_program(raw)
    assert len(programs) == 16  # active な program (paused / unknown の 3 件は対象外)
    entries = [t for terms in programs.values() for t in terms]
    brand = [t for t in entries if t.get("kind") == "brand"]
    generic = [t for t in entries if t.get("kind", "generic") == "generic"]
    counts = {
        fit: sum(1 for t in generic if t["fit"] == fit)
        for fit in (FIT_CORE, FIT_LOOSE, FIT_UNREVIEWED)
    }
    # 118 term = brand / alias 19 + generic 99 (ToDo / TODO は正規化後に同一: 1 entry)
    assert len(brand) == 19
    assert counts == {FIT_CORE: 38, FIT_LOOSE: 49, FIT_UNREVIEWED: 11}
    assert len(brand) + len(generic) == 117


def test_every_generic_term_has_an_audit_reason_and_evidence() -> None:
    for name, terms in _by_program(_raw()).items():
        for t in terms:
            assert t["reason"].strip(), (name, t["term"])
            if t.get("kind") != "brand":
                assert t["evidence"] in {
                    "spec_confirmed",
                    "audit_live_hits",
                    "audit_reasoning_only",
                }
                # core は根拠のあるものだけ。定義だけの core は unreviewed にする
                if t["fit"] == FIT_CORE:
                    assert t["evidence"] != "audit_reasoning_only", (name, t["term"])


def test_unreviewed_terms_are_exactly_the_core_labels_without_audit_evidence() -> None:
    unreviewed = {
        (name, t["term"])
        for name, terms in _by_program(_raw()).items()
        for t in terms
        if t.get("fit") == FIT_UNREVIEWED
    }
    assert unreviewed == {
        ("Descript", "動画編集 AI"),
        ("Descript", "ポッドキャスト 編集"),
        ("Grammarly Pro upgrade", "英文校正"),
        *(
            ("Semrush AI Visibility Toolkit", x)
            for x in ("AI Visibility", "LLM SEO", "生成AI SEO", "検索可視性")
        ),
        *(
            ("Semrush SEO Toolkit", x)
            for x in ("SEOツール", "キーワード調査", "検索順位", "SEO 分析")
        ),
    }
    for terms in _by_program(_raw()).values():
        for t in terms:
            if t.get("fit") == FIT_UNREVIEWED:
                assert t["audit_fit"] == "core" and t["evidence"] == "audit_reasoning_only"


def test_brand_kind_is_exactly_the_terms_the_tier_derivation_calls_strong() -> None:
    for name, terms in _by_program(_raw()).items():
        for t in terms:
            derived = is_own_name_term(name, t["term"])
            assert (t.get("kind") == "brand") == derived, (name, t["term"])


@pytest.mark.parametrize(
    ("program", "term", "fit"),
    [
        # core (spec で確認済み)
        ("HubSpot", "CRM", FIT_CORE),
        ("HubSpot", "顧客管理", FIT_CORE),
        ("HubSpot", "MAツール", FIT_CORE),
        ("HubSpot", "マーケティング自動化", FIT_CORE),
        ("Pipedrive", "SFA", FIT_CORE),
        ("Pipedrive", "営業支援", FIT_CORE),
        ("ClickUp", "タスク管理", FIT_CORE),
        ("ClickUp", "プロジェクト管理", FIT_CORE),
        ("monday.com", "プロジェクト管理", FIT_CORE),
        ("Fireflies.ai", "AI 議事録", FIT_CORE),
        ("Krisp", "ノイズキャンセル", FIT_CORE),
        ("Todoist", "タスク管理", FIT_CORE),
        ("Reclaim.ai", "AI スケジュール", FIT_CORE),
        ("Make", "業務自動化", FIT_CORE),
        ("Make", "SaaS連携", FIT_CORE),
        ("n8n Cloud", "業務自動化", FIT_CORE),
        ("Writesonic", "AIライティング", FIT_CORE),
        ("ActiveCampaign", "メール自動化", FIT_CORE),
        # loose (既知の inflation)
        ("HubSpot", "業務効率化", FIT_LOOSE),
        ("Pipedrive", "業務効率化", FIT_LOOSE),
        ("ClickUp", "業務効率化", FIT_LOOSE),
        ("monday.com", "業務効率化", FIT_LOOSE),
        ("Todoist", "業務効率化", FIT_LOOSE),
        ("Reclaim.ai", "業務効率化", FIT_LOOSE),
        ("Make", "自動化", FIT_LOOSE),
        ("n8n Cloud", "自動化", FIT_LOOSE),
        ("Descript", "議事録", FIT_LOOSE),
        ("Descript", "文字起こし", FIT_LOOSE),
        ("Descript", "AI 文字起こし", FIT_LOOSE),
        ("Descript", "音声文字起こし", FIT_LOOSE),
        ("Writesonic", "生成AI", FIT_LOOSE),
        ("ActiveCampaign", "CRM", FIT_LOOSE),
        ("ActiveCampaign", "顧客管理", FIT_LOOSE),
        # 同じ term でも program ごとに fit は違う
        ("Fireflies.ai", "文字起こし", FIT_CORE),
        ("Krisp", "文字起こし", FIT_CORE),
    ],
)
def test_audited_examples(program: str, term: str, fit: str) -> None:
    assert default_fit_config().fit_for(program, term).fit == fit


def test_every_hygiene_removal_is_loose_and_every_before_term_is_classified() -> None:
    cfg = default_fit_config()
    for change in load_hygiene_config().changes:
        for term in change.before:
            assert (
                cfg.fit_for(change.program, term).reason
                != "term is not in the fit config (no audit evidence)"
            )
        for term in change.remove:
            assert cfg.fit_for(change.program, term).fit == FIT_LOOSE, (change.program, term)


def test_unknown_program_or_term_is_unreviewed_never_core() -> None:
    cfg = default_fit_config()
    assert cfg.fit_for("HubSpot", "not a listed term").fit == FIT_UNREVIEWED
    assert cfg.fit_for("Brand New Program", "CRM").fit == FIT_UNREVIEWED
    assert "no audit evidence" in cfg.fit_for("Brand New Program", "CRM").reason


def test_ordinary_word_brand_terms_have_an_explicit_weak_fit() -> None:
    cfg = default_fit_config()
    assert cfg.fit_for("Make", "Make").fit == FIT_LOOSE  # 'make sure' / bare 'make'
    assert cfg.fit_for("monday.com", "monday").fit == FIT_LOOSE
    assert cfg.fit_for("Reclaim.ai", "Reclaim").fit == FIT_LOOSE
    assert (
        cfg.fit_for("Fireflies.ai", "Fireflies").fit == FIT_UNREVIEWED
    )  # 判断保留 (SERP/GSC 待ち)
    # weak になり得ない brand term は fit を決めていない (使われたら unreviewed)
    assert cfg.fit_for("HubSpot", "HubSpot").fit == FIT_UNREVIEWED


# ============================================================ parser
def test_aggregate_fit_prefers_core_then_unreviewed_then_loose() -> None:
    assert aggregate_fit([]) is None
    assert aggregate_fit([FIT_LOOSE, FIT_CORE, FIT_UNREVIEWED]) == FIT_CORE
    assert aggregate_fit([FIT_LOOSE, FIT_UNREVIEWED]) == FIT_UNREVIEWED
    assert aggregate_fit([FIT_LOOSE, FIT_LOOSE]) == FIT_LOOSE


def _mut(mutator) -> None:
    raw = deepcopy(_raw())
    mutator(raw)
    with pytest.raises(FitConfigError):
        parse_fit_config(raw)


def test_parser_rejects_malformed_configs() -> None:
    parse_fit_config(_raw())  # 正常系
    _mut(lambda r: r.update(version=2))
    _mut(lambda r: r.update(surprise=1))
    _mut(lambda r: r.update(programs=[]))
    _mut(lambda r: r["programs"][0]["terms"][1].update(fit="strong"))  # 未知の fit
    _mut(lambda r: r["programs"][0]["terms"][1].pop("fit"))
    _mut(lambda r: r["programs"][0]["terms"][1].update(reason=""))  # 根拠の無い分類
    _mut(lambda r: r["programs"][0]["terms"][1].update(surprise=1))
    _mut(lambda r: r["programs"][0]["terms"][0].update(fit="core"))  # brand term に fit
    _mut(lambda r: r["programs"][0]["terms"][0].update(fit_when_weak="strong"))
    _mut(
        lambda r: r["programs"][0]["terms"].append(dict(r["programs"][0]["terms"][1]))
    )  # 重複 term
    _mut(lambda r: r["programs"].append(deepcopy(r["programs"][0])))  # 重複 program
    with pytest.raises(FitConfigError):
        parse_fit_config([])


# ============================================================ brand tier and fit are independent
CATALOG = [
    _facts(1, "HubSpot", ["HubSpot", "CRM", "業務効率化", "顧客管理"], "Impact"),
    _facts(2, "Todoist", ["Todoist", "タスク管理", "業務効率化"]),
    _facts(3, "Make", ["Make", "業務自動化", "自動化"], "make"),
    _facts(4, "Fireflies.ai", ["Fireflies", "Fireflies.ai", "AI 議事録"]),
    _facts(5, "Acme Notes", ["Acme", "ノート術"]),  # config に無い
]


def _one(keyword: str, name: str):
    return next(m for m in match_catalog(keyword, CATALOG) if m.name == name)


def test_weak_core_is_scoring_and_primary_eligible_without_a_brand() -> None:
    m = _one("crm おすすめ", "HubSpot")
    assert (m.brand_tier, m.fit) == (TIER_WEAK, FIT_CORE)
    assert (m.scoring_eligible, m.primary_eligible) == (True, True)
    assert m.terms_with_fit(FIT_CORE) == ("CRM",)
    assert m.fit_reason == "'CRM' core"


def test_weak_loose_is_visible_but_not_eligible() -> None:
    m = _one("業務効率化 ツール", "HubSpot")
    assert (m.brand_tier, m.fit) == (TIER_WEAK, FIT_LOOSE)
    assert (m.scoring_eligible, m.primary_eligible) == (False, False)
    assert m.weak_terms == ("業務効率化",) and m.reason.startswith("weak: generic term(s) only")
    assert scoring_programs(match_catalog("業務効率化 ツール", CATALOG)) == []


def test_weak_unreviewed_is_not_eligible() -> None:
    m = _one("ノート術 まとめ", "Acme Notes")
    assert (m.brand_tier, m.fit) == (TIER_WEAK, FIT_UNREVIEWED)
    assert (m.scoring_eligible, m.primary_eligible) == (False, False)


def test_strong_is_eligible_regardless_of_fit() -> None:
    m = _one("Acme 料金", "Acme Notes")
    assert m.brand_tier == TIER_STRONG and m.fit is None  # brand だけで match: fit は使わない
    assert (m.scoring_eligible, m.primary_eligible) == (True, True)
    # strong が loose の generic term を同時に踏んでも strong のまま (fit は参考情報)
    both = _one("HubSpot 業務効率化", "HubSpot")
    assert both.brand_tier == TIER_STRONG and both.fit == FIT_LOOSE
    assert both.scoring_eligible is True and both.primary_eligible is True


def test_one_core_term_makes_a_program_core_even_with_loose_terms() -> None:
    m = _one("crm 業務効率化 とは", "HubSpot")
    assert m.fit == FIT_CORE and m.scoring_eligible is True
    assert m.terms_with_fit(FIT_CORE) == ("CRM",) and m.terms_with_fit(FIT_LOOSE) == ("業務効率化",)
    assert [(t.term, t.fit) for t in m.term_tiers if t.tier == TIER_WEAK] == [
        ("CRM", FIT_CORE),
        ("業務効率化", FIT_LOOSE),
    ]


def test_same_term_different_programs_can_differ_in_fit() -> None:
    matches = match_catalog("タスク管理 業務効率化", CATALOG)
    fits = {m.name: m.fit for m in matches}
    assert fits == {"HubSpot": FIT_LOOSE, "Todoist": FIT_CORE}
    assert [m.name for m in matches if m.scoring_eligible] == ["Todoist"]


def test_ambiguous_brand_used_weakly_is_loose_and_bare_fireflies_is_unreviewed() -> None:
    for text in ("make sure", "make", "make money"):
        m = _one(text, "Make")
        assert (m.brand_tier, m.fit, m.scoring_eligible) == (TIER_WEAK, FIT_LOOSE, False), text
        assert m.ambiguity is not None
    assert _one("Make 料金", "Make").brand_tier == TIER_STRONG
    bare = _one("fireflies", "Fireflies.ai")
    assert (bare.brand_tier, bare.fit) == (TIER_WEAK, FIT_UNREVIEWED)
    assert (bare.scoring_eligible, bare.primary_eligible) == (False, False)
    assert _one("fireflies.ai 料金", "Fireflies.ai").brand_tier == TIER_STRONG


def test_japanese_spacing_variants_get_the_same_fit() -> None:
    assert _one("顧客 管理 ツール", "HubSpot").fit == FIT_CORE
    assert _one("業務 効率 化 ツール", "HubSpot").fit == FIT_LOOSE


def test_a_custom_fit_config_changes_eligibility_but_not_the_brand_tier() -> None:
    custom = parse_fit_config(
        {
            "version": 1,
            "programs": [
                {
                    "program": "HubSpot",
                    "terms": [{"term": "業務効率化", "fit": "core", "reason": "x"}],
                }
            ],
        }
    )
    m = next(
        m for m in match_catalog("業務効率化", CATALOG, fit_config=custom) if m.name == "HubSpot"
    )
    assert (m.brand_tier, m.fit, m.scoring_eligible) == (TIER_WEAK, FIT_CORE, True)
    default = _one("業務効率化", "HubSpot")
    assert (default.brand_tier, default.fit, default.scoring_eligible) == (
        TIER_WEAK,
        FIT_LOOSE,
        False,
    )


# ============================================================ coverage gaps (fail-closed reporting)
def test_fit_config_gaps_reports_unrepresented_programs_and_terms_without_raising() -> None:
    from app.keyword.affiliate_tiers import fit_config_gaps

    programs = [
        _facts(1, "HubSpot", ["HubSpot", "CRM", "顧客管理"]),  # 網羅済み
        _facts(2, "HubSpot Plus", ["HubSpot", "CRM"]),  # 名前が違う = config に無い program
        _facts(3, "Pipedrive", ["Pipedrive", "CRM", "未分類の term"]),
    ]
    gaps = {g.program: g for g in fit_config_gaps(programs)}
    assert set(gaps) == {"HubSpot Plus", "Pipedrive"}
    assert gaps["HubSpot Plus"].program_missing is True
    assert gaps["HubSpot Plus"].unclassified_terms == ("CRM",)  # own-name term は対象外
    assert gaps["Pipedrive"].program_missing is False
    assert gaps["Pipedrive"].unclassified_terms == ("未分類の term",)
    # 未掲載 program の CRM は core にならない (fit は program 名で引くので HubSpot とは別扱い)
    plus = next(m for m in match_catalog("crm 比較", programs) if m.name == "HubSpot Plus")
    assert (plus.fit, plus.scoring_eligible) == (FIT_UNREVIEWED, False)
    assert plus.primary_eligible is False
    real = next(m for m in match_catalog("crm 比較", programs) if m.name == "HubSpot")
    assert real.fit == FIT_CORE and real.scoring_eligible is True


def test_the_committed_config_has_no_gaps_for_its_own_programs() -> None:
    from app.keyword.affiliate_tiers import fit_config_gaps

    programs = [
        _facts(i, p["program"], [t["term"] for t in p["terms"]])
        for i, p in enumerate(_raw()["programs"], start=1)
    ]
    assert fit_config_gaps(programs) == []
