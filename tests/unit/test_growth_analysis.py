"""成長の証拠と行動の候補 (C9、pure)。

pin する契約:

- 欠測は 0 にしない。出所ごとに unavailable / stale / insufficient / usable を持つ。
- 全体の証拠の段階は、使える読者の行動の出所の数で決まる (Threads の閾値を流用しない)。
- 信頼できる計測開始より前のクリックは行動に使わない。報酬は記事の収益にしない。
- 既存のエンジン (C6/C7) の候補を型と行動に写す (閾値を作り直さない)。
- 1 つの総合点で並べない (成分を別々に持つ)。候補は決定的。
"""

from __future__ import annotations

import random

from app.growth import analysis as ga


def _src(source, state=ga.USABLE, **metrics):
    return ga.SourceEvidence(source, state, "test", "test", metrics=metrics)


def _article(aid=1, *, status="published", age_days=40, mode="affiliate", monetized=False,
             sources=None, seo=(), revenue=()) -> ga.GrowthEvidence:  # fmt: skip
    base = {s: _src(s, ga.UNAVAILABLE) for s in ga.SOURCES}
    base["threads"] = _src("threads", ga.UNAVAILABLE, publications=0, open_proposals=[])
    base["affiliate"] = _src("affiliate", ga.UNAVAILABLE, clean_clicks=None)
    base.update(sources or {})
    return ga.GrowthEvidence(
        subject_type="article", subject_id=f"article:{aid}", article_id=aid,
        article={"status": status, "age_days": age_days, "monetization_mode": mode,
                 "monetized": monetized},
        sources=base, existing_candidates={"seo": tuple(seo), "revenue": tuple(revenue)},
    )  # fmt: skip


def _candidates(evidence, **kw):
    return ga.build_candidates(evidence, ga.classify(evidence, **kw))


def _actions(evidence, **kw):
    return {c.action_type for c in _candidates(evidence, **kw)}


# == 出所の状態 ===================================================================================
def test_missing_sources_are_unavailable_not_zero() -> None:
    seo = ga.seo_source(None, maturity=None, freshness=ga.UNAVAILABLE, coverage_through=None)
    assert seo.state == ga.UNAVAILABLE and seo.metrics["impressions"] is None
    ga4 = ga.ga4_source({"configured": False}, maturity=None, freshness="fresh",
                        coverage_through=None)  # fmt: skip
    assert ga4.state == ga.UNAVAILABLE and ga4.metrics["sessions"] is None
    aff = ga.affiliate_source(None, freshness="fresh", data_through=None, trusted_start=None)
    assert aff.state == ga.UNAVAILABLE and aff.metrics["clean_clicks"] is None
    threads = ga.threads_source([])
    assert threads.state == ga.UNAVAILABLE and threads.metrics["publications"] == 0
    assert ga.keyword_source(None, age_days=None).state == ga.UNAVAILABLE
    assert ga.index_source(None, observed_at=None).state == ga.UNAVAILABLE


def test_source_maturity_follows_each_engine() -> None:
    usable = ga.seo_source({"impressions": 900}, maturity="sufficient_search_sample",
                           freshness="fresh", coverage_through="2026-09-28")  # fmt: skip
    assert usable.state == ga.USABLE and usable.coverage_through == "2026-09-28"
    few = ga.seo_source({"impressions": 2}, maturity="insufficient_impressions",
                        freshness="fresh", coverage_through=None)  # fmt: skip
    assert few.state == ga.INSUFFICIENT
    stale = ga.seo_source({"impressions": 900}, maturity="sufficient_search_sample",
                          freshness=ga.STALE, coverage_through=None)  # fmt: skip
    assert stale.state == ga.STALE
    assert ga.keyword_source({"total_score": 70}, age_days=90).state == ga.STALE
    assert ga.keyword_source({"total_score": 70}, age_days=3).state == ga.USABLE


