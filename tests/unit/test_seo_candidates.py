"""app.seo.candidates / app.seo.policy の単体テスト (C6)。

pin する契約:

- ゲートを 1 つでも通らなければ候補は **出ない** (沈黙が既定)。
- 優先度は明示規則からのみ決まり、隠れた重み付けスコアは存在しない。
- 証拠の出所 (実測 / 構造 / メタデータ / インデックス観測) を混同しない。
- 閾値はコードではなくバージョン管理された JSON に存在する。
"""

from __future__ import annotations

from datetime import date

from app.seo.candidates import (
    CANDIDATE_TYPES,
    CANNIBALIZATION_REVIEW,
    CONTENT_FRESHNESS_REVIEW,
    CTR_IMPROVEMENT,
    ENGAGEMENT_REVIEW,
    EVIDENCE_INDEX_OBSERVATION,
    EVIDENCE_PERFORMANCE,
    EVIDENCE_SOURCE_METADATA,
    EVIDENCE_STRUCTURAL,
    INDEXING_FOLLOWUP,
    INTERNAL_LINK_OPPORTUNITY,
    QUERY_EXPANSION,
    RANKING_IMPROVEMENT,
    SCOPE_EXPAND_EXISTING,
    SCOPE_POSSIBLE_NEW_ARTICLE,
    evaluate_cannibalization,
    evaluate_ctr,
    evaluate_engagement,
    evaluate_freshness,
    evaluate_indexing_followup,
    evaluate_internal_link,
    evaluate_query_expansion,
    evaluate_ranking,
    resolve_priority,
)
from app.seo.maturity import (
    ENGAGEMENT_AWAITING_DATA,
    ENGAGEMENT_SUFFICIENT_SAMPLE,
    SEARCH_NEWLY_PUBLISHED,
    SEARCH_SUFFICIENT_SAMPLE,
    ArticleMaturity,
)
from app.seo.policy import PRIORITIES, load_policy

_POLICY = load_policy()
_TODAY = date(2026, 10, 31)


def _maturity(**over) -> ArticleMaturity:
    kwargs = dict(
        article_id=1,
        age_days=60,
        search_state=SEARCH_SUFFICIENT_SAMPLE,
        engagement_state=ENGAGEMENT_SUFFICIENT_SAMPLE,
        search_reason="",
        engagement_reason="",
        search_data_covers_article=True,
        impressions=1000,
        organic_sessions=500,
    )
    kwargs.update(over)
    return ArticleMaturity(**kwargs)


# ==================== policy ==================================================
def test_policy_declares_a_version_and_every_candidate_priority() -> None:
    assert _POLICY.policy_version
    for candidate_type in CANDIDATE_TYPES:
        assert _POLICY.base_priority(candidate_type) in PRIORITIES


def test_thresholds_live_in_config_not_code() -> None:
    assert _POLICY.gate("ctr", "minimum_impressions") is not None
    assert _POLICY.gate("ranking", "minimum_impressions") is not None
    assert _POLICY.gate("engagement", "minimum_organic_sessions") is not None


def test_freshness_policy_is_per_article_type() -> None:
    assert _POLICY.freshness_days_for("pricing") < _POLICY.freshness_days_for("informational")


def test_priority_escalation_is_explicit() -> None:
    assert resolve_priority(_POLICY, CTR_IMPROVEMENT, impressions=10) == "medium"
    assert resolve_priority(_POLICY, CTR_IMPROVEMENT, impressions=100_000) == "high"
    assert resolve_priority(_POLICY, CONTENT_FRESHNESS_REVIEW, article_type="pricing") == "high"
    assert (
        resolve_priority(_POLICY, CONTENT_FRESHNESS_REVIEW, article_type="informational")
        == "medium"
    )


