"""Growth Action の識別・既存の仕事との重なり・受け箱・まとめ・変換 (C9 Batch 2、pure)。

pin する契約:

- 機会の鍵と証拠の指紋は決定的。時間が経つだけ (経過日数・投稿の経過時間・views) では
  変わらない。判断に意味のある値 (信頼できるクリック・切り口・出所の状態) が変われば変わる。
- 開いている通常の提案・最近の通常の投稿・在庫の保守の計画は、Threads の候補を「既存の仕事が
  担う」にする。Growth と通常の投稿は互いを覆わない。
- 受け箱のまとめは少数で、1 つの行動の種類で埋め尽くさない。並べ方は決定的。
- 変換は計画だけ。自然な入口が無いものは unsupported と明示する。
"""

from __future__ import annotations

from app.growth import analysis as ga
from app.growth import conversion as gc
from app.growth import inbox as gi


def _src(source, state=ga.USABLE, **metrics):
    return ga.SourceEvidence(source, state, "test", "test", metrics=metrics)


def _article(aid=1, *, age_days=10, clean=3, threads=None, excluded=0):
    sources = {s: _src(s, ga.UNAVAILABLE) for s in ga.SOURCES}
    sources["affiliate"] = _src("affiliate", ga.INSUFFICIENT, clean_clicks=clean,
                                excluded_instrumentation_clicks=excluded)
    sources["threads"] = threads or _src("threads", ga.UNAVAILABLE, publications=0,
                                         open_proposals=[])
    return ga.GrowthEvidence(subject_type="article", subject_id=f"article:{aid}", article_id=aid,
                             article={"status": "published", "age_days": age_days,
                                      "monetization_mode": "affiliate", "monetized": True},
                             sources=sources)  # fmt: skip


def _threads(*, tried=("insight",), latest=40.0, oldest=100.0, views=10, ranks=(0.2,)):
    return _src("threads", ga.USABLE, publications=len(tried), latest_age_hours=latest,
                oldest_age_hours=oldest, angles_tried=list(tried), latest_views=views,
                reach_percentile_ranks=list(ranks), open_proposals=[])


def _cands(evidence, **kw):
    return {c.action_type: c for c in ga.build_candidates(evidence, ga.classify(evidence, **kw))}


ANGLES = ("insight", "common_mistake", "comparison", "question", "beginner_tip")


# == 識別 ================================
def test_the_opportunity_key_is_deterministic_and_per_article() -> None:
    # C9-A: 別の切り口の機会は記事ごとに 1 つ (勧める切り口は鍵に入れない)。
    c = _cands(_article(threads=_threads()), angles=ANGLES)
    alt = c[ga.CREATE_THREADS_ALTERNATIVE_ANGLE]
    assert alt.opportunity_key == "create_threads_alternative_angle:article:article:1"
    assert alt.variant is None and alt.recommendation["angle"] == "common_mistake"
    assert c[ga.REVIEW_AFFILIATE_PLACEMENT].opportunity_key == (
        "review_affiliate_placement:article:article:1")  # fmt: skip


def test_time_passing_alone_does_not_change_the_evidence_fingerprint() -> None:
    a = _cands(_article(age_days=10, threads=_threads(latest=40, oldest=100, views=10)),
               angles=ANGLES)
    b = _cands(_article(age_days=12, threads=_threads(latest=88, oldest=148, views=55)),
               angles=ANGLES)
    for action in (ga.REVIEW_AFFILIATE_PLACEMENT, ga.CREATE_THREADS_ALTERNATIVE_ANGLE):
        assert a[action].evidence_fingerprint == b[action].evidence_fingerprint
        assert a[action].candidate_fingerprint == b[action].candidate_fingerprint


def test_meaningful_changes_change_the_fingerprint_but_not_the_opportunity() -> None:
    a = _cands(_article(clean=3))[ga.REVIEW_AFFILIATE_PLACEMENT]
    b = _cands(_article(clean=5))[ga.REVIEW_AFFILIATE_PLACEMENT]
    assert a.opportunity_key == b.opportunity_key
    assert a.evidence_fingerprint != b.evidence_fingerprint
    # 順位が中央値を跨げば、別の切り口の証拠も変わる (値そのものの小さな動きでは変わらない)。
    low = _cands(_article(threads=_threads(ranks=(0.2,))), angles=ANGLES)
    lower = _cands(_article(threads=_threads(ranks=(0.3,))), angles=ANGLES)
    high = _cands(_article(threads=_threads(ranks=(0.8,))), angles=ANGLES)
    key = ga.CREATE_THREADS_ALTERNATIVE_ANGLE
    assert low[key].evidence_fingerprint == lower[key].evidence_fingerprint
    assert low[key].evidence_fingerprint != high[key].evidence_fingerprint


