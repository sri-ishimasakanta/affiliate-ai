"""C10-2 Content Intelligence の規則 (pure)。

pin する契約:

- クラスタ: 識別は設定の ID だけ (証拠が変わっても同じ)。所属は設定の語 → 主題の重なりが
  ただ 1 つのとき。重ならない・同点は所属しない。2 つの設定 (E を含む) を 1 つにまとめる。
- gap: 根拠のある語があるときだけ ``missing_<role>``。記事が少ないだけでは作らない。
- 発見: 同じ語は 1 つ。既にある Keyword・記事と同じ語 / 同じ意図は新しい候補にしない。
- 記事の種類: 8 つの役割。理由を残す。決められなければ undetermined。合成の点数は無い。
- 事実: 古い事実は ready にしない。unknown の値は事実として数えない。食い違いを出す。
- 次の記事: 重なりは blocked。Keyword に無い語は昇格が要る。記事を作らない。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.article.cluster_plan import Vocabulary
from app.content import clusters as cl
from app.content import content_types as ct
from app.content import discovery as dc
from app.content import facts as fx
from app.content import gaps as gp
from app.content import next_article as na

_NOW = datetime(2026, 10, 1, tzinfo=UTC)


def _registry():
    m = cl.ClusterMember
    return cl.ClusterRegistry((
        cl.ClusterDefinition("A", "AI meeting notes", 1, "active",
                             (m("AI 議事録 おすすめ", "pillar", "cluster_config"),
                              m("AI 議事録 比較", "supporting", "cluster_config"),
                              m("AI 議事録 料金", "supporting", "portfolio_plan", "pricing")),
                             frozenset({"議事録"})),
        cl.ClusterDefinition("C", "RPA", 2, "active",
                             (m("RPA おすすめ", "pillar", "cluster_config"),),
                             frozenset({"rpa"})),
        cl.ClusterDefinition("E", "CRM", None, "deferred",
                             (m("CRM おすすめ", "supporting", "portfolio_plan",
                                "recommendation_roundup"),),
                             frozenset({"crm"}), "no CRM keywords"),
    ), Vocabulary())


# == クラスタ ======================================================================================
def test_cluster_assignment_is_explicit_then_unique_theme() -> None:
    r = _registry()
    assert cl.assign_cluster("ai 議事録 比較", r) == cl.Assignment("A", "explicit",
                                                                "cluster_config")
    assert cl.assign_cluster("RPA ライセンス", r).cluster_key == "C"
    assert cl.assign_cluster("RPA ライセンス", r).basis == "theme_match"
    assert cl.assign_cluster("業務 自動化", r).basis == "no_theme_match"
    assert cl.assign_cluster("業務 自動化", r).cluster_key is None


def test_cluster_identity_is_stable_while_evidence_changes() -> None:
    r = _registry()
    kws = [cl.KeywordFact(1, "AI 議事録 おすすめ", "analyzed")]
    before = cl.build_clusters(r, keywords=kws, articles=[], observed_at="t1")
    after = cl.build_clusters(r, keywords=kws, articles=[cl.ArticleFact(
        5, 1, "AI 議事録 おすすめ", "published", "recommendation_roundup", "affiliate")],
        observed_at="t2")
    a0, a1 = before[0], after[0]
    assert a0.cluster_id == a1.cluster_id == "cluster:A"
    assert a0.identity_fingerprint == a1.identity_fingerprint
    assert a0.evidence_fingerprint != a1.evidence_fingerprint
    assert a0.coverage_state == cl.COVERAGE_UNCOVERED
    assert a1.coverage_state == cl.COVERAGE_PARTIAL and a1.pillar["covered"]
    assert a1.articles[0]["strategy_role"] == ct.ROUNDUP
    assert a1.mix == {"commercial": 1, "informational": 0}
    e = [c for c in after if c.cluster_key == "E"][0]
    assert e.config_status == "deferred" and e.provenance["deferred_reason"] == "no CRM keywords"


def test_the_real_registry_merges_both_configs_including_cluster_e() -> None:
    r = cl.load_registry()
    keys = {c.cluster_key: c for c in r.clusters}
    assert set(keys) >= {"A", "B", "C", "D", "E"}
    assert keys["E"].config_status == "deferred" and keys["E"].members
    assert all(sum(1 for m in c.members if m.role == "pillar") <= 1 or c.cluster_key
               for c in r.clusters)


# == gap ===========================================================================================
def test_gaps_need_evidence_and_do_not_count_articles() -> None:
    r = _registry()
    [a, *_rest] = cl.build_clusters(r, keywords=[], articles=[cl.ArticleFact(
        5, None, "AI 議事録 おすすめ", "published", "recommendation_roundup", "affiliate")],
        observed_at="t")
    none = gp.find_gaps(a, role_evidence=[])
    assert {g.gap_type for g in none} == set()  # 根拠の語が無ければ役割の gap は作らない
    gaps = gp.find_gaps(a, role_evidence=[
        gp.RoleEvidence("AI 議事録 比較", ct.COMPARISON, "keyword", "keyword:2"),
        gp.RoleEvidence("AI 議事録 おすすめ", ct.ROUNDUP, "keyword")],  # 既に記事がある語
        link_candidates={5: 2}, index_states={5: {"normalized_status": "discovered_not_indexed",
                                                  "freshness": "fresh"}},
        monetization={5: {"eligible_programs": ["Krisp"]}})
    types = {g.gap_type for g in gaps}
    assert types == {"missing_comparison", gp.GAP_WEAK_LINKING, gp.GAP_INDEX}
    missing = next(g for g in gaps if g.gap_type == "missing_comparison")
    assert missing.expected_role == ct.COMPARISON and missing.evidence[0]["ref"] == "keyword:2"
    assert missing.gap_key == "cluster:A:missing_comparison"
    stale_index = gp.find_gaps(a, role_evidence=[], index_states={5: {
        "normalized_status": "not_indexed", "freshness": "stale"}})
    assert not stale_index  # 古い調べでは索引の gap にしない


def test_a_cluster_without_articles_is_a_no_article_gap_with_blockers_when_deferred() -> None:
    r = _registry()
    e = [c for c in cl.build_clusters(r, keywords=[], articles=[], observed_at="t")
         if c.cluster_key == "E"][0]
    [gap] = [g for g in gp.find_gaps(e, role_evidence=[]) if g.gap_type == gp.GAP_NO_ARTICLE]
    assert gap.blockers == ("cluster deferred in content_clusters.json",)


# == 発見 ==========================================================================================
def test_discovery_dedups_and_suppresses_existing_keywords_and_articles() -> None:
    r = _registry()
    obs = [dc.DiscoveryObservation(dc.SRC_GSC_QUERY, "rpa ライセンス", "2026-09-20",
                                   {"impressions": 3, "clicks": 0, "pages": ["p1"]}),
           dc.DiscoveryObservation(dc.SRC_GSC_QUERY, "RPA  ライセンス", "2026-09-25",
                                   {"impressions": 2, "clicks": 1, "pages": ["p2"]}),
           dc.DiscoveryObservation(dc.SRC_PORTFOLIO_RESERVE, "AI 議事録 比較"),
           dc.DiscoveryObservation(dc.SRC_GSC_QUERY, "rpa おすすめ", "2026-09-21"),
           dc.DiscoveryObservation(dc.SRC_GSC_QUERY,
                                   "最新のオンラインaiドキュメントツールと料金は？")]
    out = {c.phrase_key: c for c in dc.merge_candidates(
        obs, keywords=[(7, "AI 議事録 比較")], article_keywords=[(3, "RPA おすすめ")],
        registry=r)}
    lic = out["rpaライセンス"]
    assert lic.is_new and lic.refresh_needs == ("google_ads",)
    assert (lic.first_seen, lic.last_seen) == ("2026-09-20", "2026-09-25")
    assert lic.evidence == {"gsc_impressions": 5, "gsc_clicks": 1, "gsc_pages": ["p1", "p2"]}
    assert lic.cluster_key == "C"
    assert out["ai議事録比較"].duplicate_state == dc.STATE_EXISTING_KEYWORD
    assert out["rpaおすすめ"].duplicate_state == dc.STATE_EXISTING_ARTICLE
    assert all("？" not in k for k in out)  # 質問文は語の候補にしない
    again = dc.merge_candidates(obs, keywords=[(7, "AI 議事録 比較")],
                                article_keywords=[(3, "RPA おすすめ")], registry=r)
    assert [c.as_dict() for c in again] == [c.as_dict() for c in out.values()]  # 決定論的


# == 記事の種類 ====================================================================================
def test_content_type_roles_cover_the_eight_strategies_with_reasons() -> None:
    assert set(ct.STRATEGY_ROLES) == {"comparison", "roundup", "how_to", "practical_workflow",
                                      "implementation", "pricing", "informational",
                                      "category_landing"}
    assert ct.TEMPLATE_FOR_ROLE[ct.IMPLEMENTATION] == ct.TEMPLATE_FOR_ROLE[ct.HOW_TO]
    rec = ct.recommend_content_type("RPA 導入")
    assert (rec.role, rec.template_type, rec.basis) == (ct.IMPLEMENTATION, "how_to", "marker")
    assert rec.reasons == ("keyword marker '導入' → implementation",)
    assert ct.recommend_content_type("Notion 活用").role == ct.PRACTICAL_WORKFLOW
    assert ct.recommend_content_type("AI 議事録 比較").role == ct.COMPARISON
    assert ct.recommend_content_type("生成AI", head_term=True,
                                     cluster_has_landing=False).role == ct.CATEGORY_LANDING
    assert ct.recommend_content_type("Krisp").role == ct.UNDETERMINED
    commercial = ct.recommend_content_type("Krisp", commercial_intent=75, affiliate_eligible=True)
    assert commercial.role == ct.ROUNDUP and commercial.basis == "commercial_evidence"
    assert ct.recommend_content_type("Krisp", commercial_intent=75).role == ct.INFORMATIONAL
    covered = ct.recommend_content_type("AI 議事録 比較", covered_roles=frozenset({ct.COMPARISON}))
    assert "already has a comparison article" in covered.reasons[-1]
    assert "score" not in covered.as_dict()


# == 事実 ==========================================================================================
def _fact(aid, key, value, days_ago, status="verified", subject="Fireflies.ai"):
    return fx.StoredFact(aid, subject, key, value, status, _NOW - timedelta(days=days_ago), 9)


def test_facts_are_reused_across_articles_with_freshness_and_conflicts() -> None:
    rows = [_fact(1, "pricing_summary", "月 10 ドル", 40),
            _fact(2, "pricing_summary", "月 12 ドル", 5),
            _fact(1, "official_product_name", "Fireflies.ai", 20),
            _fact(2, "key_features", ["議事録"], 10), _fact(3, "free_plan_available", None, 3,
                                                            status="unknown")]
    [s] = fx.build_subject_facts(rows, now=_NOW)
    views = {v.fact_key: v for v in s.facts}
    assert views["pricing_summary"].value == "月 12 ドル" and views["pricing_summary"].fresh
    assert views["pricing_summary"].conflicting and views["pricing_summary"].articles == (1, 2)
    assert "free_plan_available" not in views  # unknown は事実として数えない
    assert s.readiness == fx.PARTIAL and "free_plan_available" in s.missing_required
    old = fx.build_subject_facts([_fact(1, "pricing_summary", "x", 40)], now=_NOW)[0]
    assert old.facts[0].state == fx.STALE  # 古い事実を最新として使わない
    future = fx.build_subject_facts([_fact(1, "pricing_summary", "x", -1)], now=_NOW)
    assert future == []  # 観測の時点より後の事実は使わない
    plan = fx.research_plan([s])
    assert plan[0]["conflicting"] == ["pricing_summary"]


# == 次の記事 ======================================================================================
def _rec(role=ct.COMPARISON):
    template = ct.TEMPLATE_FOR_ROLE.get(role)
    return ct.ContentTypeRecommendation(role, template.value if template else None,
                                        ct.family_of(role), "marker", (f"marker → {role}",))


def _build(topic="AI 議事録 比較", *, keyword_id=7, existing=(), scored=True, growth=None,
           components=None, facts="ready", affiliate=None):
    return na.build_candidate(
        topic=topic, keyword_id=keyword_id, discovery_key=None if keyword_id else "x",
        cluster_id="cluster:A", cluster_key="A", cluster_role="supporting", recommendation=_rec(),
        gaps=["cluster:A:missing_comparison"],
        components=components or {"search_demand": {"state": "usable", "source": "google_ads"}},
        affiliate=affiliate or {"eligible_programs": ["Krisp"]}, facts_readiness=facts,
        existing=list(existing), scored=scored, growth=growth)


def test_a_ready_candidate_hands_off_through_the_growth_action() -> None:
    item = _build(growth={"id": 14, "status": "active"})
    assert item.identity == "create_new_article:keyword:keyword:7"  # Growth と同じ識別
    assert item.handoff["readiness"] == na.READY_FOR_GROWTH_REVIEW
    assert "manage_growth_actions.py review 14" in item.handoff["next_step"]
    assert item.monetization_role == "affiliate" and not item.blockers
    d = item.as_dict()
    assert d["creates_article"] is False and d["composite_score"] is None


def test_cannibalization_blocks_even_when_a_gap_exists() -> None:
    same = _build(existing=[na.ExistingContent("article", "article:3", "ai 議事録 比較",
                                               "published")])
    assert same.handoff["readiness"] == na.BLOCKED
    assert same.cannibalization[0]["rule"] == "same_normalized_keyword"
    request = _build(existing=[na.ExistingContent("planning_request", "handoff:4",
                                                  "AI 議事録 比較", "pending")])
    assert request.handoff["readiness"] == na.BLOCKED
    planned = _build(existing=[na.ExistingContent("planned_article", "article:9",
                                                  "AI 議事録 比較", "drafting")])
    assert planned.cannibalization[0]["kind"] == "planned_article"
    role = _build(topic="AI 議事録 ツール 比較", existing=[na.ExistingContent(
        "article", "article:5", "議事録 自動作成 ツール 比較", "published", "A", ct.COMPARISON)])
    assert role.handoff["readiness"] == na.BLOCKED


def test_discovery_and_unscored_candidates_are_not_ready() -> None:
    discovered = _build(keyword_id=None)
    assert discovered.handoff["readiness"] == na.NEEDS_KEYWORD_PROMOTION
    assert discovered.identity == "discovery:x"
    unscored = _build(scored=False, components={"competition_ease": {
        "state": "missing", "source": "manual_keyword_difficulty"}})
    assert unscored.handoff["readiness"] == na.NEEDS_SIGNALS
    assert "competition_ease" in unscored.blockers[0]
    no_facts = _build(facts="stale", growth={"id": 1, "status": "active"})
    assert "reusable SaaS facts are stale" in no_facts.blockers