# ==================== indexing followup =======================================
def _indexing(**over):
    kwargs = dict(
        article_id=1,
        maturity=_maturity(),
        live_state="LIVE_HEALTHY",
        sitemap_state="SITEMAP_PRESENT",
        google_index_state="GSC_NOT_KNOWN_TO_GOOGLE",
        index_observed_days_ago=0,
        policy=_POLICY,
    )
    kwargs.update(over)
    return evaluate_indexing_followup(**kwargs)


def test_old_unindexed_healthy_article_gets_indexing_followup() -> None:
    candidate = _indexing()
    assert candidate is not None
    assert candidate.candidate_type == INDEXING_FOLLOWUP
    assert candidate.priority == "high"
    assert candidate.evidence_strength == EVIDENCE_INDEX_OBSERVATION


def test_no_indexing_followup_inside_the_grace_period() -> None:
    assert _indexing(maturity=_maturity(age_days=1)) is None


def test_no_indexing_followup_when_already_indexed() -> None:
    assert _indexing(google_index_state="GSC_INDEXED") is None


def test_no_indexing_followup_when_inspection_was_not_taken() -> None:
    assert _indexing(google_index_state="GSC_UNKNOWN") is None


def test_stale_inspection_snapshot_does_not_trigger_followup() -> None:
    """古い 1 回のスナップショットを恒久的な真実として扱わない。"""

    assert _indexing(index_observed_days_ago=90) is None


def test_no_indexing_followup_when_page_is_unhealthy_or_absent_from_sitemap() -> None:
    assert _indexing(live_state="LIVE_NOINDEX") is None
    assert _indexing(sitemap_state="SITEMAP_MISSING") is None


# ==================== ctr =====================================================
def _ctr(**over):
    kwargs = dict(
        article_id=1,
        maturity=_maturity(),
        impressions=1000,
        clicks=2,
        ctr=0.002,
        average_position=6.0,
        policy=_POLICY,
    )
    kwargs.update(over)
    return evaluate_ctr(**kwargs)


def test_weak_ctr_with_enough_impressions_is_a_candidate() -> None:
    candidate = _ctr()
    assert candidate is not None
    assert candidate.candidate_type == CTR_IMPROVEMENT
    assert candidate.evidence_strength == EVIDENCE_PERFORMANCE
    assert candidate.evidence["gate_minimum_impressions"] > 0
    # 原因を断定しない。
    assert "断定" in candidate.suggested_action


def test_immature_article_never_gets_a_ctr_candidate() -> None:
    assert _ctr(maturity=_maturity(search_state=SEARCH_NEWLY_PUBLISHED)) is None


def test_impressions_below_the_gate_produce_no_ctr_candidate() -> None:
    assert _ctr(impressions=10, ctr=0.0) is None


def test_healthy_ctr_produces_no_candidate() -> None:
    assert _ctr(ctr=0.12, clicks=120) is None


def test_deeply_ranked_page_is_not_a_ctr_problem() -> None:
    """順位が低いときの低 CTR は露出位置の問題であってタイトルの問題ではない。"""

    assert _ctr(average_position=70.0) is None


def test_missing_ctr_or_position_is_not_treated_as_zero() -> None:
    assert _ctr(ctr=None) is None
    assert _ctr(average_position=None) is None


# ==================== ranking =================================================
def test_ranking_in_the_opportunity_band_is_a_candidate() -> None:
    candidate = evaluate_ranking(
        article_id=1, maturity=_maturity(), impressions=500, average_position=12.0, policy=_POLICY
    )
    assert candidate is not None
    assert candidate.candidate_type == RANKING_IMPROVEMENT


def test_already_top_ranked_article_is_not_a_ranking_candidate() -> None:
    assert (
        evaluate_ranking(
            article_id=1,
            maturity=_maturity(),
            impressions=500,
            average_position=1.5,
            policy=_POLICY,
        )
        is None
    )


def test_far_ranked_article_is_not_a_ranking_candidate() -> None:
    assert (
        evaluate_ranking(
            article_id=1,
            maturity=_maturity(),
            impressions=500,
            average_position=90.0,
            policy=_POLICY,
        )
        is None
    )


