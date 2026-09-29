"""別の切り口の機会の識別を記事ごとに固定する・古い形の行との互換 (C9-A)。

pin する契約:

- 勧める切り口が変わるだけ (サイトの直近の切り口の入れ替わり) では、新しい行を作らない。
- 判断に意味のある変化 (順位の中央値を跨ぐ・試した切り口が増える) は新しい版を作る。
- 古い形 (鍵と material に切り口が入っていた) の行は消さず・書き換えず、今の規則で同じなら
  その行を同じ候補として扱う (却下・変換・レビュー中の状態もそのまま効く)。
- 1 つの機会に生きている行は 1 つ。証拠が前の版に戻っても、いまの版は生きたまま。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.growth import analysis as ga
from app.growth import inbox as gi
from app.models.growth_action import GA_CONVERTED, GrowthActionCandidate
from app.services import growth_action_service as gas

_NOW = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)
ALT = ga.CREATE_THREADS_ALTERNATIVE_ANGLE


def _src(source, state=ga.USABLE, **metrics):
    return ga.SourceEvidence(source, state, "test", "test", metrics=metrics)


def _report(*, recent=(), ranks=(0.2,), tried=("insight",), article=1) -> dict:
    sources = {s: _src(s, ga.UNAVAILABLE) for s in ga.SOURCES}
    sources["threads"] = _src("threads", ga.USABLE, publications=len(tried), latest_age_hours=90,
                              oldest_age_hours=90, angles_tried=list(tried),
                              reach_percentile_ranks=list(ranks), open_proposals=[])
    evidence = ga.GrowthEvidence(subject_type="article", subject_id=f"article:{article}",
                                 article_id=article,
                                 article={"status": "published", "age_days": 20,
                                          "monetization_mode": "supporting"},
                                 sources=sources)  # fmt: skip
    cands = ga.build_candidates(evidence, ga.classify(
        evidence, angles=("insight", "comparison", "question"), recent_angles=recent))
    return {"candidates": [c.as_dict() for c in cands if c.action_type == ALT],
            "growth_plan": None}  # fmt: skip


def _legacy(candidate: dict, angle: str) -> dict:
    """C9-A より前の形 (鍵と material に切り口) の候補を作る (古い行を再現するため)。"""

    legacy = dict(candidate)
    legacy["opportunity_key"] = f"{candidate['opportunity_key']}:angle={angle}"
    legacy["variant"] = f"angle={angle}"
    legacy["material"] = [{**m, "angle": angle} for m in candidate["material"]]
    legacy["evidence_fingerprint"] = ga.evidence_fingerprint_for(legacy["source_states"],
                                                                 legacy["material"])
    legacy["candidate_fingerprint"] = ga.candidate_fingerprint_for({
        **{k: legacy[k] for k in ("patterns", "effort", "reversible", "requires_human_approval",
                                  "external_write_required", "prerequisites", "blockers")},
        "opportunity_key": legacy["opportunity_key"],
        "evidence_fingerprint": legacy["evidence_fingerprint"]})  # fmt: skip
    return legacy


def _refresh(session, report, hours=0):
    return gas.GrowthActionHistory(session).refresh(
        report, gi.CoverageContext(), now=_NOW + timedelta(hours=hours), execute=True)


def _rows(session):
    return list(session.scalars(select(GrowthActionCandidate).order_by(GrowthActionCandidate.id)))


def _live(session):
    return [r for r in _rows(session) if r.status != "superseded" and r.superseded_by_id is None
            and r.availability != gas.NOT_OBSERVED]


def test_recommendation_changes_alone_create_no_rows(session) -> None:
    first = _report(recent=())
    rotated = _report(recent=("comparison",))
    a, b = first["candidates"][0], rotated["candidates"][0]
    assert a["recommendation"]["angle"] == "comparison" and b["recommendation"]["angle"] == (
        "question")  # 勧めは変わる  # fmt: skip
    assert a["opportunity_key"] == b["opportunity_key"] == ALT + ":article:article:1"
    assert a["candidate_fingerprint"] == b["candidate_fingerprint"]
    _refresh(session, first)
    for hour, report in enumerate((rotated, first, rotated), 1):
        out = _refresh(session, report, hours=hour)
        assert out["plan"]["counts"]["decisions"] == {gas.REOBSERVED: 1}
    assert len(_rows(session)) == 1


def test_material_changes_create_a_revision(session) -> None:
    _refresh(session, _report(ranks=(0.2,)))
    _refresh(session, _report(ranks=(0.8,)), hours=1)  # 中央値を跨いだ
    _refresh(session, _report(ranks=(0.8,), tried=("insight", "comparison")), hours=2)
    rows = _rows(session)
    assert [r.revision for r in rows] == [1, 2, 3]
    assert [r.status for r in rows][:2] == ["superseded", "superseded"]
    assert len(_live(session)) == 1


def test_a_legacy_row_is_reobserved_not_duplicated(session) -> None:
    current = _report()
    legacy = {"candidates": [_legacy(current["candidates"][0], "comparison")],
              "growth_plan": None}
    _refresh(session, legacy)
    [old] = _rows(session)
    assert old.opportunity_key.endswith(":angle=comparison")
    out = _refresh(session, current, hours=1)
    [item] = out["plan"]["items"]
    assert (item["decision"], item["existing_id"]) == (gas.REOBSERVED, old.id)
    rows = _rows(session)
    assert len(rows) == 1 and rows[0].opportunity_key == old.opportunity_key  # 書き換えない
    assert rows[0].seen_count == 2


def test_legacy_decisions_keep_suppressing(session) -> None:
    current = _report()
    _refresh(session, {"candidates": [_legacy(current["candidates"][0], "comparison")],
                       "growth_plan": None})
    [old] = _rows(session)
    reviews = gas.GrowthActionReviewService(session)
    # 一覧・まとめで見た今の形の指紋でも、古い行のレビューを依頼できる (同じ候補)。
    review = reviews.request_review(
        old.id, now=_NOW,
        expected_candidate_fingerprint=current["candidates"][0]["candidate_fingerprint"])
    reviews.reject(review.id, expected_candidate_fingerprint=old.candidate_fingerprint,
                   reason="no", now=_NOW)
    out = _refresh(session, current, hours=1)
    assert out["plan"]["items"][0]["decision"] == gas.SUPPRESSED_REJECTED
    old.status = GA_CONVERTED
    session.commit()
    out = _refresh(session, current, hours=2)
    assert out["plan"]["items"][0]["decision"] == gas.SUPPRESSED_COMPLETED
    assert len(_rows(session)) == 1


def test_a_pending_legacy_review_still_blocks_a_second_review(session) -> None:
    current = _report()
    _refresh(session, {"candidates": [_legacy(current["candidates"][0], "comparison")],
                       "growth_plan": None})
    [old] = _rows(session)
    gas.GrowthActionReviewService(session).request_review(old.id, now=_NOW)
    coverage = gas.collect_coverage(session, now=_NOW, report={"growth_plan": None},
                                    include_stock_plan=False)  # fmt: skip
    assert current["candidates"][0]["opportunity_key"] in coverage.open_reviews
    out = _refresh(session, current, hours=1)
    assert out["plan"]["items"][0]["decision"] == gas.REOBSERVED_PENDING


def test_two_live_legacy_variants_collapse_to_one(session) -> None:
    current = _report()
    base = current["candidates"][0]
    other = _legacy(_report(ranks=(0.8,))["candidates"][0], "question")  # 別の証拠・別の切り口
    _refresh(session, {"candidates": [_legacy(base, "comparison"), other], "growth_plan": None})
    assert len(_live(session)) == 2  # C9-A より前の状態 (1 記事に 2 つ)
    out = _refresh(session, current, hours=1)
    assert out["plan"]["counts"]["not_observed"] == 1
    assert [r.opportunity_key.split(":angle=")[1] for r in _live(session)] == ["comparison"]
    assert len(_rows(session)) == 2  # 消さない


def test_evidence_returning_to_an_older_revision_keeps_the_current_row_live(session) -> None:
    _refresh(session, _report(ranks=(0.2,)))
    _refresh(session, _report(ranks=(0.8,)), hours=1)
    [_first, second] = _rows(session)
    out = _refresh(session, _report(ranks=(0.2,)), hours=2)  # 前の版の証拠に戻った
    assert out["plan"]["items"][0]["decision"] == gas.SUPPRESSED_SUPERSEDED
    assert [r.id for r in _live(session)] == [second.id]  # 生きている行は残る
    assert len(_rows(session)) == 2