def test_the_overall_state_counts_usable_behaviour_sources() -> None:
    assert _article().evidence_state == ga.EVIDENCE_INSUFFICIENT
    one = _article(sources={"seo": _src("seo")})
    assert one.evidence_state == ga.EVIDENCE_HYPOTHESIS
    two = _article(sources={"seo": _src("seo"), "ga4": _src("ga4")})
    assert two.evidence_state == ga.EVIDENCE_PRELIMINARY
    three = _article(sources={"seo": _src("seo"), "ga4": _src("ga4"),
                              "affiliate": _src("affiliate", clean_clicks=40)})  # fmt: skip
    assert three.evidence_state == ga.EVIDENCE_DESCRIPTIVE
    # keyword・index・commission は読者の行動ではないので数えない。
    assert _article(sources={"keyword": _src("keyword"), "index": _src("index")}
                    ).evidence_state == ga.EVIDENCE_INSUFFICIENT  # fmt: skip


def test_zero_denominators_give_none() -> None:
    assert ga.safe_ratio(1, 0) is None and ga.safe_ratio(None, 5) is None
    assert ga.safe_ratio(1, 4) == 0.25
    assert ga.percentile_of(3, []) is None


def test_commission_stays_program_level() -> None:
    program = ga.commission_source(ever_had_data=True, attribution="program_only",
                                   coverage_through="2026-09-29")  # fmt: skip
    assert program.state == ga.UNAVAILABLE
    assert program.metrics == {"attribution_scope": "program_only", "article_level_revenue": None}
    none = ga.commission_source(ever_had_data=False, attribution=None, coverage_through=None)
    assert none.metrics["article_level_revenue"] is None


def test_instrumentation_clicks_are_not_reader_interest() -> None:
    dirty = _article(sources={"affiliate": _src("affiliate", ga.INSUFFICIENT, clean_clicks=0,
                                                excluded_instrumentation_clicks=12)})
    assert ga.AFFILIATE_INTEREST not in {o.pattern for o in ga.classify(dirty)}
    clean = _article(sources={"affiliate": _src("affiliate", ga.INSUFFICIENT, clean_clicks=3,
                                                excluded_instrumentation_clicks=12)})
    interest = [o for o in ga.classify(clean) if o.pattern == ga.AFFILIATE_INTEREST]
    assert interest and interest[0].evidence == {"clean_clicks": 3,
                                                 "excluded_instrumentation_clicks": 12}


def test_index_states() -> None:
    assert ga.index_source({"google_index_state": "GSC_INDEXED"}, observed_at="x").state == (
        ga.USABLE)  # fmt: skip
    assert ga.index_source({"google_index_state": "GSC_UNKNOWN", "live_state": "LIVE_HEALTHY"},
                           observed_at="x").state == ga.INSUFFICIENT  # fmt: skip
    not_indexed = ga.index_source({"google_index_state": "GSC_DISCOVERED_NOT_INDEXED"},
                                  observed_at="x")  # fmt: skip
    assert not_indexed.maturity == "GSC_DISCOVERED_NOT_INDEXED"


# == 型と行動 =====================================================================================
def test_an_unpublished_article_gets_no_candidates() -> None:
    assert ga.classify(_article(status="drafting")) == []


def test_a_published_article_without_data_waits_and_is_offered_threads() -> None:
    actions = _actions(_article())
    assert ga.WAIT_FOR_MORE_DATA in actions
    assert ga.CREATE_REGULAR_THREADS_POST in actions  # Threads の紹介が無い (構造)
    wait = next(c for c in _candidates(_article()) if c.action_type == ga.WAIT_FOR_MORE_DATA)
    assert wait.evidence_state == ga.EVIDENCE_INSUFFICIENT
    assert wait.requires_human_approval is False and wait.external_write_required is None