def test_immature_article_never_gets_a_ranking_candidate() -> None:
    assert (
        evaluate_ranking(
            article_id=1,
            maturity=_maturity(search_state=SEARCH_NEWLY_PUBLISHED),
            impressions=500,
            average_position=12.0,
            policy=_POLICY,
        )
        is None
    )


# ==================== query expansion =========================================
def _queries(**over):
    row = {"query": "rpa 比較 ツール", "impressions": 120, "clicks": 1, "average_position": 15.0}
    row.update(over)
    return [row]


def test_uncovered_query_with_enough_impressions_is_a_candidate() -> None:
    found = evaluate_query_expansion(
        article_id=1,
        maturity=_maturity(),
        queries=_queries(),
        body_text="RPA の導入手順だけを扱う本文",
        policy=_POLICY,
    )
    assert len(found) == 1
    assert found[0].candidate_type == QUERY_EXPANSION
    assert found[0].evidence["scope"] in (SCOPE_EXPAND_EXISTING, SCOPE_POSSIBLE_NEW_ARTICLE)


def test_query_already_covered_by_the_body_is_not_a_candidate() -> None:
    found = evaluate_query_expansion(
        article_id=1,
        maturity=_maturity(),
        queries=_queries(),
        body_text="rpa 比較 ツール をすべて扱っています",
        policy=_POLICY,
    )
    assert found == []


def test_low_impression_query_is_not_a_candidate() -> None:
    found = evaluate_query_expansion(
        article_id=1,
        maturity=_maturity(),
        queries=_queries(impressions=2),
        body_text="無関係",
        policy=_POLICY,
    )
    assert found == []


def test_query_expansion_distinguishes_expansion_from_a_new_article() -> None:
    small = evaluate_query_expansion(
        article_id=1,
        maturity=_maturity(),
        queries=_queries(impressions=60),
        body_text="無関係",
        policy=_POLICY,
    )
    large = evaluate_query_expansion(
        article_id=1,
        maturity=_maturity(),
        queries=_queries(impressions=5000),
        body_text="無関係",
        policy=_POLICY,
    )
    assert small[0].evidence["scope"] == SCOPE_EXPAND_EXISTING
    assert large[0].evidence["scope"] == SCOPE_POSSIBLE_NEW_ARTICLE


def test_immature_article_gets_no_query_expansion() -> None:
    found = evaluate_query_expansion(
        article_id=1,
        maturity=_maturity(search_state=SEARCH_NEWLY_PUBLISHED),
        queries=_queries(),
        body_text="無関係",
        policy=_POLICY,
    )
    assert found == []


# ==================== cannibalization =========================================
def _pages(*pairs):
    return [
        {"article_id": a, "url": f"https://x.test/{a}/", "impressions": i, "clicks": 0}
        for a, i in pairs
    ]


def test_shared_query_across_two_pages_is_a_candidate_for_both() -> None:
    found = evaluate_cannibalization(
        query="ai 議事録", pages=_pages((2, 300), (7, 200)), policy=_POLICY
    )
    assert {c.article_id for c in found} == {2, 7}
    assert all(c.candidate_type == CANNIBALIZATION_REVIEW for c in found)
    assert all(c.priority == "high" for c in found)
    assert found[0].evidence["query"] == "ai 議事録"


def test_single_page_query_is_not_cannibalization() -> None:
    assert evaluate_cannibalization(query="q", pages=_pages((2, 300)), policy=_POLICY) == []


def test_low_impression_overlap_is_not_enough_evidence() -> None:
    found = evaluate_cannibalization(query="q", pages=_pages((2, 1), (7, 1)), policy=_POLICY)
    assert found == []


