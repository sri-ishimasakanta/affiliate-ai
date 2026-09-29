"""C10-A の分析の土台 (DB)。外に問い合わせない。

pin する契約:

- 出所の状態は 1 か所で決まり、C9 の証拠の鮮度 (fresh / stale / unavailable) は変わらない。
  最新の取り込みの失敗は provider_error、取り込めたが行が無いは insufficient、設定が無い・一度も
  無いは missing。クリックの data-through は取り込んだ日の前の日。観測の時点より後は使わない。
- 索引: URL Inspection をした最新の確認を使う (新しい日ごとの確認の unknown で上書きしない)。
  古い調べは stale。一度も調べていない記事は unknown。C9 の証拠の index の出所も同じ。
- commercial_intent の導き直し: 保存済みの値だけ (Google Ads を呼ばない)。PLAN は書かない。
  期待値が違えば断る。2 回目は何も書かない。元の観測の時刻のまま。search_demand は変えない。
- 帰属: クリックは記事まで、成果は提供元まで。束は印を付けるだけ。記事の収益は作らない。
- 分析の証拠と CLI は読むだけ。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from app.models import (
    AffiliateClickImportRun,
    AffiliateCommissionImportRun,
    AffiliateLinkTarget,
    AffiliateOutboundClick,
    AffiliateProgram,
    Article,
    Ga4ImportRun,
    Keyword,
    KeywordSignal,
    OperationsRun,
    OperationsStepRun,
    SearchConsoleImportRun,
    SearchConsolePageDaily,
)
from app.services.source_health_service import SourceHealthService

_NOW = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)  # 2026-10-01 12:00 JST


class _Settings:
    wordpress_base_url = "https://example.com"
    ga4_property_id = "123"
    search_console_property_uri = "sc-domain:example.com"
    search_console_credentials_file = None


class _NoGa4(_Settings):
    ga4_property_id = None


@pytest.fixture(autouse=True)
def _no_external(monkeypatch):
    import httpx

    def refuse(*_a, **_k):
        raise AssertionError("analysis must not call anything external")

    monkeypatch.setattr(httpx.Client, "send", refuse)


def _counts(session) -> dict:
    names = [r[0] for r in session.execute(text(
        "select name from sqlite_master where type='table'"))]  # fmt: skip
    return {n: session.execute(text(f'select count(*) from "{n}"')).scalar() for n in names}


def _sc_run(session, *, end, finished, status="succeeded"):
    session.add(SearchConsoleImportRun(property_uri="sc-domain:example.com", start_date=end,
                                       end_date=end, status=status, dimensions_json="[]",
                                       import_identity_hash="s" * 64, finished_at=finished))
    session.commit()


def _health(session, settings=_Settings, now=_NOW):
    return SourceHealthService(session, settings=settings()).collect(now=now)


# == 出所の状態 ====================================================================================
def test_import_sources_distinguish_missing_error_stale_and_fresh(session) -> None:
    health = _health(session)
    assert health["search_console"].freshness_state == "missing"
    assert health["search_console"].legacy_state == "unavailable"  # C9 の言葉のまま
    _sc_run(session, end=date(2026, 9, 29), finished=_NOW - timedelta(hours=5))
    empty = _health(session)["search_console"]
    assert empty.freshness_state == "insufficient"  # 取り込めたが行が無い (0 ではない)
    assert empty.legacy_state == "fresh"
    run = session.scalars(select(SearchConsoleImportRun)).first()
    session.add(SearchConsolePageDaily(property_uri="sc-domain:example.com",
                                       metric_date=date(2026, 9, 28), page="https://example.com/",
                                       clicks=1, impressions=10, ctr=0.1, position=5.0,
                                       source_import_run_id=run.id))
    session.commit()
    fresh = _health(session)["search_console"]
    assert fresh.freshness_state == "fresh" and fresh.legacy_state == "fresh"
    assert fresh.data_through == "2026-09-29" and fresh.usable
    _sc_run(session, end=date(2026, 9, 29), finished=None, status="failed")
    error = _health(session)["search_console"]
    assert error.freshness_state == "provider_error" and error.needs_external_refresh
    assert error.legacy_state == "fresh"  # C9 の判定 (成功の時刻だけを見る) は変えない
    stale = _health(session, now=_NOW + timedelta(days=5))["search_console"]
    assert stale.freshness_state in ("stale", "provider_error")
    assert stale.legacy_state == "stale"


def test_ga4_without_a_property_is_missing_and_clicks_use_the_cursor_rule(session) -> None:
    session.add(Ga4ImportRun(property_id="123", start_date=date(2026, 9, 29),
                             end_date=date(2026, 9, 30), status="succeeded", request_json="{}",
                             import_identity_hash="4" * 64, finished_at=_NOW))
    session.add(AffiliateClickImportRun(status="succeeded", requested_since_id=0,
                                        requested_limit=100,
                                        finished_at=datetime(2026, 9, 30, 22, 0, tzinfo=UTC)))
    session.commit()
    health = _health(session, settings=_NoGa4)
    assert health["ga4"].freshness_state == "missing"
    assert health["ga4"].missing_reason == "ga4_property_id is not configured"
    clicks = health["affiliate_clicks"]
    # 10-01 07:00 JST の取り込み → 09-30 まで届いている。
    assert clicks.data_through == "2026-09-30"
    assert clicks.freshness_state == "insufficient"  # 取り込めたが、行がまだ無い (0 ではない)


def test_future_coverage_is_clipped_to_the_observation_time(session) -> None:
    _sc_run(session, end=date(2026, 10, 20), finished=_NOW - timedelta(hours=1))
    assert _health(session)["search_console"].data_through == "2026-10-01"


def test_the_c9_evidence_and_measurement_use_the_same_judgement(session) -> None:
    from app.services.growth_evidence_service import GrowthEvidenceService
    from app.services.growth_measurement_service import GrowthMeasurementService

    _sc_run(session, end=date(2026, 9, 29), finished=_NOW - timedelta(hours=5))
    states, _detail = GrowthEvidenceService(session, settings=_Settings())._freshness(_NOW)
    health = _health(session)
    for name in ("search_console", "ga4", "affiliate_clicks", "make_commissions"):
        assert states[name] == health[name].legacy_state
    coverage = GrowthMeasurementService(session, settings=_Settings())._coverage(_NOW)
    assert coverage["search_console"].data_through == date(2026, 9, 29)
    assert coverage["search_console"].freshness == "fresh"


def test_google_ads_status_comes_from_stored_signals_only(session) -> None:
    ads = _health(session)["google_ads"]
    assert ads.freshness_state == "missing" and ads.needs_external_refresh
    keyword = Keyword(keyword="AI 比較")
    session.add(keyword)
    session.flush()
    session.add(KeywordSignal(keyword_id=keyword.id, component="commercial_intent",
                              normalized_value=85.0, provider="google_ads",
                              observed_at=_NOW - timedelta(days=50),
                              raw_data={"low_top_of_page_bid_micros": 0, "competition_index": 0,
                                        "competition": "UNSPECIFIED"}))
    session.commit()
    stale = _health(session)["google_ads"]
    assert stale.freshness_state == "stale"  # 45 日を過ぎた (source_policy.json)
    assert stale.affected["market_evidence"] == {"missing": 1}
    assert "1_keywords_without_market_evidence" in stale.quality_flags


# == 索引の状態 ====================================================================================
def _step(session, *, inspect, finished, articles, profile="daily"):
    run = OperationsRun(profile=profile, policy_version="c8", effective_date=finished.date(),
                        timezone_name="Asia/Tokyo", status="succeeded")
    session.add(run)
    session.flush()
    session.add(OperationsStepRun(operations_run_id=run.id, step_name="check_indexability",
                                  status="succeeded", finished_at=finished,
                                  result_json={"inspect": inspect, "articles": articles}))
    session.commit()


def _article(session, aid=7):
    session.add(Article(id=aid, title="t", slug=f"a-{aid}", status="published",
                        published_url=f"https://example.com/a-{aid}/"))
    session.commit()


def test_the_latest_inspected_run_wins_over_a_newer_daily_unknown(session) -> None:
    from app.services.index_state_service import IndexStateService

    _article(session)
    _step(session, inspect=True, finished=_NOW - timedelta(days=4), profile="weekly",
          articles=[{"article_id": 7, "url": "u", "google_index_state": "GSC_INDEXED",
                     "verdict": "PASS", "live_state": "LIVE_HEALTHY"}])
    _step(session, inspect=False, finished=_NOW - timedelta(days=1),
          articles=[{"article_id": 7, "url": "u", "google_index_state": "GSC_UNKNOWN",
                     "live_state": "LIVE_REDIRECTED", "sitemap_state": "SITEMAP_PRESENT"}])
    observations, meta = IndexStateService(session).latest(now=_NOW)
    o = observations[7]
    assert o.normalized_status == "indexed" and o.inspected and o.freshness_state == "fresh"
    assert o.site_checks == {"live_state": "LIVE_REDIRECTED", "sitemap_state": "SITEMAP_PRESENT"}
    assert meta["latest_inspected_run_id"] != meta["latest_run_id"]
    later, _meta = IndexStateService(session).latest(now=_NOW + timedelta(days=10))
    assert later[7].freshness_state == "stale"  # 古い調べを最新として使わない


def test_an_article_never_inspected_is_unknown_not_unindexed(session) -> None:
    from app.growth import analysis as ga
    from app.services.growth_evidence_service import GrowthEvidenceService
    from app.services.index_state_service import IndexStateService

    _article(session)
    _step(session, inspect=False, finished=_NOW - timedelta(days=1),
          articles=[{"article_id": 7, "url": "u", "google_index_state": "GSC_UNKNOWN"}])
    [o] = IndexStateService(session).latest(now=_NOW)[0].values()
    assert o.normalized_status == "unknown" and not o.known
    rows, _at = GrowthEvidenceService(session, settings=_Settings())._index_snapshot(_NOW)
    assert ga.index_source(rows[7], observed_at=None).state == ga.INSUFFICIENT
    _step(session, inspect=True, finished=_NOW - timedelta(days=20), profile="weekly",
          articles=[{"article_id": 7, "url": "u", "google_index_state": "GSC_INDEXED"}])
    rows, _at = GrowthEvidenceService(session, settings=_Settings())._index_snapshot(_NOW)
    assert ga.index_source(rows[7], observed_at=None).state == ga.STALE  # 20 日前の調べ
    assert _health(session)["url_inspection"].freshness_state == "stale"


# == commercial_intent の導き直し ==================================================================
def _ads_keyword(session, text_, *, low, index, competition, value, version="v1"):
    keyword = Keyword(keyword=text_)
    session.add(keyword)
    session.flush()
    observed = datetime(2026, 8, 28, 3, 34, tzinfo=UTC)
    raw = {"low_top_of_page_bid_micros": low, "high_top_of_page_bid_micros": low,
           "competition": competition, "competition_index": index,
           "normalizer": {"name": "commercial_intent", "version": version},
           "normalizer_version": version}
    for component, v in (("commercial_intent", value), ("search_demand", 40.0)):
        session.add(KeywordSignal(keyword_id=keyword.id, component=component,
                                  normalized_value=v, provider="google_ads", observed_at=observed,
                                  raw_data=raw,
                                  source_reference="google-ads:keyword-plan-idea:historical-metrics"))
    session.commit()
    return keyword


def test_rederive_plans_from_stored_values_and_executes_idempotently(session) -> None:
    from app.services.commercial_intent_rederive_service import (
        CommercialIntentRederiveService,
        RederiveRefusedError,
    )

    zero = _ads_keyword(session, "生成AI 法人 導入", low=0, index=0, competition="UNSPECIFIED",
                        value=51.0)
    real = _ads_keyword(session, "AI 議事録 比較", low=250_000_000, index=88, competition="HIGH",
                        value=81.76)
    session.add(Keyword(keyword="新しいキーワード"))  # Google Ads の値が無い
    session.commit()
    service = CommercialIntentRederiveService(session)
    before = _counts(session)
    plan = service.plan()
    assert _counts(session) == before  # PLAN は書かない
    items = {i.keyword_id: i for i in plan.items}
    assert (items[zero.id].current_value, items[zero.id].new_value) == (51.0, 85.0)
    assert items[zero.id].market_evidence_state == "missing"
    assert items[real.id].new_value == 81.76 and not items[real.id].value_changed
    assert [i.decision for i in plan.items][-1] == "no_stored_metrics"
    with pytest.raises(RederiveRefusedError):
        service.execute(expect_rederive=1, expect_rescore=0)
    assert _counts(session) == before  # 期待値が違えば何も書かない
    result = service.execute(expect_rederive=2, expect_rescore=0)
    assert result["executed"] and len(result["signals_created"]) == 2
    latest = session.scalars(select(KeywordSignal).where(
        KeywordSignal.keyword_id == zero.id, KeywordSignal.component == "commercial_intent")
        .order_by(KeywordSignal.observed_at.desc(), KeywordSignal.id.desc())).first()
    assert latest.normalized_value == 85.0 and latest.raw_data["normalizer"]["version"] == "v2"
    assert latest.observed_at.replace(tzinfo=UTC) == datetime(2026, 8, 28, 3, 34, tzinfo=UTC)
    assert latest.raw_data["rederived_without_external_call"] is True
    demand = session.scalars(select(KeywordSignal).where(
        KeywordSignal.keyword_id == zero.id, KeywordSignal.component == "search_demand")).all()
    assert [d.normalized_value for d in demand] == [40.0]  # search_demand は変えない
    again = service.execute(expect_rederive=0, expect_rescore=0)
    assert again == {"executed": False, "reason": "already_current",
                     "counts": again["counts"]}


def test_the_rederive_cli_is_plan_by_default(session, capsys) -> None:
    from scripts import rederive_commercial_intent as cli
    from tests.integration.test_threads_proposal_stock_service import _factory

    _ads_keyword(session, "生成AI 法人 導入", low=0, index=0, competition="UNSPECIFIED",
                 value=51.0)
    before = _counts(session)
    assert cli.main([], session_factory=_factory(session)) == 0
    out = capsys.readouterr().out
    assert "51.0 (v1) → 85.0 [missing]" in out and "external calls = 0" in out
    assert _counts(session) == before
    with pytest.raises(SystemExit):
        cli.main(["--execute"], session_factory=_factory(session))  # 期待値が要る


# == 帰属 ==========================================================================================
def test_attribution_links_clicks_to_articles_and_flags_bursts(session) -> None:
    from app.services.attribution_readiness_service import AttributionReadinessService

    _article(session)
    program = AffiliateProgram(name="Make", provider="make", tracking_url="https://www.make.com/x",
                               status="active")
    session.add(program)
    session.flush()
    session.add(AffiliateLinkTarget(token="t" * 20, article_id=7,
                                    affiliate_program_id=program.id,
                                    destination_url="https://www.make.com/x",
                                    destination_host="www.make.com", link_identity_hash="h" * 64))
    run = AffiliateClickImportRun(status="succeeded", requested_since_id=0, requested_limit=10)
    session.add(run)
    session.flush()
    burst_at = datetime(2026, 9, 25, 16, 19, 6, tzinfo=UTC)
    rows = [(1, "t" * 20, burst_at), (2, "t" * 20, burst_at + timedelta(milliseconds=300)),
            (3, "t" * 20, datetime(2026, 9, 26, 5, 0, tzinfo=UTC)),
            (4, "u" * 20, datetime(2026, 9, 26, 6, 0, tzinfo=UTC)),
            (5, "t" * 20, datetime(2026, 9, 1, 6, 0, tzinfo=UTC))]
    for sid, token, at in rows:
        session.add(AffiliateOutboundClick(source_click_id=sid, token=token, clicked_at=at,
                                           source_import_run_id=run.id))
    session.add(AffiliateCommissionImportRun(provider="make", affiliate_program_id=program.id,
                                             status="succeeded"))
    session.commit()
    before = _counts(session)
    report = AttributionReadinessService(session).report()
    assert _counts(session) == before
    clicks = report["clicks"]
    assert (clicks["trusted_linked"], clicks["trusted_unattributed"],
            clicks["before_trusted_start"]) == (3, 1, 1)
    assert clicks["possible_instrumentation_bursts"] == 1 and clicks["clicks_in_bursts"] == 2
    [make] = [p for p in report["programs"] if p["program_id"] == program.id]
    assert make["click_attribution_level"] == "A_direct"
    assert make["commission_attribution_level"] == "B_provider"
    assert make["deterministic_join_possible"] is False and make["subid_currently_used"] is False
    assert report["commissions"]["rows_with_click_reference"] == 0
    assert "no revenue is allocated to articles" in report["notes"][0]
    assert session.get(AffiliateProgram, program.id).tracking_url == "https://www.make.com/x"


# == 分析の証拠と CLI ==============================================================================
def test_the_evidence_contract_and_the_health_cli_are_read_only(session, capsys) -> None:
    import json

    from scripts import analyze_signal_health as cli
    from tests.integration.test_threads_proposal_stock_service import _factory

    _ads_keyword(session, "生成AI 法人 導入", low=0, index=0, competition="UNSPECIFIED",
                 value=51.0)
    session.add(Keyword(keyword="取り直しが要る"))
    session.commit()
    _article(session)
    before = _counts(session)
    assert cli.main(["--section", "all", "--format", "json", "--as-of", _NOW.isoformat()],
                    session_factory=_factory(session), settings=_Settings()) == 0
    payload = json.loads(capsys.readouterr().out.split("\nread-only")[0])
    assert _counts(session) == before
    assert set(payload["sources"]) == {"search_console", "ga4", "affiliate_clicks",
                                       "make_commissions", "google_ads", "threads_insights",
                                       "url_inspection"}
    candidates = {c["subject_id"]: c for c in payload["keywords"]["candidates"]}
    assert all(c["composite_score"] is None for c in candidates.values())
    missing = next(c for c in candidates.values()
                   if c["components"]["commercial_intent"]["state"] == "missing")
    assert "commercial_intent" in missing["needs_external_refresh"]
    assert payload["refresh"]["google_ads"]["calls_if_run"] == 1
    assert payload["articles"]["components"]["index_state"] == {"missing": 1}
    flagged = next(c for c in candidates.values()
                   if c["components"]["commercial_intent"]["value"] == 51.0)
    assert "market_evidence_missing_query_intent_only" not in (
        flagged["components"]["commercial_intent"]["quality_flags"])  # v1 の行は印が無い
    assert session.scalar(select(func.count()).select_from(KeywordSignal)) == 2


def test_the_weekly_step_keeps_raw_inspection_values_without_new_calls(session,
                                                                       monkeypatch) -> None:
    from types import SimpleNamespace

    from app.services import article_indexability_report_service as reports
    from tests.integration.test_operations_runner import _runner

    calls = []

    class _Report:
        def __init__(self, _session, *, settings):
            pass

        def build(self, *, inspect):
            calls.append(inspect)
            return SimpleNamespace(articles=[SimpleNamespace(
                article_id=7, url="https://example.com/a-7/", live_state="LIVE_HEALTHY",
                sitemap_state="SITEMAP_PRESENT", google_index_state="GSC_INDEXED",
                live_issues=(), google_facts={
                    "verdict": "PASS", "coverage_state": "Submitted and indexed",
                    "last_crawl_time": "2026-09-20T01:00:00Z", "robots_txt_state": "ALLOWED",
                    "google_canonical": "https://example.com/a-7/", "referring_urls": "x"})])

    monkeypatch.setattr(reports, "ArticleIndexabilityReportService", _Report)
    outcome = _runner(session)._step_check_indexability(profile="weekly")
    [row] = outcome.result["articles"]
    assert calls == [True]  # 週ごとの実行が今までどおり調べる (呼び出しの形は同じ)
    assert row["verdict"] == "PASS" and row["last_crawl_time"] == "2026-09-20T01:00:00Z"
    assert "google_canonical" not in row and "referring_urls" not in row  # URL の値は入れない
    assert row["google_index_state"] == "GSC_INDEXED" and row["live_state"] == "LIVE_HEALTHY"