def test_search_visibility_from_the_c6_candidates() -> None:
    seo = [{"candidate_type": "CTR_IMPROVEMENT", "reason_code": "WEAK_CTR_FOR_POSITION",
            "priority": "high", "evidence": {"impressions": 1000, "ctr": 0.002}}]
    evidence = _article(sources={"seo": _src("seo")}, seo=seo)
    snippet = next(c for c in _candidates(evidence) if c.action_type == ga.IMPROVE_SEARCH_SNIPPET)
    assert snippet.patterns == (ga.SEARCH_VISIBILITY,)
    assert snippet.priority["potential_opportunity"]["level"] == "high"  # C6 の優先度
    assert snippet.evidence_state == ga.EVIDENCE_HYPOTHESIS
    assert snippet.requires_human_approval and snippet.external_write_required == "wordpress"
    links = _candidates(_article(seo=[{"candidate_type": "INTERNAL_LINK_OPPORTUNITY",
                                       "priority": "low"}]))  # fmt: skip
    assert next(c for c in links if c.action_type == ga.REVIEW_INTERNAL_LINKS).evidence_state == (
        ga.EVIDENCE_STRUCTURAL)  # fmt: skip


def test_content_refresh_candidate() -> None:
    seo = [{"candidate_type": "CONTENT_FRESHNESS_REVIEW", "reason_code": "SOURCES_AGED",
            "priority": "medium"}]
    c = next(c for c in _candidates(_article(seo=seo))
             if c.action_type == ga.UPDATE_EXISTING_ARTICLE)  # fmt: skip
    assert ga.CONTENT_REFRESH in c.patterns
    assert c.priority["recency_urgency"]["level"] == "medium"


def test_monetization_opportunity_from_the_c7_candidates() -> None:
    revenue = [{"candidate_type": "HIGH_TRAFFIC_UNMONETIZED", "priority": "high"}]
    c = next(c for c in _candidates(_article(revenue=revenue))
             if c.action_type == ga.REVIEW_AFFILIATE_PLACEMENT)  # fmt: skip
    assert ga.MONETIZATION in c.patterns
    tracking = [{"candidate_type": "TRACKING_SETUP_REQUIRED", "priority": "medium"}]
    c = next(c for c in _candidates(_article(revenue=tracking))
             if c.action_type == ga.REVIEW_AFFILIATE_PLACEMENT)  # fmt: skip
    assert any("affiliate provider" in b for b in c.blockers)


def test_threads_repromotion_after_the_window_and_with_a_blocker() -> None:
    old = _src("threads", ga.USABLE, publications=1, latest_age_hours=20 * 24,
               oldest_age_hours=20 * 24, angles_tried=["insight"], open_proposals=[34])
    c = next(c for c in _candidates(_article(sources={"threads": old}))
             if c.action_type == ga.CREATE_REGULAR_THREADS_POST)  # fmt: skip
    assert ga.THREADS_REPROMOTION in c.patterns
    assert any("#34" in b for b in c.blockers)
    recent = _src("threads", ga.USABLE, publications=1, latest_age_hours=30,
                  oldest_age_hours=30, angles_tried=["insight"], open_proposals=[])
    assert ga.CREATE_REGULAR_THREADS_POST not in _actions(_article(sources={"threads": recent}))


def test_alternative_angle_is_behavioural_only_when_the_article_ranks_low() -> None:
    def threads(ranks):
        return _src("threads", ga.USABLE, publications=2, latest_age_hours=30,
                    oldest_age_hours=60, angles_tried=["insight"], reach_percentile_ranks=ranks,
                    open_proposals=[])  # fmt: skip

    angles = ("insight", "comparison", "question")
    low = [o for o in ga.classify(_article(sources={"threads": threads([0.1, 0.3])}),
                                  angles=angles) if o.pattern == ga.THREADS_ALTERNATIVE_ANGLE]
    assert low[0].basis == "behavioral" and low[0].evidence["untried_angles"] == [
        "comparison", "question"]  # fmt: skip
    high = [o for o in ga.classify(_article(sources={"threads": threads([0.8, 0.9])}),
                                   angles=angles) if o.pattern == ga.THREADS_ALTERNATIVE_ANGLE]
    assert high[0].basis == "structural"
    young = _src("threads", ga.USABLE, publications=1, latest_age_hours=5, oldest_age_hours=5,
                 angles_tried=["insight"], open_proposals=[])
    assert ga.CREATE_THREADS_ALTERNATIVE_ANGLE not in _actions(
        _article(sources={"threads": young}), angles=angles)  # fmt: skip


