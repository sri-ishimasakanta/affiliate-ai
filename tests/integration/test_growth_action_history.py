"""Growth Action の履歴・重複の抑え・人のレビュー (C9 Batch 2)。

pin する契約:

- 同じ候補 (同じ指紋) を何度評価しても行は増えない (再観測は last_seen と回数だけ)。
- 証拠が変われば新しい版。古い版は superseded (上書きしない)。
- 却下した候補は、証拠が変わらない限り出てこない。変われば新しい版で出てくる。
- 同じ機会に開いているレビューがあれば、2 つ目のレビューを作らない。
- 承認は固定した指紋が合い、最新の版で、いまも観測されているときだけ。合わなければ stale にして
  承認しない (fail closed)。同じ決定の繰り返しは冪等。
- 承認・却下は C9 の表だけを書く (ほかの表・外のサービスには触れない)。
- PLAN (execute なし) は何も書かない。履歴の表が無い DB では書かずに断る。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from app.growth import analysis as ga
from app.growth import inbox as gi
from app.models import ThreadsPostProposal
from app.models.growth_action import (
    GA_ACTIVE,
    GA_APPROVED,
    GA_PENDING_REVIEW,
    GA_REJECTED,
    GA_SUPERSEDED,
    GAR_STALE,
    GrowthActionCandidate,
    GrowthActionEvent,
    GrowthActionReview,
)
from app.services import growth_action_service as gas

_NOW = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_external(monkeypatch):
    import httpx

    from app.social.threads import service as threads_service

    def refuse(*_a, **_k):
        raise AssertionError("growth actions must not call anything external")

    monkeypatch.setattr(httpx.Client, "send", refuse)
    monkeypatch.setattr(threads_service.ThreadsService, "__init__", refuse)


def _src(source, state=ga.USABLE, **metrics):
    return ga.SourceEvidence(source, state, "test", "test", metrics=metrics)


def _report(*, clean=3, articles=(1,)) -> dict:
    candidates = []
    for aid in articles:
        sources = {s: _src(s, ga.UNAVAILABLE) for s in ga.SOURCES}
        sources["affiliate"] = _src("affiliate", ga.INSUFFICIENT, clean_clicks=clean)
        sources["threads"] = _src("threads", ga.USABLE, publications=1, latest_age_hours=90,
                                  oldest_age_hours=90, angles_tried=["insight"],
                                  reach_percentile_ranks=[0.2], open_proposals=[])
        evidence = ga.GrowthEvidence(subject_type="article", subject_id=f"article:{aid}",
                                     article_id=aid, article={"status": "published",
                                                              "age_days": 20,
                                                              "monetization_mode": "affiliate",
                                                              "monetized": True},
                                     sources=sources)  # fmt: skip
        candidates += ga.build_candidates(evidence, ga.classify(
            evidence, angles=("insight", "question")))  # fmt: skip
    return {"candidates": [c.as_dict() for c in ga.sort_candidates(candidates)],
            "growth_plan": None}


def _refresh(session, report, *, now=_NOW, coverage=None, execute=True):
    return gas.GrowthActionHistory(session).refresh(
        report, coverage or gi.CoverageContext(), now=now, execute=execute)


def _rows(session):
    return list(session.scalars(select(GrowthActionCandidate).order_by(GrowthActionCandidate.id)))


def _placement(session):
    return next(r for r in _rows(session) if r.action_type == ga.REVIEW_AFFILIATE_PLACEMENT
                and r.status != GA_SUPERSEDED)  # fmt: skip


def _tables(session) -> dict:
    names = [r[0] for r in session.execute(text(
        "select name from sqlite_master where type='table'"))]  # fmt: skip
    return {n: session.execute(text(f'select count(*) from "{n}"')).scalar() for n in names
            if not n.startswith("growth_action_")}


# == 履歴 ================================
def test_plan_writes_nothing(session) -> None:
    out = _refresh(session, _report(), execute=False)
    assert out["executed"] is False and out["plan"]["counts"]["decisions"] == {"new": 2}
    assert _rows(session) == []


def test_exact_duplicates_are_reobserved_not_duplicated(session) -> None:
    _refresh(session, _report())
    first = {r.id: r.seen_count for r in _rows(session)}
    out = _refresh(session, _report(), now=_NOW + timedelta(days=1))
    assert out["plan"]["counts"]["decisions"] == {gas.REOBSERVED: 2}
    assert out["plan"]["counts"]["surfaced"] == 0  # 同じ候補を毎日知らせない
    rows = _rows(session)
    assert {r.id for r in rows} == set(first)
    assert all(r.seen_count == 2 for r in rows)
    assert all(r.last_seen_at.replace(tzinfo=UTC) == _NOW + timedelta(days=1) for r in rows)


def test_changed_evidence_creates_a_new_revision_and_supersedes(session) -> None:
    _refresh(session, _report(clean=3))
    old = _placement(session)
    out = _refresh(session, _report(clean=5), now=_NOW + timedelta(days=1))
    assert out["plan"]["counts"]["decisions"] == {gas.NEW_REVISION: 1, gas.REOBSERVED: 1}
    session.refresh(old)
    new = _placement(session)
    assert (old.status, old.superseded_by_id) == (GA_SUPERSEDED, new.id)
    assert (new.revision, new.opportunity_key) == (2, old.opportunity_key)
    assert old.snapshot_json["evidence"][0]["evidence"]["clean_clicks"] == 3  # 上書きしない
    history = gas.GrowthActionHistory(session).history(new.id)
    assert [r["revision"] for r in history["revisions"]] == [1, 2]
    assert "superseded" in [e["event_type"] for e in history["events"]]


def test_rejected_unchanged_stays_hidden_changed_resurfaces(session) -> None:
    _refresh(session, _report(clean=3))
    row = _placement(session)
    reviews = gas.GrowthActionReviewService(session)
    review = reviews.request_review(row.id, now=_NOW)
    reviews.reject(review.id, expected_candidate_fingerprint=row.candidate_fingerprint,
                   reason="not now", now=_NOW)
    same = _refresh(session, _report(clean=3), now=_NOW + timedelta(days=1))
    decisions = {i["action_type"]: i["decision"] for i in same["plan"]["items"]}
    assert decisions[ga.REVIEW_AFFILIATE_PLACEMENT] == gas.SUPPRESSED_REJECTED
    session.refresh(row)
    assert row.status == GA_REJECTED  # 再観測で状態を戻さない
    changed = _refresh(session, _report(clean=7), now=_NOW + timedelta(days=2))
    item = next(i for i in changed["plan"]["items"]
                if i["action_type"] == ga.REVIEW_AFFILIATE_PLACEMENT)  # fmt: skip
    assert item["decision"] == gas.NEW_REVISION and item["surfaced"] is True
    assert _placement(session).status == GA_ACTIVE


def test_candidates_no_longer_produced_are_marked(session) -> None:
    _refresh(session, _report(articles=(1, 2)))
    out = _refresh(session, _report(articles=(1,)), now=_NOW + timedelta(days=1))
    assert out["plan"]["counts"]["not_observed"] == 2
    gone = [r for r in _rows(session) if r.article_id == 2]
    assert {r.availability for r in gone} == {gas.NOT_OBSERVED}


# == レビュー ================================
def test_review_approve_is_permission_only_and_idempotent(session) -> None:
    _refresh(session, _report())
    row = _placement(session)
    before = _tables(session)
    reviews = gas.GrowthActionReviewService(session)
    review = reviews.request_review(row.id, now=_NOW)
    assert reviews.request_review(row.id, now=_NOW).id == review.id  # 2 つ目は作らない
    assert review.snapshot_json["candidate_fingerprint"] == row.candidate_fingerprint
    assert review.snapshot_json["conversion"]["support"] == "manual_only"
    assert "executes nothing" in review.snapshot_json["approval_meaning"]
    approved = reviews.approve(review.id, expected_candidate_fingerprint=row.candidate_fingerprint,
                               now=_NOW)
    events = session.scalar(select(func.count()).select_from(GrowthActionEvent))
    again = reviews.approve(review.id, expected_candidate_fingerprint=row.candidate_fingerprint,
                            now=_NOW)
    assert approved.id == again.id and approved.status == "approved"
    assert session.scalar(select(func.count()).select_from(GrowthActionEvent)) == events
    session.refresh(row)
    assert row.status == GA_APPROVED
    assert _tables(session) == before  # WordPress・Threads・メール・変更の依頼は 0 件
    with pytest.raises(gas.GrowthActionError, match="already approved"):
        reviews.reject(review.id, expected_candidate_fingerprint=row.candidate_fingerprint,
                       reason="changed my mind", now=_NOW)
    # 承認済みで同じ証拠なら、次の評価で出てこない。
    out = _refresh(session, _report(), now=_NOW + timedelta(days=1))
    assert gas.SUPPRESSED_COMPLETED in {i["decision"] for i in out["plan"]["items"]}


def test_a_wrong_fingerprint_fails_closed(session) -> None:
    _refresh(session, _report())
    row = _placement(session)
    reviews = gas.GrowthActionReviewService(session)
    review = reviews.request_review(row.id, now=_NOW)
    with pytest.raises(gas.GrowthActionError, match="does not match"):
        reviews.approve(review.id, expected_candidate_fingerprint="0" * 64, now=_NOW)
    session.refresh(review)
    session.refresh(row)
    assert review.status == GAR_STALE and row.status == GA_PENDING_REVIEW


def test_a_superseded_candidate_cannot_be_approved(session) -> None:
    _refresh(session, _report(clean=3))
    row = _placement(session)
    reviews = gas.GrowthActionReviewService(session)
    review = reviews.request_review(row.id, now=_NOW)
    fp = row.candidate_fingerprint
    _refresh(session, _report(clean=9), now=_NOW + timedelta(hours=2))  # 新しい証拠
    session.refresh(review)
    assert review.status == GAR_STALE  # 照らし合わせの時点で stale に
    with pytest.raises(gas.GrowthActionError, match="already stale"):
        reviews.approve(review.id, expected_candidate_fingerprint=fp, now=_NOW)
    new = _placement(session)
    assert new.status == GA_ACTIVE
    # 新しい版には新しいレビューを出せる (開いているレビューは無い)。
    assert reviews.request_review(new.id, now=_NOW).candidate_id == new.id


def test_a_pending_review_blocks_a_second_review_of_the_opportunity(session) -> None:
    _refresh(session, _report())
    row = _placement(session)
    reviews = gas.GrowthActionReviewService(session)
    reviews.request_review(row.id, now=_NOW)
    covered = gas.collect_coverage(session, now=_NOW, report={"growth_plan": None},
                                   include_stock_plan=False)  # fmt: skip
    assert covered.open_reviews == {row.opportunity_key: 1}
    entry = next(c for c in _report()["candidates"]
                 if c["action_type"] == ga.REVIEW_AFFILIATE_PLACEMENT)  # fmt: skip
    assert gi.assess(entry, covered)[0] == gi.COVERED


def test_only_active_actionable_candidates_can_be_reviewed(session) -> None:
    coverage = gi.CoverageContext(open_regular_proposals={1: ({"id": 5, "status": "proposed"},)})
    _refresh(session, _report(), coverage=coverage)
    alt = next(r for r in _rows(session)
               if r.action_type == ga.CREATE_THREADS_ALTERNATIVE_ANGLE)  # fmt: skip
    assert alt.availability == gi.COVERED
    with pytest.raises(gas.GrowthActionError, match="only active, actionable"):
        gas.GrowthActionReviewService(session).request_review(alt.id, now=_NOW)


def test_dismiss_needs_a_reason(session) -> None:
    _refresh(session, _report())
    row = _placement(session)
    reviews = gas.GrowthActionReviewService(session)
    with pytest.raises(gas.GrowthActionError, match="needs a reason"):
        reviews.dismiss(row.id, reason=" ")
    assert reviews.dismiss(row.id, reason="duplicate of manual work").status == "dismissed"


def test_missing_history_tables_refuse_to_write(session, monkeypatch) -> None:
    monkeypatch.setattr(gas, "history_tables_ready", lambda _s: False)
    out = _refresh(session, _report(), execute=False)
    assert out["plan"]["tables_ready"] is False
    with pytest.raises(gas.GrowthActionError, match="74bfaf6c9c9f"):
        _refresh(session, _report(), execute=True)
    assert session.scalar(select(func.count()).select_from(GrowthActionReview)) == 0


# == 既存の仕事 (DB) ================================
def test_coverage_reads_regular_proposals_but_not_growth(session) -> None:
    from app.article.draft_promotion_canonical import compute_text_hash
    from app.models import Article

    session.add(Article(id=1, title="a", slug="a", body="本文", status="published",
                        published_url="https://x.test/a/", published_at=_NOW - timedelta(days=9)))
    session.commit()
    for seed, aid, status in (("r", 1, "awaiting_approval"), ("g", None, "awaiting_approval")):
        session.add(ThreadsPostProposal(
            source_article_id=aid, source_article_body_hash=compute_text_hash("本文"),
            angle="insight" if aid else "account_growth", link_mode="none",
            content_text=f"本文 {seed}", character_count=4, content_seed=seed * 64,
            proposal_hash=seed.upper() * 64, policy_version="t", generator_version="g",
            status=status,
            learning_guidance_json=None if aid else {"content_kind": "account_growth"}))
    session.commit()
    coverage = gas.collect_coverage(session, now=_NOW, report={"growth_plan": None},
                                    include_stock_plan=False)  # fmt: skip
    assert list(coverage.open_regular_proposals) == [1]
    assert coverage.open_regular_proposals[1][0]["status"] == "awaiting_approval"


# == CLI ================================
def _scoped(session):
    class _Scoped:
        def __call__(self):
            return self

        def __enter__(self):
            return session

        def __exit__(self, *_exc):
            return False

    return _Scoped()


def test_the_inbox_cli_is_read_only_by_default(session, capsys, monkeypatch) -> None:
    from scripts import manage_growth_actions as cli

    monkeypatch.setattr(cli, "build_inbox", lambda s, **kw: _box(s, kw["now"]))
    kw = {"session_factory": _scoped(session), "settings": object()}
    assert cli.main(["--as-of", _NOW.isoformat(), "list"], **kw) == 0
    out = capsys.readouterr().out
    assert "review_affiliate_placement (1)" in out and "WordPress writes = 0" in out
    assert cli.main(["refresh"], **kw) == 0
    assert "refresh PLAN" in capsys.readouterr().out and _rows(session) == []
    assert cli.main(["refresh", "--execute"], **kw) == 0
    assert len(_rows(session)) == 2
    row = _placement(session)
    assert cli.main(["review", str(row.id)], **kw) == 0
    assert "PLAN: review would write" in capsys.readouterr().out
    assert session.scalar(select(func.count()).select_from(GrowthActionReview)) == 0
    assert cli.main(["review", str(row.id), "--execute"], **kw) == 0
    review = session.scalars(select(GrowthActionReview)).one()
    assert cli.main(["approve", str(review.id), "--fingerprint", "bad", "--execute"], **kw) == 2
    assert "refused: review is stale" in capsys.readouterr().out
    assert cli.main(["--format", "json", "history", str(row.id)], **kw) == 0
    payload = json.loads(capsys.readouterr().out.split("side effects:")[0])
    assert payload["reviews"][0]["status"] == GAR_STALE


def _box(session, now):
    report = _report()
    coverage = gi.CoverageContext()
    history = gas.GrowthActionHistory(session)
    plan = history.plan_refresh(report, coverage, now=now or _NOW)
    rows = {r.id: r for r in history.rows()}
    return {"as_of": (now or _NOW).isoformat(), "report": report, "coverage": coverage,
            "plan": plan, "entries": gas.plan_entries(report, plan, rows),
            "history_source": "history"}


def test_the_digest_plan_sends_nothing(session, capsys, monkeypatch) -> None:
    from scripts import plan_growth_action_digest as cli

    monkeypatch.setattr(cli, "build_inbox", lambda s, **kw: _box(s, kw["now"]))
    before = _tables(session)
    assert cli.main(["--as-of", _NOW.isoformat()], session_factory=_scoped(session),
                    settings=object()) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert "digest PLAN (not sent)" in out and "emails = 0" in out
    assert "review_affiliate_placement article:1" in out
    assert _tables(session) == before and _rows(session) == []
