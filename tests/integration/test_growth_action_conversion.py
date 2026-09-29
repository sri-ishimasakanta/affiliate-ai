"""承認した Growth Action を既存の流れへ渡す・その先を観測する (C9 Batch 3)。

pin する契約:

- PLAN は何も書かない。実行は、承認済み・固定した指紋が合う・最新の版・いまも観測され動ける・
  止める理由が同じ・ほかの変更の依頼が無い、のときだけ。どれかが違えば断り、理由を残す。
- 内部リンクの見直し → ChangeRequest (awaiting_approval) を作るところまで。ChangeRequest の
  承認・WordPress への適用は 0 件。Growth Action の承認を ChangeRequest の承認に使わない。
- 同じ変換を 2 回しても依頼は 1 つ (前の結果を返す)。
- ほかの行動は計画だけ・対応なしとして断る (架空の依頼を作らない)。
- 変換 ≠ 適用。effective_at は実際の適用の時刻だけ (承認・変換の時刻ではない)。
- 変換した候補は、同じ証拠なら再び出てこない。新しい証拠は新しい版 (生きている版は 1 つ)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from app.growth import analysis as ga
from app.growth import inbox as gi
from app.models import (
    CR_APPROVED,
    CR_AWAITING_APPROVAL,
    ChangeApplication,
    ChangeRequest,
    ChangeRequestApproval,
    SeoImprovementRun,
)
from app.models.growth_action import (
    GA_CONVERTED,
    GrowthActionCandidate,
    GrowthActionConversion,
    GrowthActionEvent,
)
from app.services import growth_action_conversion_service as gcs
from app.services import growth_action_service as gas
from app.services.change_request_service import ChangeRequestService
from tests.integration.test_change_request_service import _SOURCE_BODY, _article, _candidate

_NOW = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)


class _Settings:
    wordpress_base_url = "https://example.com"
    search_console_property_uri = "sc-domain:example.com"
    search_console_credentials_file = None
    ga4_property_id = None


@pytest.fixture(autouse=True)
def _no_external(monkeypatch):
    import httpx

    from app.services import threads_openai_provider
    from app.social.threads import service as threads_service

    def refuse(*_a, **_k):
        raise AssertionError("conversion must not call anything external")

    monkeypatch.setattr(httpx.Client, "send", refuse)
    monkeypatch.setattr(threads_service.ThreadsService, "__init__", refuse)
    monkeypatch.setattr(threads_openai_provider.OpenAIResponsesClient, "__init__", refuse)


def _src(source, state=ga.USABLE, **metrics):
    return ga.SourceEvidence(source, state, "test", "test", metrics=metrics)


def _report(*, priority="low", source=18, target=17) -> dict:
    sources = {s: _src(s, ga.UNAVAILABLE) for s in ga.SOURCES}
    sources["threads"] = _src("threads", ga.USABLE, publications=1, latest_age_hours=90,
                              oldest_age_hours=90, angles_tried=["insight"],
                              reach_percentile_ranks=[0.2], open_proposals=[])
    sources["affiliate"] = _src("affiliate", ga.INSUFFICIENT, clean_clicks=2)
    evidence = ga.GrowthEvidence(
        subject_type="article", subject_id=f"article:{source}", article_id=source,
        article={"status": "published", "age_days": 30, "monetization_mode": "supporting"},
        sources=sources,
        existing_candidates={"seo": ({"candidate_type": "INTERNAL_LINK_OPPORTUNITY",
                                      "reason_code": "DEFERRED_PAIR", "priority": priority,
                                      "evidence": {"target_article_id": target,
                                                   "relation": "deferred pair"}},)})
    candidates = ga.build_candidates(evidence, ga.classify(
        evidence, angles=("insight", "question")))  # fmt: skip
    return {"candidates": [c.as_dict() for c in ga.sort_candidates(candidates)],
            "growth_plan": None}  # fmt: skip


@pytest.fixture
def approved(session):
    """記事 18 → 17 の内部リンクの Growth Action (承認済み) と、保存済みの C6 の候補。"""

    _article(session, 18, "rpa-implementation", body=_SOURCE_BODY)
    _article(session, 17, "rpa-comparison", body="# 比較\n\n本文\n")
    seo = _candidate(session, article_id=18, target_article_id=17)
    gas.GrowthActionHistory(session).refresh(_report(), gi.CoverageContext(), now=_NOW,
                                             execute=True)
    row = _row(session, ga.REVIEW_INTERNAL_LINKS)
    reviews = gas.GrowthActionReviewService(session)
    review = reviews.request_review(row.id, now=_NOW)
    reviews.approve(review.id, expected_candidate_fingerprint=row.candidate_fingerprint, now=_NOW)
    return {"row": row, "review": review, "seo": seo}


def _row(session, action):
    return next(r for r in session.scalars(select(GrowthActionCandidate)
                                           .order_by(GrowthActionCandidate.id.desc()))
                if r.action_type == action)


def _builder(**override):
    """いまの評価の代役 (候補の現在の availability・止める理由を差し替えられる)。"""

    def build(session, **_kw):
        entries = []
        for c in _report()["candidates"]:
            entries.append({**c, "availability": gi.ACTIONABLE_NOW, "availability_reasons": [],
                            **override.get(c["action_type"], {})})
        return {"entries": entries}

    return build


def _service(session, **override):
    return gcs.GrowthActionConversionService(session, settings=_Settings(),
                                             inbox_builder=_builder(**override))


def _counts(session) -> dict:
    names = [r[0] for r in session.execute(text(
        "select name from sqlite_master where type='table'"))]  # fmt: skip
    return {n: session.execute(text(f'select count(*) from "{n}"')).scalar() for n in names}


def _refused(session, service, row_id, match):
    with pytest.raises(gas.GrowthActionError, match=match):
        service.execute(row_id, now=_NOW)
    assert session.scalar(select(func.count()).select_from(ChangeRequest)) == 0
    events = [e.event_type for e in session.scalars(select(GrowthActionEvent))]
    assert "conversion_refused" in events


# == PLAN ================================
def test_the_plan_writes_nothing_and_is_frozen(session, approved) -> None:
    before = _counts(session)
    result = _service(session).plan(approved["row"].id, now=_NOW)
    assert _counts(session) == before
    plan = result["plan"]
    assert result["executable"] is True and plan["supported"] is True
    assert plan["target_subject"]["internal_link_targets"] == [17]
    assert plan["expected_external_writes"] == []
    assert plan["candidate_fingerprint"] == approved["row"].candidate_fingerprint
    assert plan["review_id"] == approved["review"].id
    assert plan["idempotency_key"].startswith("gac1:")
    again = _service(session).plan(approved["row"].id, now=_NOW)["plan"]
    assert again["plan_hash"] == plan["plan_hash"]  # 決定的


# == 断る ================================
def test_an_unapproved_candidate_is_refused(session) -> None:
    _article(session, 18, "rpa-implementation", body=_SOURCE_BODY)
    _article(session, 17, "rpa-comparison", body="# 比較\n\n本文\n")
    gas.GrowthActionHistory(session).refresh(_report(), gi.CoverageContext(), now=_NOW,
                                             execute=True)
    row = _row(session, ga.REVIEW_INTERNAL_LINKS)
    _refused(session, _service(session), row.id, "approved_review")
    gas.GrowthActionReviewService(session).request_review(row.id, now=_NOW)  # pending
    _refused(session, _service(session), row.id, "approved_review: review pending")


def test_a_rejected_review_is_refused(session) -> None:
    _article(session, 18, "rpa-implementation", body=_SOURCE_BODY)
    _article(session, 17, "rpa-comparison", body="# 比較\n\n本文\n")
    gas.GrowthActionHistory(session).refresh(_report(), gi.CoverageContext(), now=_NOW,
                                             execute=True)
    row = _row(session, ga.REVIEW_INTERNAL_LINKS)
    reviews = gas.GrowthActionReviewService(session)
    review = reviews.request_review(row.id, now=_NOW)
    reviews.reject(review.id, expected_candidate_fingerprint=row.candidate_fingerprint,
                   reason="no", now=_NOW)
    _refused(session, _service(session), row.id, "review rejected")


def test_a_superseded_candidate_is_refused(session, approved) -> None:
    gas.GrowthActionHistory(session).refresh(_report(priority="high"), gi.CoverageContext(),
                                             now=_NOW + timedelta(hours=1), execute=True)
    _refused(session, _service(session), approved["row"].id, "current_revision")


def test_a_tampered_review_is_refused(session, approved) -> None:
    approved["review"].candidate_fingerprint = "0" * 64
    session.commit()
    _refused(session, _service(session), approved["row"].id, "review_fingerprint_match")


def test_changed_blockers_or_availability_are_refused(session, approved) -> None:
    changed = {ga.REVIEW_INTERNAL_LINKS: {"blockers": ["seo data is stale"]}}
    _refused(session, _service(session, **changed), approved["row"].id, "blockers_unchanged")
    covered = {ga.REVIEW_INTERNAL_LINKS: {"availability": gi.COVERED,
                                          "availability_reasons": ["x"]}}
    _refused(session, _service(session, **covered), approved["row"].id, "still_actionable")


def test_a_candidate_no_longer_observed_is_refused(session, approved) -> None:
    service = gcs.GrowthActionConversionService(
        session, settings=_Settings(), inbox_builder=lambda *_a, **_k: {"entries": []})
    _refused(session, service, approved["row"].id, "still_observed")


def test_conflicting_downstream_work_is_refused(session, approved) -> None:
    other = ChangeRequestService(session).propose_from_seo_candidate(
        candidate_id=approved["seo"].id, idempotency_key="someone-else", now=_NOW)
    with pytest.raises(gas.GrowthActionError, match="no_conflicting_downstream_work"):
        _service(session).execute(approved["row"].id, now=_NOW)
    assert session.scalar(select(func.count()).select_from(ChangeRequest)) == 1
    assert session.get(ChangeRequest, other.id).idempotency_key == "someone-else"


def test_unsupported_actions_are_refused_explicitly(session, approved) -> None:
    row = _row(session, ga.CREATE_THREADS_ALTERNATIVE_ANGLE)
    result = _service(session).plan(row.id, now=_NOW)
    assert result["plan"]["execution_mode"] == "unsupported"
    assert "GenerationRequest adapter" in result["plan"]["missing"]
    _refused(session, _service(session), row.id, "supported: execution mode unsupported")


# == 実行 ================================
def test_execute_hands_off_a_change_request_only(session, approved) -> None:
    result = _service(session).execute(approved["row"].id, now=_NOW)
    assert result["executed"] is True
    [cr_id] = result["conversion"]["downstream_ids"]
    request = session.get(ChangeRequest, cr_id)
    assert request.status == CR_AWAITING_APPROVAL and request.source_engine == "seo"
    assert request.idempotency_key.startswith(result["plan"]["idempotency_key"])
    # ChangeRequest の承認・適用は 0 件 (その流れの人の判断)。
    assert session.scalar(select(func.count()).select_from(ChangeRequestApproval)) == 0
    assert session.scalar(select(func.count()).select_from(ChangeApplication)) == 0
    row = session.get(GrowthActionCandidate, approved["row"].id)
    assert row.status == GA_CONVERTED
    conversion = session.scalars(select(GrowthActionConversion)).one()
    assert (conversion.candidate_id, conversion.review_id, conversion.status) == (
        row.id, approved["review"].id, "created")  # fmt: skip
    assert conversion.candidate_fingerprint == row.candidate_fingerprint
    assert result["side_effects"]["external_writes"] == 0
    assert result["side_effects"]["wordpress_writes"] == 0


def test_execute_twice_creates_one_request(session, approved) -> None:
    first = _service(session).execute(approved["row"].id, now=_NOW)
    second = _service(session).execute(approved["row"].id, now=_NOW + timedelta(minutes=5))
    assert second["idempotent_replay"] is True and second["executed"] is False
    assert second["conversion"]["id"] == first["conversion"]["id"]
    assert session.scalar(select(func.count()).select_from(ChangeRequest)) == 1
    assert session.scalar(select(func.count()).select_from(GrowthActionConversion)) == 1


def test_a_missing_c6_candidate_that_c6_no_longer_proposes_fails_cleanly(session) -> None:
    # 90 → 91 は content portfolio に無い組 (C6 はこのリンクを提案しない)。保存済みの候補も無い。
    _article(session, 90, "not-in-portfolio-a", body=_SOURCE_BODY)
    _article(session, 91, "not-in-portfolio-b", body="# 比較\n\n本文\n")
    report = _report(source=90, target=91)
    gas.GrowthActionHistory(session).refresh(report, gi.CoverageContext(), now=_NOW,
                                             execute=True)
    row = _row(session, ga.REVIEW_INTERNAL_LINKS)
    reviews = gas.GrowthActionReviewService(session)
    review = reviews.request_review(row.id, now=_NOW)
    reviews.approve(review.id, expected_candidate_fingerprint=row.candidate_fingerprint, now=_NOW)
    service = gcs.GrowthActionConversionService(session, settings=_Settings(), inbox_builder=(
        lambda *_a, **_k: {"entries": [{**c, "availability": gi.ACTIONABLE_NOW,
                                        "availability_reasons": []}
                                       for c in report["candidates"]]}))
    with pytest.raises(gas.GrowthActionError, match="no longer proposes"):
        service.execute(row.id, now=_NOW)
    assert session.scalar(select(func.count()).select_from(SeoImprovementRun)) == 0
    assert session.scalar(select(func.count()).select_from(ChangeRequest)) == 0
    events = [e.event_type for e in session.scalars(select(GrowthActionEvent))]
    assert "conversion_failed" in events
    assert session.get(GrowthActionCandidate, row.id).status == "approved"


def test_the_conversion_table_is_required_to_execute(session, approved, monkeypatch) -> None:
    monkeypatch.setattr(gcs, "conversions_ready", lambda _s: False)
    with pytest.raises(gas.GrowthActionError, match="74dbecaa4bb2"):
        _service(session).execute(approved["row"].id, now=_NOW)
    assert _service(session).plan(approved["row"].id, now=_NOW)["plan"]["supported"] is True


# == その先の観測・効果 ================================
def test_downstream_lifecycle_and_effective_at(session, approved) -> None:
    service = _service(session)
    [cr_id] = service.execute(approved["row"].id, now=_NOW)["conversion"]["downstream_ids"]
    conversion = session.scalars(select(GrowthActionConversion)).one()
    [pending] = service.downstream(conversion)
    assert (pending["state"], pending["effective_at"]) == (CR_AWAITING_APPROVAL, None)
    request = session.get(ChangeRequest, cr_id)
    ChangeRequestService(session).approve(cr_id, proposal_hash=request.proposal_hash,
                                          now=_NOW + timedelta(hours=1))
    [approved_state] = service.downstream(conversion)
    assert approved_state["state"] == CR_APPROVED and approved_state["effective_at"] is None
    applied_at = _NOW + timedelta(days=2)
    session.add(ChangeApplication(
        change_request_id=cr_id, article_id=18, proposal_hash=request.proposal_hash,
        proposal_version=1, source_body_hash="b" * 64, proposed_body_hash="c" * 64,
        pre_change_body="本文", wordpress_post_id="1018", outcome="succeeded",
        attempted_at=applied_at, finished_at=applied_at))
    session.commit()
    [applied] = service.downstream(conversion)
    assert applied["effective_at"] == applied_at.isoformat()  # 承認・変換の時刻ではない
    # Growth Action の状態は converted のまま (converted ≠ applied)。
    assert session.get(GrowthActionCandidate, approved["row"].id).status == GA_CONVERTED
    outcomes = gcs.GrowthActionOutcomeService(session, settings=_Settings())
    [anchor] = outcomes.anchors()
    assert anchor.effective_at == applied_at.isoformat()
    early = outcomes.outcome(anchor, now=applied_at + timedelta(hours=6)).as_dict()
    assert [c["name"] for c in early["checkpoints"]] == ["24h", "72h", "7d", "14d", "28d"]
    assert {c["state"] for c in early["checkpoints"]} == {"waiting"}  # 窓・GSC がまだ
    assert early["score"] is None and early["causal_claim"] == "none"


def test_before_application_everything_waits(session, approved) -> None:
    _service(session).execute(approved["row"].id, now=_NOW)
    outcomes = gcs.GrowthActionOutcomeService(session, settings=_Settings())
    [anchor] = outcomes.anchors()
    result = outcomes.outcome(anchor, now=_NOW + timedelta(days=40)).as_dict()
    assert anchor.effective_at is None
    assert {c["state"] for c in result["checkpoints"]} == {"waiting"}
    assert all("not applied yet" in c["reasons"][0] for c in result["checkpoints"])


# == 閉じた輪 ================================
def test_a_converted_candidate_does_not_resurface(session, approved) -> None:
    _service(session).execute(approved["row"].id, now=_NOW)
    history = gas.GrowthActionHistory(session)
    same = history.refresh(_report(), gi.CoverageContext(), now=_NOW + timedelta(days=1),
                           execute=True)
    item = next(i for i in same["plan"]["items"] if i["action_type"] == ga.REVIEW_INTERNAL_LINKS)
    assert item["decision"] == gas.SUPPRESSED_COMPLETED and item["surfaced"] is False
    # 新しい証拠 → 新しい版。変換済みの版は converted のまま (つながりは superseded_by_id)。
    changed = history.refresh(_report(priority="high"), gi.CoverageContext(),
                              now=_NOW + timedelta(days=2), execute=True)
    item = next(i for i in changed["plan"]["items"]
                if i["action_type"] == ga.REVIEW_INTERNAL_LINKS)  # fmt: skip
    assert item["decision"] == gas.NEW_REVISION
    old = session.get(GrowthActionCandidate, approved["row"].id)
    assert old.status == GA_CONVERTED and old.superseded_by_id is not None
    live = [r for r in session.scalars(select(GrowthActionCandidate))
            if r.opportunity_key == old.opportunity_key and r.status not in ("superseded",
                                                                             "converted")]
    assert len(live) == 1
    # 変換済みは受け箱の既定・まとめに出ない。
    entries = [{"status": GA_CONVERTED, "decision": gas.SUPPRESSED_COMPLETED,
                "availability": gi.ACTIONABLE_NOW}]
    assert gas.filter_entries(entries) == []


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


def test_the_cli_is_plan_by_default(session, approved, capsys, monkeypatch) -> None:
    from scripts import analyze_growth_action_outcomes as outcomes_cli
    from scripts import convert_growth_action as cli

    monkeypatch.setattr(gcs, "build_inbox", _builder())
    kw = {"session_factory": _scoped(session), "settings": _Settings()}
    row_id = str(approved["row"].id)
    assert cli.main([row_id, "--as-of", _NOW.isoformat()], **kw) == 0
    out = capsys.readouterr().out
    assert "conversion PLAN" in out and "re-run with --execute" in out
    assert session.scalar(select(func.count()).select_from(ChangeRequest)) == 0
    assert cli.main([row_id, "--execute", "--as-of", _NOW.isoformat()], **kw) == 0
    assert "conversion EXECUTED" in capsys.readouterr().out
    assert cli.main([row_id, "--execute", "--as-of", _NOW.isoformat()], **kw) == 0
    assert "ALREADY CONVERTED" in capsys.readouterr().out
    assert session.scalar(select(func.count()).select_from(ChangeRequest)) == 1
    assert outcomes_cli.main(["list", "--as-of", _NOW.isoformat()], **kw) == 0
    out = capsys.readouterr().out
    assert "effective_at — (not applied)" in out and "no success score" in out


def test_the_worker_never_converts() -> None:
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2]
              / "app/services/threads_worker_service.py").read_text(encoding="utf-8")
    assert "GrowthActionConversionService" not in source
    assert "convert_growth_action" not in source