def test_the_recommendation_avoids_the_sites_most_recent_angles() -> None:
    c = _cands(_article(threads=_threads()), angles=ANGLES,
               recent_angles=("common_mistake", "comparison"))
    assert c[ga.CREATE_THREADS_ALTERNATIVE_ANGLE].recommendation["angle"] == "question"


# == 既存の仕事との重なり (Threads) ================================
def _dict(candidate):
    return candidate.as_dict()


def _alt():
    return _dict(_cands(_article(threads=_threads()), angles=ANGLES)[
        ga.CREATE_THREADS_ALTERNATIVE_ANGLE])  # fmt: skip


def test_an_open_or_awaiting_regular_proposal_covers_the_threads_candidate() -> None:
    for status in ("proposed", "awaiting_approval", "approved"):
        ctx = gi.CoverageContext(open_regular_proposals={1: ({"id": 34, "status": status,
                                                              "angle": "insight"},)})
        availability, reasons = gi.assess(_alt(), ctx)
        assert availability == gi.COVERED and "#34" in reasons[0]


def test_a_recent_regular_post_covers_an_alternative_angle() -> None:
    availability, reasons = gi.assess(_alt(), gi.CoverageContext(
        latest_regular_post_hours={1: 12.0}))  # fmt: skip
    assert availability == gi.COVERED and "let it mature" in reasons[0]
    assert gi.assess(_alt(), gi.CoverageContext(latest_regular_post_hours={1: 200.0}))[0] == (
        gi.ACTIONABLE_NOW)  # fmt: skip


def test_the_stock_plan_covers_and_other_articles_stay_actionable() -> None:
    ctx = gi.CoverageContext(stock_planned={1: ("question",)},
                             open_regular_proposals={2: ({"id": 9, "status": "proposed"},)})
    assert gi.assess(_alt(), ctx)[0] == gi.COVERED
    other = _dict(_cands(_article(2, threads=_threads()), angles=ANGLES)[
        ga.CREATE_THREADS_ALTERNATIVE_ANGLE])  # fmt: skip
    assert gi.assess(other, gi.CoverageContext(stock_planned={1: ("question",)}))[0] == (
        gi.ACTIONABLE_NOW)  # fmt: skip


def test_growth_and_regular_lanes_do_not_cover_each_other() -> None:
    # 記事の無い Growth の提案は open_regular_proposals に入らない (記事の候補を覆わない)。
    growth_only = gi.CoverageContext(growth_proposal_today=36)
    assert gi.assess(_alt(), growth_only)[0] == gi.ACTIONABLE_NOW
    site = [c.as_dict() for c in ga.site_candidates(
        data_quality=[], growth_plan={"due": True, "date_jst": "2026-09-30"}, freshness={})]
    [growth] = site
    regular_only = gi.CoverageContext(open_regular_proposals={1: ({"id": 34},)},
                                      growth_lane_automatic=False)
    assert gi.assess(growth, regular_only)[0] == gi.ACTIONABLE_NOW
    assert gi.assess(growth, gi.CoverageContext(growth_proposal_today=36))[0] == gi.COVERED


def test_informational_blocked_and_open_reviews() -> None:
    wait = _dict(ga.build_candidates(_article(clean=0), [ga.Opportunity(
        ga.INSUFFICIENT_EVIDENCE, ga.WAIT_FOR_MORE_DATA, "data_quality", "x")])[0])
    assert gi.assess(wait, gi.CoverageContext())[0] == gi.INFORMATIONAL
    stale = _article(clean=3)
    stale = ga.GrowthEvidence(**{**stale.__dict__, "sources": {**stale.sources,
                                                               "seo": _src("seo", ga.STALE)}})
    blocked = _dict(_cands(stale)[ga.REVIEW_AFFILIATE_PLACEMENT])
    assert gi.assess(blocked, gi.CoverageContext())[0] == gi.BLOCKED
    open_review = gi.CoverageContext(open_reviews={blocked["opportunity_key"]: 7})
    assert gi.assess(blocked, open_review) == (gi.COVERED,
                                               ["a C9 review is already open (#7)"])


# == 受け箱・まとめ ================================
def _entry(action, subject, *, evidence="structural", potential=1, availability=gi.ACTIONABLE_NOW):
    rank = {"structural": 1, "hypothesis": 2}[evidence]
    return {"action_type": action, "subject_id": subject, "variant": None,
            "availability": availability, "candidate_fingerprint": f"{action}:{subject}",
            "priority": {"evidence_strength": {"rank": rank}, "potential_opportunity":
                         {"rank": potential}, "monetization_relevance": {"rank": 1},
                         "effort": {"rank": 1}, "recency_urgency": {"rank": 1}}}  # fmt: skip