def test_a_keyword_without_an_article_is_a_new_content_opportunity() -> None:
    keyword = ga.GrowthEvidence(subject_type="keyword", subject_id="keyword:7", keyword_id=7,
                                sources={"keyword": _src("keyword", total_score=81.0)})
    [c] = ga.build_candidates(keyword, ga.classify(keyword),
                              context={"keyword_percentile": 0.9, "commercial_rank": 2})
    assert c.action_type == ga.CREATE_NEW_ARTICLE and c.patterns == (ga.NEW_CONTENT,)
    # 機会の大きさは他のキーワードとの相対の位置 (スコアそのものを総合点にしない)。
    assert c.priority["potential_opportunity"]["level"] == "high"
    assert "percentile" in c.priority["potential_opportunity"]["reason"]
    assert c.effort == "high" and c.evidence_state == ga.EVIDENCE_STRUCTURAL


def test_stale_sources_block_and_raise_urgency() -> None:
    evidence = _article(sources={"seo": _src("seo", ga.STALE)},
                        seo=[{"candidate_type": "RANKING_IMPROVEMENT", "priority": "medium"}])
    c = next(c for c in _candidates(evidence) if c.action_type == ga.UPDATE_EXISTING_ARTICLE)
    assert any("stale" in b for b in c.blockers)
    assert c.priority["recency_urgency"]["level"] == "high"


def test_site_candidates_for_data_quality_and_the_growth_lane() -> None:
    site = ga.site_candidates(
        data_quality=[{"candidate_type": "AFFILIATE_DATA_QUALITY_REVIEW",
                       "reason_code": "CLICKS_BEFORE_TRUSTED_BASELINE", "priority": "low",
                       "evidence": {"excluded_clicks": 29, "token": "secret"}}],
        growth_plan={"due": True, "date_jst": "2026-09-30"}, freshness={"ga4": ga.STALE})
    actions = {c.action_type for c in site}
    assert actions == {ga.INVESTIGATE_DATA_QUALITY, ga.CREATE_GROWTH_POST}
    dq = next(c for c in site if c.action_type == ga.INVESTIGATE_DATA_QUALITY)
    assert all("token" not in e["evidence"] for e in dq.evidence)  # 秘密らしい値は入れない
    assert len(dq.evidence) == 2  # データの質 + 古い出所


# == 並べ方・決定性 ================================================================================
def test_priority_components_are_separate_and_the_order_is_by_components() -> None:
    items = [_article(1, sources={"seo": _src("seo")},
                      seo=[{"candidate_type": "CTR_IMPROVEMENT", "priority": "low"}]),
             _article(2, seo=[{"candidate_type": "INTERNAL_LINK_OPPORTUNITY",
                               "priority": "high"}])]  # fmt: skip
    candidates = ga.sort_candidates(c for e in items for c in _candidates(e))
    first = candidates[0]
    assert set(first.priority) == {"evidence_strength", "potential_opportunity",
                                   "recency_urgency", "effort", "monetization_relevance"}
    assert "score" not in first.as_dict()
    # 行動の証拠 (仮説) は、構造だけの証拠 (優先度 high) より前。
    assert first.action_type == ga.IMPROVE_SEARCH_SNIPPET


def test_candidates_are_deterministic() -> None:
    items = [_article(i, sources={"seo": _src("seo")},
                      seo=[{"candidate_type": "CTR_IMPROVEMENT", "priority": "medium"}])
             for i in range(1, 6)]  # fmt: skip
    a = [c for e in items for c in _candidates(e)]
    shuffled = list(items)
    random.Random(3).shuffle(shuffled)
    b = [c for e in shuffled for c in _candidates(e)]
    assert ga.fingerprint(a) == ga.fingerprint(b)
    assert [c.subject_id for c in ga.sort_candidates(a)] == [
        c.subject_id for c in ga.sort_candidates(b)]  # fmt: skip