# ==================== engagement ==============================================
def _engagement(**over):
    kwargs = dict(
        article_id=1,
        maturity=_maturity(),
        organic_sessions=500,
        engagement_rate=0.1,
        average_engagement_seconds=5.0,
        policy=_POLICY,
    )
    kwargs.update(over)
    return evaluate_engagement(**kwargs)


def test_weak_engagement_with_enough_organic_sessions_is_a_candidate() -> None:
    candidate = _engagement()
    assert candidate is not None
    assert candidate.candidate_type == ENGAGEMENT_REVIEW
    assert "engagement_rate" in candidate.evidence["failing_metrics"]


def test_no_engagement_candidate_without_a_sufficient_ga4_sample() -> None:
    assert _engagement(maturity=_maturity(engagement_state=ENGAGEMENT_AWAITING_DATA)) is None


def test_healthy_engagement_produces_no_candidate() -> None:
    assert _engagement(engagement_rate=0.8, average_engagement_seconds=120.0) is None


# ==================== freshness ===============================================
def test_aged_pricing_sources_are_a_high_priority_freshness_candidate() -> None:
    candidate = evaluate_freshness(
        article_id=1,
        article_type="pricing",
        latest_source_checked_on=date(2026, 1, 1),
        today=_TODAY,
        source_count=3,
        policy=_POLICY,
    )
    assert candidate is not None
    assert candidate.candidate_type == CONTENT_FRESHNESS_REVIEW
    assert candidate.priority == "high"
    assert candidate.evidence_strength == EVIDENCE_SOURCE_METADATA


def test_recent_sources_are_not_stale() -> None:
    assert (
        evaluate_freshness(
            article_id=1,
            article_type="pricing",
            latest_source_checked_on=date(2026, 10, 20),
            today=_TODAY,
            source_count=3,
            policy=_POLICY,
        )
        is None
    )


def test_freshness_gate_differs_by_article_type() -> None:
    checked = date(2026, 6, 1)  # 152 日前
    assert (
        evaluate_freshness(
            article_id=1,
            article_type="pricing",
            latest_source_checked_on=checked,
            today=_TODAY,
            source_count=1,
            policy=_POLICY,
        )
        is not None
    )
    assert (
        evaluate_freshness(
            article_id=1,
            article_type="informational",
            latest_source_checked_on=checked,
            today=_TODAY,
            source_count=1,
            policy=_POLICY,
        )
        is None
    )


def test_article_without_sources_has_no_freshness_candidate() -> None:
    assert (
        evaluate_freshness(
            article_id=1,
            article_type="pricing",
            latest_source_checked_on=None,
            today=_TODAY,
            source_count=0,
            policy=_POLICY,
        )
        is None
    )


def test_freshness_is_independent_of_traffic() -> None:
    """トラフィックがゼロでも鮮度候補は成立する。"""

    candidate = evaluate_freshness(
        article_id=1,
        article_type="pricing",
        latest_source_checked_on=date(2026, 1, 1),
        today=_TODAY,
        source_count=1,
        policy=_POLICY,
    )
    assert candidate is not None


# ==================== internal links ==========================================
def test_structural_link_candidate_is_labelled_structural() -> None:
    candidate = evaluate_internal_link(
        source_article_id=18,
        target_article_id=16,
        relation="deferred pair",
        target_url="https://x.test/rpa-tools/",
        target_title="RPAおすすめ",
        policy=_POLICY,
    )
    assert candidate.candidate_type == INTERNAL_LINK_OPPORTUNITY
    assert candidate.evidence_strength == EVIDENCE_STRUCTURAL
    assert candidate.priority == "low"


def test_link_candidate_with_performance_evidence_is_labelled_performance() -> None:
    candidate = evaluate_internal_link(
        source_article_id=18,
        target_article_id=16,
        relation="shared query",
        target_url="https://x.test/rpa-tools/",
        target_title=None,
        policy=_POLICY,
        performance_evidence={"query": "rpa", "impressions": 100},
    )
    assert candidate.evidence_strength == EVIDENCE_PERFORMANCE
