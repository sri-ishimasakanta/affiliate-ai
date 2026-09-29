# ruff: noqa: F811 - fixtures imported from other modules are used as parameters
"""変換した Growth Action の追跡の観測 (C9-C、DB・偽の出所の届き方)。

pin する契約:

- 効果の始まり: 変更の依頼は適用の成功の時刻、Threads は公開の時刻、記事は公開の時刻。承認・
  変換・依頼の作成・提案の承認・準備・記事の作成・下書きは使わない。
- チェックポイント (24h / 72h / 7d / 14d / 28d): 期日の前は待つ。出所の届き方 (GSC の遅れ・GA4 の
  data-through) を守る。目安を過ぎて届かなければ古い。欠測は 0 にしない。観測の時点より後の
  データは使わない。信頼できるクリックだけ。
- 同じ観測をもう一度しても同じ (冪等)。worker の観測は期日の来たものだけ。
- 証拠へ戻す観測は観測だけ: 候補の指紋・版を変えない。変換済みで証拠が同じなら再び出さない。
  本当の証拠の変化は新しい版。
- 外への書き込み・呼び出し・変換・承認・公開は 0。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from app.growth import analysis as ga
from app.growth import inbox as gi
from app.growth import measurement as gm
from app.growth import outcome as go
from app.models import (
    ChangeApplication,
    ChangeRequest,
    Ga4ImportRun,
    Ga4PageDaily,
    SearchConsoleImportRun,
    SearchConsolePageDaily,
    ThreadsInsightSnapshot,
    ThreadsPostProposal,
    ThreadsPublication,
)
from app.models.growth_action import GrowthActionCandidate
from app.services import growth_action_service as gas
from app.services import growth_handoff_service as ghs
from app.services.growth_measurement_service import GrowthMeasurementService, summarize
from tests.integration import test_growth_action_conversion as conv
from tests.integration import test_growth_handoffs as hand
from tests.integration.test_growth_action_conversion import approved  # noqa: F401 - fixture
from tests.integration.test_growth_handoffs import articles, site  # noqa: F401 - fixture

_BASE = "https://example.com"
_APPLIED = datetime(2026, 10, 3, 3, 0, tzinfo=UTC)  # 2026-10-03 12:00 JST (変更日 10-03)


class _Settings:
    wordpress_base_url = _BASE
    search_console_property_uri = "sc-domain:example.com"
    search_console_credentials_file = None
    ga4_property_id = "123"


@pytest.fixture(autouse=True)
def _no_external(monkeypatch):
    import httpx

    from app.services import threads_openai_provider
    from app.social.threads import service as threads_service

    def refuse(*_a, **_k):
        raise AssertionError("follow-up measurement must not call anything external")

    monkeypatch.setattr(httpx.Client, "send", refuse)
    monkeypatch.setattr(threads_service.ThreadsService, "__init__", refuse)
    monkeypatch.setattr(threads_openai_provider.OpenAIResponsesClient, "__init__", refuse)


def _coverage(gsc=None, ga4=None, clicks=None):
    def provide(_now):
        def cov(name, through):
            if through is None:
                return gm.SourceCoverage(name, None, None, "unavailable")
            return gm.SourceCoverage(name, through, None, "fresh")

        return {gm.SRC_SEARCH_CONSOLE: cov(gm.SRC_SEARCH_CONSOLE, gsc),
                gm.SRC_GA4: cov(gm.SRC_GA4, ga4),
                gm.SRC_AFFILIATE: cov(gm.SRC_AFFILIATE, clicks)}

    return provide


def _measurement(session, **coverage) -> GrowthMeasurementService:
    return GrowthMeasurementService(session, settings=_Settings(), coverage=_coverage(**coverage))


def _gsc_rows(session, start: date, end: date, *, impressions=20, url=f"{_BASE}/"
              "rpa-implementation/"):
    run = SearchConsoleImportRun(property_uri="sc-domain:example.com", start_date=start,
                                 end_date=end, status="succeeded",
                                 dimensions_json='[["date","page"]]',
                                 import_identity_hash="g" * 64)  # fmt: skip
    session.add(run)
    session.flush()
    day = start
    while day <= end:
        session.add(SearchConsolePageDaily(property_uri="sc-domain:example.com", metric_date=day,
                                           page=url, clicks=1, impressions=impressions,
                                           ctr=1 / impressions, position=8.0,
                                           source_import_run_id=run.id))
        day += timedelta(days=1)
    session.commit()


def _ga4_rows(session, start: date, end: date, *, sessions=5, path="/rpa-implementation/"):
    run = Ga4ImportRun(property_id="123", start_date=start, end_date=end, status="succeeded",
                       request_json="{}", import_identity_hash="4" * 64)
    session.add(run)
    session.flush()
    day = start
    while day <= end:
        session.add(Ga4PageDaily(property_id="123", metric_date=day, page_path=path,
                                 channel_scope="all", sessions=sessions,
                                 source_import_run_id=run.id))
        day += timedelta(days=1)
    session.commit()


def _converted_change(session, approved):
    conv._service(session).execute(approved["row"].id, now=conv._NOW)
    return session.scalars(select(ChangeRequest)).one()


def _apply(session, request, *, at=_APPLIED, outcome="succeeded"):
    session.add(ChangeApplication(
        change_request_id=request.id, article_id=request.article_id,
        proposal_hash=request.proposal_hash, proposal_version=1, source_body_hash="b" * 64,
        proposed_body_hash="c" * 64, pre_change_body="本文", wordpress_post_id="1018",
        outcome=outcome, attempted_at=at, finished_at=at))  # fmt: skip
    session.commit()


def _one(service, now, checkpoint=None):
    [item] = service.anchors()
    return service.measure(item, now=now, checkpoint=checkpoint)


def _cp(measured, name):
    return next(c for c in measured.checkpoints if c["name"] == name)


# == 効果の始まり (変更の依頼) =====================================================================
def test_a_change_is_measured_only_from_a_successful_application(session, approved) -> None:
    request = _converted_change(session, approved)
    service = _measurement(session, gsc=date(2026, 12, 31))
    early = _one(service, datetime(2026, 11, 30, tzinfo=UTC))
    assert early.lifecycle.effective_at is None  # 変換・依頼の作成では始まらない
    assert early.next_measurement_at is None
    assert all(c["reasons"] == ["waiting for approval of the change request"]
               for c in early.checkpoints)
    _apply(session, request, at=_APPLIED - timedelta(days=1), outcome="failed")
    assert _one(service, datetime(2026, 11, 30, tzinfo=UTC)).lifecycle.effective_at is None
    _apply(session, request)
    live = _one(service, datetime(2026, 11, 30, tzinfo=UTC))
    assert live.lifecycle.effective_at == _APPLIED.isoformat()
    assert live.lifecycle.effective_event == gm.EV_CHANGE_APPLIED
    assert live.anchor["downstream_type"] == "change_request"
    assert live.anchor["evidence_version"].startswith("rev1:")


# == チェックポイントと出所の届き方 ================================================================
def test_checkpoints_wait_for_the_window_and_the_gsc_lag(session, approved) -> None:
    request = _converted_change(session, approved)
    _apply(session, request)
    _gsc_rows(session, date(2026, 9, 1), date(2026, 10, 31))
    not_due = _cp(_one(_measurement(session, gsc=date(2026, 10, 7)),
                       datetime(2026, 10, 8, tzinfo=UTC), "7d"), "7d")
    assert not_due["state"] == go.WAITING and "the window ends 2026-10-10" in not_due["reasons"]
    lag = _cp(_one(_measurement(session, gsc=date(2026, 10, 9)),
                   datetime(2026, 10, 12, tzinfo=UTC), "7d"), "7d")
    assert lag["state"] == go.WAITING
    assert any(r.startswith("waiting for search_console data") for r in lag["reasons"])
    stale = _cp(_one(_measurement(session, gsc=date(2026, 10, 9)),
                     datetime(2026, 10, 20, tzinfo=UTC), "7d"), "7d")
    assert stale["state"] == go.STALE_DATA
    done = _cp(_one(_measurement(session, gsc=date(2026, 10, 11)),
                    datetime(2026, 10, 12, tzinfo=UTC), "7d"), "7d")
    assert done["state"] == go.COMPLETED_WINDOW
    assert (done["before"]["impressions"], done["after"]["impressions"]) == (140, 140)
    assert done["differences"]["impressions"] == 0
    assert done["due_at"].startswith("2026-10-11T00:00:00")
    assert all("によ" not in o for o in done["observations"])


def test_future_data_is_excluded_and_missing_is_not_zero(session, approved) -> None:
    request = _converted_change(session, approved)
    _apply(session, request)
    _gsc_rows(session, date(2026, 9, 1), date(2026, 10, 31))
    _ga4_rows(session, date(2026, 9, 1), date(2026, 10, 31))
    from app.change.effect import EffectWindow
    from app.models import Article
    from app.services.change_effect_service import ChangeEffectService

    as_of = datetime(2026, 10, 12, 3, 0, tzinfo=UTC)  # 10-12 JST
    # 出所が「10-31 まで届いた」と言っても、観測の時点 (10-12) までしか使わない。
    long = _cp(_one(_measurement(session, gsc=date(2026, 10, 31), ga4=date(2026, 10, 31)),
                    as_of, "28d"), "28d")
    assert long["state"] == go.WAITING  # 窓 (10-04〜10-31) はまだ終わっていない
    assert long["after"]["impressions"] is None  # 途中の値を完成した窓として出さない
    window = EffectWindow(date(2026, 10, 4), date(2026, 10, 31), True, date(2026, 10, 31))
    clipped = ChangeEffectService(session, settings=_Settings()).measure_window(
        window, session.get(Article, 18), trusted_from=None, through=date(2026, 10, 12))
    assert clipped.impressions == 9 * 20  # 10-13 以降の行は使わない
    ga4_waiting = _cp(_one(_measurement(session, gsc=date(2026, 10, 11), ga4=date(2026, 10, 5)),
                           as_of, "7d"), "7d")
    assert ga4_waiting["state"] == go.WAITING  # 届いた出所だけで閉じない
    assert ga4_waiting["after"]["ga4_sessions"] is None  # 行はあるが、届いていない: 0 にしない
    assert ga4_waiting["sources"][gm.SRC_GA4]["state"] == go.WAITING
    covered = _cp(_one(_measurement(session, gsc=date(2026, 10, 11), ga4=date(2026, 10, 11)),
                       as_of, "7d"), "7d")
    assert covered["state"] == go.COMPLETED_WINDOW and covered["after"]["ga4_sessions"] == 35
    # クリックの出所が無い: None のまま (0 ではない)。
    assert covered["after"]["affiliate_clicks"] is None


def test_only_trusted_clicks_are_compared(session, approved) -> None:
    request = _converted_change(session, approved)
    _apply(session, request)
    _gsc_rows(session, date(2026, 9, 1), date(2026, 10, 31))
    service = _measurement(session, gsc=date(2026, 10, 31), clicks=date(2026, 10, 31))
    two_weeks = _cp(_one(service, datetime(2026, 11, 2, tzinfo=UTC), "14d"), "14d")
    # 前の窓 (09-19〜10-02) は信頼できる計測開始 (09-23) より前を含む: 比べない。
    assert two_weeks["before"]["affiliate_clicks"] is None
    assert two_weeks["differences"]["affiliate_clicks"] is None
    assert any("trusted measurement start" in r for r in two_weeks["reasons"])
    week = _cp(_one(service, datetime(2026, 11, 2, tzinfo=UTC), "7d"), "7d")
    assert week["before"]["affiliate_clicks"] == 0 and week["after"]["affiliate_clicks"] == 0


def test_measuring_again_is_idempotent_and_refresh_only_measures_due_anchors(session,
                                                                             approved) -> None:
    request = _converted_change(session, approved)
    _apply(session, request)
    _gsc_rows(session, date(2026, 9, 1), date(2026, 10, 31))
    service = _measurement(session, gsc=date(2026, 10, 11))
    now = datetime(2026, 10, 12, tzinfo=UTC)
    first, second = _one(service, now), _one(service, now)
    assert first.checkpoints == second.checkpoints  # 同じ観測 → 同じ結果
    cache: dict = {}
    measured, stats = service.refresh(now=now, cache=cache)
    assert (stats["measured"], stats["reused"]) == (1, 0)
    assert stats["changed"] == [approved["row"].id]  # 終わった窓が初めて出た
    assert stats["next_measurement_at"] == measured[0].next_measurement_at
    _again, stats = service.refresh(now=now + timedelta(minutes=5), cache=cache)
    assert (stats["measured"], stats["reused"], stats["changed"]) == (0, 1, [])
    later = datetime.fromisoformat(measured[0].next_measurement_at)
    _later, stats = service.refresh(now=later, cache=cache)
    assert stats["measured"] == 1


def test_the_operator_summary_lists_what_is_awaited(session, approved) -> None:
    request = _converted_change(session, approved)
    _apply(session, request)
    _gsc_rows(session, date(2026, 9, 1), date(2026, 10, 31))
    now = datetime(2026, 10, 12, tzinfo=UTC)
    service = _measurement(session, gsc=date(2026, 10, 11))
    summary = summarize(service.refresh(now=now)[0], now=now)
    assert summary["completed"] == {"completed 72h checkpoint": 1,
                                    "completed 7d checkpoint": 1}
    # 24h の窓は 1 日 20 回の表示だけ (下限 30 に届かない)。
    assert summary["insufficient"] == {"24h: insufficient trusted clicks / volume": 1}
    assert summary["waiting"] == {"14d: waiting for the 14d checkpoint": 1,
                                  "28d: waiting for the 28d checkpoint": 1}
    assert "no single success score" in summary["notes"][0]


# == Threads と記事の流れ ==========================================================================
def test_a_threads_request_is_measured_from_its_publication(session, site, tmp_path,
                                                            monkeypatch) -> None:
    row = hand._approved(session, site, ga.CREATE_REGULAR_THREADS_POST, 25)
    hand._service(session, site).execute(row.id, now=hand._NOW)
    hand._enable(monkeypatch)
    stock = hand._stock(session, tmp_path)
    stock.maintain(now=hand._NOW, execute=True)
    hand._answer(stock.provider)
    stock.maintain(now=hand._NOW + timedelta(hours=1), execute=True)
    service = GrowthMeasurementService(session, settings=_Settings(), coverage=_coverage())
    waiting = _one(service, hand._NOW + timedelta(hours=2))
    assert waiting.lifecycle.effective_at is None
    assert waiting.lifecycle.waiting_for == "approval of the proposal"
    [pid] = ghs.GrowthHandoffService(session).list()[0].downstream_ids_json
    proposal = session.get(ThreadsPostProposal, pid)
    proposal.status, proposal.approved_at = "approved", hand._NOW + timedelta(hours=3)
    session.commit()
    assert _one(service, hand._NOW + timedelta(hours=4)).lifecycle.waiting_for == "publication"
    published_at = hand._NOW + timedelta(hours=5)
    publication = ThreadsPublication(proposal_id=pid, proposal_hash=proposal.proposal_hash,
                                     angle=proposal.angle, source_article_id=25,
                                     exact_published_text=proposal.content_text,
                                     status="published", threads_media_id="m-1",
                                     published_at=published_at)  # fmt: skip
    session.add(publication)
    session.flush()
    session.add(ThreadsInsightSnapshot(threads_publication_id=publication.id,
                                       threads_media_id="m-1",
                                       observed_at=published_at + timedelta(hours=24),
                                       views=50, likes=2, replies=0, reposts=0, quotes=0,
                                       shares=0))  # fmt: skip
    session.commit()
    live = _one(service, published_at + timedelta(hours=30))
    assert live.lifecycle.effective_event == gm.EV_THREADS_PUBLISHED
    assert live.lifecycle.effective_at == published_at.isoformat()
    assert live.anchor["downstream_type"] == "threads_publication"
    assert _cp(live, "24h")["state"] == go.OBSERVABLE
    assert _cp(live, "72h")["state"] == go.WAITING
    assert _cp(live, "7d")["state"] == go.NOT_APPLICABLE
    assert [s.name for s in live.lifecycle.stages] == ["generation_request", "proposal",
                                                       "publication"]


def test_a_planning_request_is_measured_from_article_publication(session, site) -> None:
    from app.models import Article

    row = hand._approved(session, site, ga.CREATE_NEW_ARTICLE)
    hand._service(session, site).execute(row.id, now=hand._NOW)
    handoffs = ghs.GrowthHandoffService(session)
    [handoff] = handoffs.list()
    service = GrowthMeasurementService(session, settings=_Settings(), coverage=_coverage())
    assert _one(service, hand._NOW).lifecycle.waiting_for == (
        "human approval of the planning request")
    handoffs.decide_planning(handoff.id, approve=True, now=hand._NOW)
    article = Article(title="RPA 比較", slug="rpa-hikaku", status="drafting",
                      keyword_id=site["keyword"].id, created_at=hand._NOW + timedelta(hours=1),
                      published_url=f"{_BASE}/rpa-hikaku/")
    session.add(article)
    session.commit()
    handoffs.materialize(handoff.id, article_id=article.id, now=hand._NOW + timedelta(hours=2))
    session.commit()
    drafting = _one(service, hand._NOW + timedelta(hours=3))
    assert drafting.lifecycle.effective_at is None  # 記事の作成・下書きでは始まらない
    assert drafting.lifecycle.waiting_for == "publication of the article"
    published = datetime(2026, 10, 3, 3, 0, tzinfo=UTC)
    article.status, article.published_at = "published", published
    session.commit()
    _gsc_rows(session, date(2026, 10, 4), date(2026, 10, 31), url=f"{_BASE}/rpa-hikaku/")
    service = _measurement(session, gsc=date(2026, 10, 11))
    live = _one(service, datetime(2026, 10, 12, tzinfo=UTC), "7d")
    assert live.lifecycle.effective_event == gm.EV_ARTICLE_PUBLISHED
    week = _cp(live, "7d")
    # 公開は前の窓が無い (記事が無かった): 後の窓だけを観測し、比べない。
    assert week["state"] == go.OBSERVABLE and week["before"] == {}
    assert week["after"]["impressions"] == 140


def test_a_preparation_is_not_measured_until_a_linked_change_is_applied(session, site) -> None:
    row = hand._approved(session, site, ga.UPDATE_EXISTING_ARTICLE, 25)
    plan = hand._service(session, site).plan(row.id, now=hand._NOW)["plan"]
    hand._service(session, site).execute(row.id, now=hand._NOW,
                                         expected_source_hash=plan["source_hash"])
    service = GrowthMeasurementService(session, settings=_Settings(), coverage=_coverage())
    pending = _one(service, hand._NOW)
    assert pending.lifecycle.effective_at is None
    assert pending.lifecycle.waiting_for == "a concrete change linked to the preparation"
    [handoff] = ghs.GrowthHandoffService(session).list()
    linked = hand._change_request(session, 25, source_hash=handoff.frozen_json["source"][
        "body_hash"], seed="q", created_at=hand._NOW + timedelta(hours=1))  # fmt: skip
    ghs.GrowthHandoffService(session).prepare(handoff.id, downstream_type="change_request",
                                              downstream_id=linked.id, now=hand._NOW)
    session.commit()
    prepared = _one(service, hand._NOW + timedelta(hours=2))
    assert prepared.lifecycle.effective_at is None  # 準備した ≠ 適用
    assert [s.name for s in prepared.lifecycle.stages][:2] == ["preparation", "change_request"]


# == 証拠へ戻す (自分を強めない) ===================================================================
def _followup_report(followup=()):
    report = conv._report()
    for c in report["candidates"]:
        c["followup"] = list(followup)
    return report


def test_followup_never_changes_identity_or_creates_revisions(session, approved) -> None:
    evidence = ga.GrowthEvidence(subject_type="article", subject_id="article:1", article_id=1,
                                 article={"status": "published"},
                                 sources={"threads": ga.SourceEvidence(
                                     "threads", ga.USABLE, "t", "t",
                                     metrics={"publications": 0, "open_proposals": []})})
    item = {"growth_action_id": 1, "state": go.COMPLETED_WINDOW, "after": {"impressions": 9},
            "causal_claim": "none", "score": None}
    with_followup = ga.GrowthEvidence(**{**evidence.__dict__, "followup": (item,)})
    [plain] = ga.build_candidates(evidence, ga.classify(evidence))
    [fed] = ga.build_candidates(with_followup, ga.classify(with_followup))
    assert fed.followup == (item,) and plain.followup == ()
    assert fed.candidate_fingerprint == plain.candidate_fingerprint
    assert fed.evidence_fingerprint == plain.evidence_fingerprint
    assert list(fed.as_dict()["followup"]) == [item]
    # 変換済みの行動: 追跡の観測が入っても、同じ証拠なら再び出さない・新しい版を作らない。
    conv._service(session).execute(approved["row"].id, now=conv._NOW)
    history = gas.GrowthActionHistory(session)
    before = session.scalar(select(func.count()).select_from(GrowthActionCandidate))
    out = history.refresh(_followup_report([item]), gi.CoverageContext(),
                          now=conv._NOW + timedelta(days=10), execute=True)
    links = next(i for i in out["plan"]["items"] if i["action_type"] == ga.REVIEW_INTERNAL_LINKS)
    assert links["decision"] == gas.SUPPRESSED_COMPLETED and links["surfaced"] is False
    assert session.scalar(select(func.count()).select_from(GrowthActionCandidate)) == before
    # 本当の証拠の変化 (C6 の優先が変わった) は、新しい版になってよい。
    changed = conv._report(priority="high")
    for c in changed["candidates"]:
        c["followup"] = [item]
    history.refresh(changed, gi.CoverageContext(), now=conv._NOW + timedelta(days=11),
                    execute=True)
    assert session.scalar(select(func.count()).select_from(GrowthActionCandidate)) > before


def test_the_evidence_pipeline_carries_followup_as_observation(session, approved) -> None:
    from app.services.growth_evidence_service import GrowthEvidenceService

    item = {"growth_action_id": 9, "state": go.COMPLETED_WINDOW, "causal_claim": "none"}
    bundle = GrowthEvidenceService(session, settings=_Settings(), include_threads=False,
                                   include_growth_lane=False,
                                   followup={18: (item,)}).collect(now=conv._NOW)
    by_id = {e.article_id: e for e in bundle["evidence"] if e.subject_type == "article"}
    assert by_id[18].followup == (item,) and by_id[17].followup == ()
    assert by_id[18].as_dict()["followup"] == [item]


# == worker ========================================================================================
def test_the_worker_measures_only_due_anchors_and_writes_nothing(session, approved,
                                                                 tmp_path, monkeypatch) -> None:
    from app.services.threads_worker_service import ThreadsWorkerService
    from tests.integration.test_threads_proposal_stock_service import _factory, _ReadOnlyThreads

    request = _converted_change(session, approved)
    _apply(session, request)
    _gsc_rows(session, date(2026, 9, 1), date(2026, 10, 31))
    real = GrowthMeasurementService.__init__

    def with_coverage(self, *a, **k):
        real(self, *a, **{**k, "coverage": _coverage(gsc=date(2026, 10, 11))})

    monkeypatch.setattr(GrowthMeasurementService, "__init__", with_coverage)
    worker = ThreadsWorkerService(_factory(session), settings=_Settings(),
                                  threads_service=_ReadOnlyThreads(), alert_notifiers=[])

    def counts():
        names = [r[0] for r in session.execute(text(
            "select name from sqlite_master where type='table'"))]  # fmt: skip
        return {n: session.execute(text(f'select count(*) from "{n}"')).scalar() for n in names}

    before = counts()
    now = datetime(2026, 10, 12, tzinfo=UTC)
    _measured, stats = worker._measure_followups(session, now)
    assert (stats["anchors"], stats["measured"]) == (1, 1) and stats["changed"]
    _measured, again = worker._measure_followups(session, now + timedelta(hours=1))
    assert (again["measured"], again["reused"], again["changed"]) == (0, 1, [])
    assert counts() == before  # DB に書かない (変換・承認・適用・公開も無い)
    assert session.scalar(select(func.count()).select_from(ChangeApplication)) == 1
    assert session.get(ChangeRequest, request.id).status == request.status


# == 出所の届き方 (DB の取り込みの記録から) ========================================================
def test_coverage_comes_from_the_import_records(session, approved) -> None:
    from app.models import AffiliateClickImportRun

    request = _converted_change(session, approved)
    _apply(session, request)
    _gsc_rows(session, date(2026, 9, 1), date(2026, 10, 11))
    run = session.scalars(select(SearchConsoleImportRun)).one()
    run.finished_at = datetime(2026, 10, 12, 0, 30, tzinfo=UTC)
    session.add(AffiliateClickImportRun(status="succeeded", requested_since_id=0,
                                        requested_limit=100,
                                        finished_at=datetime(2026, 10, 12, 0, 30, tzinfo=UTC)))
    session.commit()
    service = GrowthMeasurementService(session, settings=_Settings())  # 偽の届き方を使わない
    measured = _one(service, datetime(2026, 10, 12, 3, 0, tzinfo=UTC), "7d")
    sources = measured.sources
    assert sources[gm.SRC_SEARCH_CONSOLE]["data_through"] == "2026-10-11"
    # クリックは cursor の取り込み: 取り込んだ日 (10-12 JST) の前の日まで届いている。
    assert sources[gm.SRC_AFFILIATE]["data_through"] == "2026-10-11"
    assert sources[gm.SRC_GA4]["freshness"] == "unavailable"  # GA4 は一度も取り込んでいない
    week = _cp(measured, "7d")
    assert week["state"] == go.COMPLETED_WINDOW
    assert week["after"]["ga4_sessions"] is None  # 出所が無い: 0 にしない


def test_the_outcomes_cli_is_read_only(session, approved, capsys) -> None:
    import json

    from scripts import analyze_growth_action_outcomes as cli
    from tests.integration.test_threads_proposal_stock_service import _factory

    request = _converted_change(session, approved)
    _apply(session, request)
    before = session.scalar(select(func.count()).select_from(GrowthActionCandidate))
    assert cli.main(["summary", "--as-of", "2026-10-02T00:00:00+00:00"],
                    session_factory=_factory(session), settings=_Settings()) == 0
    out = capsys.readouterr().out
    assert "no single success score" in out and "read-only" in out
    assert cli.main(["list", "--due", "--format", "json", "--as-of",
                     "2026-10-02T00:00:00+00:00"], session_factory=_factory(session),
                    settings=_Settings()) == 0
    payload = json.loads(capsys.readouterr().out.split("\nread-only")[0])
    assert payload["outcomes"] == []  # 期日が来たものはまだ無い
    assert cli.main(["show", str(approved["row"].id)], session_factory=_factory(session),
                    settings=_Settings()) == 0
    shown = capsys.readouterr().out
    assert "lifecycle: change_request=" in shown and "application=succeeded" in shown
    assert session.scalar(select(func.count()).select_from(GrowthActionCandidate)) == before
