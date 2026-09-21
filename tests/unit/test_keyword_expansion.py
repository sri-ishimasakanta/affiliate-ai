"""app/article/keyword_expansion.py — keep / merge / reject の expansion planner (pure)。

C2.3 で人手判定した 135 候補の判定を、規則 (alias / qualifier / reject rule / curated merge) から
再現できることを検証する。DB / HTTP / LLM には触れない。
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path

import pytest

from app.article.cluster_plan import (
    AffiliateMatch,
    ArticleInput,
    KeywordInput,
    build_content_queue,
    load_cluster_config,
)
from app.article.keyword_expansion import (
    NOT_AVAILABLE,
    ExpansionRulesError,
    IdeaCandidate,
    load_expansion_rules,
    parse_expansion_rules,
    plan_expansion,
)
from tests.support.c22_pool import POOL_30
from tests.support.c23_expansion_cases import CASES

_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = load_cluster_config(_ROOT / "app" / "config" / "content_clusters.json")
_RULES = load_expansion_rules(_ROOT / "app" / "config" / "keyword_expansion_rules.json")
_ARTICLE_1 = ArticleInput(
    id=1, keyword_id=21, keyword="業務効率化 ツール おすすめ", title="t", status="published"
)


def _pool() -> list[KeywordInput]:
    return [
        KeywordInput(
            id=kid,
            keyword=text,
            status="analyzed",
            opportunity_score=score,
            missing_components=tuple(missing.split("|")) if missing else (),
        )
        for kid, text, score, missing in POOL_30
    ]


def _plan(candidates, *, rules=_RULES, keywords=None, articles=None):
    keywords = keywords if keywords is not None else _pool()
    articles = articles if articles is not None else [_ARTICLE_1]
    queue = build_content_queue(_CONFIG, keywords, articles)
    return plan_expansion(_CONFIG, rules, candidates, keywords, articles, queue)


def _cands(*texts: str, cluster: str | None = None) -> list[IdeaCandidate]:
    return [IdeaCandidate(keyword=t, cluster=cluster) for t in texts]


def _by(plan) -> dict:
    return {d.keyword: d for d in plan.decisions}


# ================================================================== rules
def test_tracked_rules_load_and_are_small_and_explicit() -> None:
    assert len(_RULES.reject_rules) == 22
    assert len(_RULES.curated_merges) == 13  # 語彙規則で表せない対応だけ
    assert len({r.id for r in _RULES.reject_rules}) == len(_RULES.reject_rules)
    assert all(r.reason_code and r.note for r in _RULES.reject_rules)


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ([], "must be a JSON object"),
        ({"version": 2}, "version must be 1"),
        ({"version": 1, "bogus": 1}, "unknown top-level"),
        ({"version": 1, "reject_rules": "x"}, "must be a list"),
        ({"version": 1, "reject_rules": [{"id": "a", "reason_code": "c"}]}, "needs products"),
        (
            {
                "version": 1,
                "reject_rules": [
                    {"id": "a", "reason_code": "c", "phrase": "x"},
                    {"id": "a", "reason_code": "c", "phrase": "y"},
                ],
            },
            "duplicate reject rule id",
        ),
        (
            {
                "version": 1,
                "reject_rules": [
                    {"id": "a", "reason_code": "c", "phrase": "x", "intents": ["nope"]}
                ],
            },
            "unknown intents",
        ),
        (
            {
                "version": 1,
                "reject_rules": [{"id": "a", "reason_code": "c", "phrase": "x", "extra": 1}],
            },
            "unknown keys",
        ),
        (
            {"version": 1, "reject_rules": [{"id": "a", "phrase": "x"}]},
            "reason_code",
        ),
        (
            {"version": 1, "reject_rules": [{"id": "a", "reason_code": "c", "products": [""]}]},
            "list fields",
        ),
        (
            {"version": 1, "curated_merges": [{"candidate": "a", "target": "A"}]},
            "must differ",
        ),
        (
            {
                "version": 1,
                "curated_merges": [
                    {"candidate": "a", "target": "b"},
                    {"candidate": "ａ", "target": "c"},
                ],
            },
            "duplicate curated candidate",
        ),
        ({"version": 1, "curated_merges": [{"candidate": "a"}]}, "non-empty strings"),
    ],
)
def test_rules_validation(raw, fragment: str) -> None:
    with pytest.raises(ExpansionRulesError) as exc:
        parse_expansion_rules(raw)
    assert fragment in str(exc.value)


def test_rules_load_errors(tmp_path: Path) -> None:
    with pytest.raises(ExpansionRulesError, match="not found"):
        load_expansion_rules(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    with pytest.raises(ExpansionRulesError, match="not readable JSON"):
        load_expansion_rules(bad)


# ====================================================== C2.3 decisions reproduced
def _c23_plan():
    return _plan([IdeaCandidate(keyword=k, cluster=c) for c, k, _, _ in CASES])


def test_reproduces_every_c23_keep_merge_reject_decision_and_merge_target() -> None:
    plan = _c23_plan()
    got = _by(plan)
    mismatches = []
    for _cluster, keyword, decision, target in CASES:
        d = got[keyword]
        if d.decision != decision or (decision == "merge" and d.target != target):
            mismatches.append((keyword, decision, target, d.decision, d.reason_code, d.target))
    assert mismatches == []
    assert (plan.summary["keep"], plan.summary["merge"], plan.summary["reject"]) == (67, 38, 30)
    assert plan.summary["candidates"] == 135


def test_c23_decisions_carry_reason_codes_and_rule_provenance() -> None:
    got = _by(_c23_plan())
    assert all(d.reason_code and d.reason for d in got.values())
    rejects = [d for d in got.values() if d.decision == "reject"]
    assert all(d.matched_rule for d in rejects)  # reject は必ずどの規則かを示す
    assert got["ノーコード ツール 比較"].reason_code == "off_theme"
    assert got["Make 代替"].reason_code == "affiliate_conflict"
    assert got["Todoist 使い方"].reason_code == "product_not_b2b"
    assert got["業務効率化 アイデア"].reason_code == "b2c_general_noise"
    assert got["Notta 料金"].reason_code == "competitor_navigational"
    assert got["Salesforce 料金"].reason_code == "no_catalog_large_vendor"

    assert got["業務効率化 ツール 中小企業"].reason_code == "overlaps_existing_article"
    assert got["業務効率化 ツール 中小企業"].target_kind == "article"
    assert got["業務効率化 ツール 中小企業"].target_article_id == 1
    assert got["RPA 費用"].reason_code == "overlaps_existing_keyword"
    assert got["RPA 費用"].target == "RPA おすすめ" and got["RPA 費用"].target_kind == "keyword"
    assert got["タスク管理 ツール 比較"].reason_code == "intent_overlap_with_candidate"
    assert got["タスク管理 ツール 比較"].target_kind == "candidate"
    assert got["ChatGPT Team 料金"].reason_code == "curated_merge"
    assert got["ChatGPT Team 料金"].target == "ChatGPT 法人 プラン"
    assert got["社内 ナレッジ 共有 ツール"].reason_code == "curated_merge"
    for keep in (d for d in got.values() if d.decision == "keep"):
        assert keep.reason_code == "distinct_intent" and keep.overlap_risk in {
            "low",
            "medium",
            "high",
        }


def test_c23_automatic_rules_explain_most_merges_and_curated_ones_are_few() -> None:
    merges = [d for d in _c23_plan().decisions if d.decision == "merge"]
    curated = [d for d in merges if d.reason_code == "curated_merge"]
    assert len(curated) == 13 and len(merges) - len(curated) == 25


def test_c23_reproduction_is_deterministic_for_any_input_order() -> None:
    baseline = _c23_plan().to_dict()
    rng = random.Random(20260922)
    for _ in range(5):
        cases = list(CASES)
        rng.shuffle(cases)
        shuffled = _plan([IdeaCandidate(keyword=k, cluster=c) for c, k, _, _ in cases]).to_dict()
        assert shuffled == baseline
    assert _c23_plan().to_dict() == baseline


def test_output_is_json_serialisable_and_sorted_keep_merge_reject() -> None:
    plan = _c23_plan()
    decoded = json.loads(json.dumps(plan.to_dict(), ensure_ascii=False, sort_keys=True))
    assert decoded["summary"]["keep"] == 67
    order = [d.decision for d in plan.decisions]
    assert order == sorted(order, key={"keep": 0, "merge": 1, "reject": 2}.get)


# ============================================================= regressions / behaviour
def test_clickup_pricing_vs_alternatives_are_separate_keeps_and_free_folds_into_pricing() -> None:
    plan = _plan(_cands("ClickUp 料金", "ClickUp 代替", "ClickUp 使い方", "ClickUp 無料"))
    by = _by(plan)
    assert [by[k].decision for k in ("ClickUp 料金", "ClickUp 代替", "ClickUp 使い方")] == [
        "keep",
        "keep",
        "keep",
    ]
    assert by["ClickUp 無料"].decision == "merge" and by["ClickUp 無料"].target == "ClickUp 料金"


def test_alias_and_qualifier_behaviour_at_planner_level() -> None:
    by = _by(
        _plan(
            _cands(
                "CRM おすすめ",
                "顧客管理 ツール 比較",  # 顧客管理 == CRM
                "中小企業 CRM",  # 任意 qualifier
                "SFA 比較",  # sfa == crm
                "SFA CRM 違い",  # 違い は別 SERP
                "議事録 ツール 無料",  # AI 議事録 と同一 theme -> 既存 slot へ
            )
        )
    )
    assert by["CRM おすすめ"].decision == "keep"
    for merged in ("顧客管理 ツール 比較", "中小企業 CRM", "SFA 比較"):
        assert by[merged].decision == "merge" and by[merged].target == "CRM おすすめ"
    assert by["SFA CRM 違い"].decision == "keep" and by["SFA CRM 違い"].serp_family == "difference"
    assert by["議事録 ツール 無料"].target == "AI 議事録 無料"
    assert by["議事録 ツール 無料"].target_kind == "keyword"


def test_canonical_spelling_is_the_anchor_even_if_alphabetically_later() -> None:
    by = _by(_plan(_cands("MA ツール 比較", "マーケティングオートメーション 比較")))
    assert by["マーケティングオートメーション 比較"].decision == "keep"
    assert by["MA ツール 比較"].target == "マーケティングオートメーション 比較"


@pytest.mark.parametrize("status", ["idea", "planned", "drafting", "review", "approved", "rewrite"])
def test_in_flight_articles_absorb_overlapping_candidates(status: str) -> None:
    article = ArticleInput(id=9, keyword_id=None, keyword="Zapier 代替", title="t", status=status)
    by = _by(_plan(_cands("Zapier 代替 おすすめ", "Zapier 使い方"), articles=[article]))
    # "Zapier 代替 おすすめ" は代替 intent -> 同 SERP の article へ
    assert by["Zapier 代替 おすすめ"].decision == "merge"
    assert by["Zapier 代替 おすすめ"].target_kind == "article"
    assert by["Zapier 代替 おすすめ"].target_article_id == 9
    assert by["Zapier 使い方"].decision == "reject"  # 規則が先に効く


def test_overlap_with_an_unassigned_existing_keyword_targets_that_keyword() -> None:
    d = _by(_plan(_cands("生成AI 意味")))["生成AI 意味"]
    assert d.decision == "merge" and d.reason_code == "overlaps_unassigned_keyword"
    assert d.target == "生成AI とは" and d.target_kind == "keyword"


def test_exact_duplicates_are_rejected_against_existing_keywords_and_within_the_list() -> None:
    plan = _plan(_cands("ｃｈａｔｇｐｔ　料金", "AI 議事録", "crm 無料", "CRM 無料"))
    by = {(d.keyword, d.reason_code): d for d in plan.decisions}
    assert by[("ｃｈａｔｇｐｔ　料金", "duplicate_existing_keyword")].target == "ChatGPT 料金"
    assert by[("AI 議事録", "duplicate_existing_keyword")].decision == "reject"
    assert ("CRM 無料", "duplicate_candidate") in by or ("crm 無料", "duplicate_candidate") in by
    assert sum(1 for d in plan.decisions if d.reason_code == "duplicate_candidate") == 1


def test_metrics_default_to_not_available_and_pass_through_when_present() -> None:
    metrics = {"avg_monthly_searches": 880, "competition": "HIGH"}
    plan = _plan(
        [
            IdeaCandidate(keyword="CRM おすすめ", cluster="E", metrics=metrics),
            IdeaCandidate(keyword="HubSpot 料金", cluster="E"),
        ]
    )
    by = _by(plan)
    assert by["CRM おすすめ"].metrics == metrics
    assert by["HubSpot 料金"].metrics == NOT_AVAILABLE
    assert plan.summary["metrics_available"] == 1


def test_search_volume_breaks_anchor_ties_when_intent_and_spelling_are_equal() -> None:
    low = IdeaCandidate(keyword="タスク管理 無料", metrics={"avg_monthly_searches": 100})
    high = IdeaCandidate(keyword="タスク管理 ツール 無料", metrics={"avg_monthly_searches": 900})
    by = _by(_plan([low, high]))
    assert by["タスク管理 ツール 無料"].decision == "keep"
    assert by["タスク管理 無料"].target == "タスク管理 ツール 無料"
    # 指標が無ければ文字列順 (決定論的)
    by = _by(_plan(_cands("タスク管理 無料", "タスク管理 ツール 無料")))
    assert sum(1 for d in by.values() if d.decision == "keep") == 1


def test_affiliate_coverage_and_cluster_are_reported_per_decision() -> None:
    matches = (AffiliateMatch(3, "HubSpot", "impact"), AffiliateMatch(1, "Pipedrive", None))
    plan = _plan(
        [
            IdeaCandidate(keyword="CRM おすすめ", cluster="E", affiliate_matches=matches),
            IdeaCandidate(keyword="Zapier 代替", cluster="C"),
        ]
    )
    by = _by(plan)
    assert by["CRM おすすめ"].affiliate.level == "multiple"
    assert by["CRM おすすめ"].affiliate.program_names == ("HubSpot", "Pipedrive")
    assert by["Zapier 代替"].affiliate.level == "none"
    assert by["CRM おすすめ"].cluster == "E"
    assert plan.summary["keep_by_cluster"] == {"C": 1, "E": 1}


def test_overlap_risk_flags_near_duplicates_and_shared_products() -> None:
    by = _by(
        _plan(
            _cands(
                "営業 自動化 ツール",
                "営業 メール 自動化",
                "ClickUp 料金",
                "ClickUp 使い方",
                "生成AI 導入 事例",
                "生成AI 導入 費用",
            )
        )
    )
    assert by["ClickUp 料金"].overlap_risk == "medium"
    assert "same product" in by["ClickUp 料金"].overlap_note
    assert by["営業 自動化 ツール"].overlap_risk == "high"  # 同 family で theme が近い
    assert "near-similar" in by["営業 自動化 ツール"].overlap_note
    # 事例 と 費用 は別 SERP family なので、互いの重複リスクにはならない
    assert by["生成AI 導入 事例"].overlap_risk == "low"


# ================================================================= curated merges
def test_curated_merge_is_inert_when_its_target_does_not_exist() -> None:
    d = _by(_plan(_cands("RAG 社内 導入")))["RAG 社内 導入"]  # target 社内 ChatGPT 構築 が無い
    assert d.reason_code != "curated_merge"


def test_curated_merge_follows_the_target_to_its_final_destination() -> None:
    plan = _plan(_cands("ChatGPT Team 料金", "ChatGPT 法人 プラン"))
    by = _by(plan)
    assert by["ChatGPT 法人 プラン"].decision == "keep"
    assert by["ChatGPT Team 料金"].target == "ChatGPT 法人 プラン"

    # target が別の候補へ merge される場合は、その最終 keep へ辿る
    rules = parse_expansion_rules(
        {
            "version": 1,
            "curated_merges": [
                {"candidate": "cand-a", "target": "cand-b", "note": "a->b"},
                {"candidate": "cand-b", "target": "cand-c", "note": "b->c"},
            ],
        }
    )
    by = _by(_plan(_cands("cand-a", "cand-b", "cand-c"), rules=rules))
    assert by["cand-a"].target == "cand-c" and by["cand-b"].target == "cand-c"
    assert by["cand-c"].decision == "keep"


def test_curated_merge_into_a_rejected_target_becomes_a_reject() -> None:
    rules = parse_expansion_rules(
        {
            "version": 1,
            "reject_rules": [
                {"id": "noise", "reason_code": "b2c_general_noise", "terms_any": ["noise"]}
            ],
            "curated_merges": [{"candidate": "cand-a", "target": "cand noise", "note": ""}],
        }
    )
    by = _by(_plan(_cands("cand-a", "cand noise"), rules=rules))
    assert by["cand noise"].decision == "reject"
    assert by["cand-a"].decision == "reject" and by["cand-a"].reason_code == "merge_target_rejected"


def test_rules_are_evaluated_before_overlap_checks() -> None:
    # "Notion 使い方" は B2C 規則で reject。overlap 判定には進まない。
    d = _by(_plan(_cands("Notion 使い方")))["Notion 使い方"]
    assert d.decision == "reject" and d.matched_rule == "b2c-notion-tutorial"


# ================================================================= misc
def test_empty_candidates_and_blank_keywords() -> None:
    plan = _plan([])
    assert plan.decisions == () and plan.summary["candidates"] == 0
    assert _plan(_cands("   ")).decisions == ()


def test_article_without_keyword_warns_but_does_not_crash() -> None:
    orphan = ArticleInput(id=5, keyword_id=None, keyword=None, title="孤立", status="planned")
    plan = _plan(_cands("ClickUp 料金"), articles=[_ARTICLE_1, orphan])
    assert any("article #5" in w for w in plan.warnings)


def test_planner_modules_have_no_write_network_llm_or_google_imports() -> None:
    forbidden_imports = re.compile(
        r"^\s*(?:import|from)\s+(httpx|requests|urllib\.request|socket|openai|anthropic|aiohttp|"
        r"google|app\.keyword\.providers)\b",
        re.MULTILINE,
    )
    forbidden_calls = re.compile(r"session\.(commit|add|add_all|delete|merge|flush|execute)\(")
    for rel in (
        "app/article/keyword_expansion.py",
        "app/services/keyword_expansion_service.py",
        "scripts/plan_keyword_expansion.py",
    ):
        source = (_ROOT / rel).read_text(encoding="utf-8")
        assert not forbidden_imports.search(source), rel
        assert not forbidden_calls.search(source), rel
    # planner は keyword を追加しない (Keyword model / repository の create を使わない)
    for rel in ("app/article/keyword_expansion.py", "app/services/keyword_expansion_service.py"):
        source = (_ROOT / rel).read_text(encoding="utf-8")
        assert "KeywordRepository" not in source and "Keyword(" not in source, rel


# ==========================================================================
# 日本語 phrase の空白差 (Google Ads の分かち書き) を吸収する equivalence fallback
# ==========================================================================
def test_spaced_variants_of_existing_keywords_are_duplicates_with_text_preserved() -> None:
    plan = _plan(_cands("AI議事録おすすめ", "ai 議事 録 おすすめ", "Make料金"))
    by = _by(plan)
    for text, target in (
        ("AI議事録おすすめ", "AI 議事録 おすすめ"),
        ("ai 議事 録 おすすめ", "AI 議事録 おすすめ"),
        ("Make料金", "Make 料金"),
    ):
        d = by[text]  # Google が返した表記のまま (書き換えない)
        assert d.keyword == text
        assert d.decision == "reject" and d.reason_code == "duplicate_existing_keyword"
        assert d.target == target and d.target_kind == "keyword"
        assert "whitespace-insensitive" in d.reason


def test_exact_normalized_match_still_reports_identical_not_equivalent() -> None:
    d = _by(_plan(_cands("ai 議事録 おすすめ")))["ai 議事録 おすすめ"]
    assert d.reason_code == "duplicate_existing_keyword"
    assert d.reason.startswith("identical to existing keyword")


def test_spaced_and_unspaced_candidates_collapse_to_one_and_keep_original_text() -> None:
    plan = _plan(_cands("タスク 管理 ツール おすすめ", "タスク管理 ツール おすすめ"))
    by = _by(plan)
    keep = [d for d in plan.decisions if d.decision == "keep"]
    dupes = [d for d in plan.decisions if d.reason_code == "duplicate_candidate"]
    assert len(keep) == 1 and len(dupes) == 1
    assert set(by) == {
        "タスク 管理 ツール おすすめ",
        "タスク管理 ツール おすすめ",
    }  # 両方の原文を保持
    # 空白の少ない (プロジェクトの表記に近い) 方が代表
    assert keep[0].keyword == "タスク管理 ツール おすすめ"
    assert dupes[0].target == "タスク管理 ツール おすすめ" and dupes[0].target_kind == "candidate"
    assert "whitespace-insensitive" in dupes[0].reason


def test_search_volume_chooses_the_representative_of_equivalent_candidates() -> None:
    spaced = IdeaCandidate(
        keyword="タスク 管理 ツール おすすめ", metrics={"avg_monthly_searches": 900}
    )
    compact = IdeaCandidate(
        keyword="タスク管理 ツール おすすめ", metrics={"avg_monthly_searches": 50}
    )
    plan = _plan([compact, spaced])
    keep = [d for d in plan.decisions if d.decision == "keep"]
    assert [d.keyword for d in keep] == [
        "タスク 管理 ツール おすすめ"
    ]  # 原文のまま、高ボリューム側
    assert keep[0].metrics == {"avg_monthly_searches": 900}
    assert _plan([spaced, compact]).to_dict() == plan.to_dict()  # 入力順に依存しない


def test_english_only_phrases_are_not_collapsed_by_the_fallback() -> None:
    plan = _plan(
        _cands("google meet recorder", "googlemeet recorder", "crm  software", "crm software")
    )
    by = _by(plan)
    assert by["google meet recorder"].decision == "keep"
    assert by["googlemeet recorder"].decision == "keep"  # 空白の意味は英語では保たれる
    # 連続空白 (通常の正規化) の重複は従来どおり
    assert sum(1 for d in plan.decisions if d.reason_code == "duplicate_candidate") == 1


def test_article_keyword_spacing_variant_is_a_duplicate_of_the_existing_keyword() -> None:
    d = _by(_plan(_cands("業務効率化ツールおすすめ")))["業務効率化ツールおすすめ"]
    assert d.decision == "reject" and d.reason_code == "duplicate_existing_keyword"
    assert d.target == "業務効率化 ツール おすすめ"  # article #1 の keyword


def test_curated_merge_matches_spacing_variants_of_the_candidate_and_the_target() -> None:
    plan = _plan(_cands("ChatGPT Team料金", "ChatGPT法人プラン"))
    by = _by(plan)
    assert by["ChatGPT法人プラン"].decision == "keep"
    merged = by["ChatGPT Team料金"]
    assert merged.decision == "merge" and merged.reason_code == "curated_merge"
    assert merged.target == "ChatGPT法人プラン" and merged.target_kind == "candidate"


def test_equivalence_is_only_a_duplicate_fallback_not_a_token_overlap_replacement() -> None:
    # 空白なしの表記は intent の token 判定には使われない (dedupe 対象でなければ従来どおり keep)。
    d = _by(_plan(_cands("タスク管理ツール比較")))["タスク管理ツール比較"]
    assert d.decision == "keep" and d.reason_code == "distinct_intent"


def test_all_c23_decisions_are_unchanged_by_the_equivalence_fallback() -> None:
    plan = _c23_plan()
    got = _by(plan)
    for _cluster, keyword, decision, target in CASES:
        assert got[keyword].decision == decision, keyword
        if decision == "merge":
            assert got[keyword].target == target, keyword
        assert got[keyword].keyword == keyword  # 表示テキストは入力のまま
    assert (plan.summary["keep"], plan.summary["merge"], plan.summary["reject"]) == (67, 38, 30)
    assert not [d for d in plan.decisions if d.reason_code == "duplicate_candidate"]