def test_the_digest_does_not_let_one_action_type_flood_it() -> None:
    entries = [_entry(ga.REVIEW_INTERNAL_LINKS, f"article:{i}") for i in range(20)]
    entries += [_entry(ga.CREATE_NEW_ARTICLE, f"keyword:{i}") for i in range(5)]
    entries += [_entry(ga.REVIEW_AFFILIATE_PLACEMENT, "article:1", evidence="hypothesis")]
    entries += [_entry(ga.CREATE_REGULAR_THREADS_POST, "article:9", availability=gi.COVERED)]
    selected, skipped = gi.select_digest(entries, limit=8, per_action=2)
    types = [e["action_type"] for e in selected]
    assert types[0] == ga.REVIEW_AFFILIATE_PLACEMENT  # 行動の証拠が先
    assert max(types.count(t) for t in set(types)) <= 2
    assert ga.CREATE_REGULAR_THREADS_POST not in types  # 覆われたものは入らない
    assert len(selected) == 5 and all("selection_reason" in e for e in selected)
    assert len(skipped) == 21


def test_grouping_and_ordering_are_deterministic() -> None:
    entries = [_entry(ga.REVIEW_INTERNAL_LINKS, f"article:{i}", potential=i % 3)
               for i in range(6)]  # fmt: skip
    a = gi.group_by_action(entries)
    b = gi.group_by_action(list(reversed(entries)))
    assert [e["subject_id"] for e in a[ga.REVIEW_INTERNAL_LINKS]] == [
        e["subject_id"] for e in b[ga.REVIEW_INTERNAL_LINKS]]  # fmt: skip
    assert a[ga.REVIEW_INTERNAL_LINKS][0]["priority"]["potential_opportunity"]["rank"] == 2


# == 変換 (計画だけ) ================================
def test_conversion_plans_are_explicit() -> None:
    def plan(action, **kw):
        return gc.plan_conversion({"action_type": action, "article_id": 10, **kw})

    links = plan(ga.REVIEW_INTERNAL_LINKS)
    assert links.support == gc.SUPPORTED and "propose_change.py" in links.entry_point
    assert plan(ga.CREATE_NEW_ARTICLE, keyword_id=12).entry_point == (
        "scripts/export_article_plan.py --keyword-id 12")  # fmt: skip
    for action in (ga.CREATE_REGULAR_THREADS_POST, ga.CREATE_THREADS_ALTERNATIVE_ANGLE,
                   ga.UPDATE_EXISTING_ARTICLE, ga.IMPROVE_SEARCH_SNIPPET):
        p = plan(action)
        assert p.support == gc.UNSUPPORTED and p.entry_point is None
        assert "unsupported conversion" in p.note
    assert plan(ga.CREATE_GROWTH_POST).support == gc.LANE
    assert plan(ga.REVIEW_AFFILIATE_PLACEMENT).support == gc.MANUAL
    assert plan(ga.WAIT_FOR_MORE_DATA).support == gc.NOT_APPLICABLE
    assert all(not plan(a).writes_on_plan for a in ga.ACTION_TYPES)


def test_an_unrelated_source_does_not_create_a_new_revision() -> None:
    def evidence(threads_state):
        base = _article(threads=_threads())
        return ga.GrowthEvidence(**{**base.__dict__, "sources": {
            **base.sources, "threads": _src("threads", threads_state, publications=1,
                                            latest_age_hours=40.0, oldest_age_hours=100.0,
                                            angles_tried=["insight"], open_proposals=[])}})

    seo = [{"candidate_type": "INTERNAL_LINK_OPPORTUNITY", "reason_code": "MISSING",
            "priority": "low", "evidence": {"target_article_id": 24}}]
    a, b = evidence(ga.INSUFFICIENT), evidence(ga.USABLE)
    a = ga.GrowthEvidence(**{**a.__dict__, "existing_candidates": {"seo": tuple(seo)}})
    b = ga.GrowthEvidence(**{**b.__dict__, "existing_candidates": {"seo": tuple(seo)}})
    links_a = _cands(a, angles=ANGLES)[ga.REVIEW_INTERNAL_LINKS]
    links_b = _cands(b, angles=ANGLES)[ga.REVIEW_INTERNAL_LINKS]
    assert links_a.evidence_fingerprint == links_b.evidence_fingerprint  # Threads は関係ない
    assert "threads" not in links_a.source_states
    alt_a = _cands(a, angles=ANGLES)[ga.CREATE_THREADS_ALTERNATIVE_ANGLE]
    alt_b = _cands(b, angles=ANGLES)[ga.CREATE_THREADS_ALTERNATIVE_ANGLE]
    assert alt_a.evidence_fingerprint != alt_b.evidence_fingerprint  # Threads の候補は変わる
