"""夜の分析・Content Intelligence・次の記事 (C10-2、DB)。外に問い合わせない。

pin する契約:

- PLAN は書かない。候補が予算より少なければ少ないまま (埋めない)。多ければ説明できる順で
  選び、残りは理由つきで後回し。外の取り直しは提供元ごとに 1 回の一括の計画 (呼ばない)。
- EXECUTE は手元の 2 つの表だけ。同じ日の 2 回目は前の結果を返す。失敗はやり直せる。発見の
  候補の seen_count は日が変わったときだけ増える。人が外した候補は戻さない。Keyword になった
  候補は promoted。Keyword・記事・計画の依頼・Growth Action は作らない。表が無ければ断る。
- 次の記事: 既にある記事と重なる語は blocked。既存の Growth Action と同じ識別。
- search_demand: 保存済みの「平均 0・履歴無し」の 0.0 は欠測 (score を作らない)。
- C6 の索引: 調べていない・古い記事は GSC_UNKNOWN。調べた最新の状態を渡す。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from app.models import (
    Article,
    Keyword,
    KeywordSignal,
    OperationsRun,
    OperationsStepRun,
    SearchConsoleImportRun,
    SearchConsoleQueryDaily,
)
from app.models.content_discovery import ContentDiscoveryCandidate, NightlyAnalysisRun
from app.services.nightly_analysis_service import NightlyAnalysisError, NightlyAnalysisService

_NOW = datetime(2026, 10, 1, 18, 30, tzinfo=UTC)  # 2026-10-02 03:30 JST


class _Settings:
    wordpress_base_url = "https://example.com"
    ga4_property_id = None
    search_console_property_uri = "sc-domain:example.com"
    search_console_credentials_file = None


@pytest.fixture(autouse=True)
def _no_external(monkeypatch):
    import httpx

    from app.keyword.providers import google_ads
    from app.services import threads_openai_provider

    def refuse(*_a, **_k):
        raise AssertionError("the nightly analysis must not call anything external")

    monkeypatch.setattr(httpx.Client, "send", refuse)
    monkeypatch.setattr(google_ads.GoogleAdsKeywordMetricsProvider, "fetch_historical_metrics",
                        refuse)
    monkeypatch.setattr(threads_openai_provider.OpenAIResponsesClient, "__init__", refuse)


@pytest.fixture
def site(session):
    kws = {}
    for text_ in ("AI 議事録 おすすめ", "AI 議事録 比較", "RPA 比較", "RPA おすすめ"):
        kws[text_] = Keyword(keyword=text_)
        session.add(kws[text_])
    session.flush()
    session.add(Article(id=1, title="AI 議事録 おすすめ", slug="ai-minutes", status="published",
                        keyword_id=kws["AI 議事録 おすすめ"].id,
                        article_type="recommendation_roundup",
                        published_url="https://example.com/ai-minutes/"))
    session.add(Article(id=2, title="RPA おすすめ", slug="rpa", status="published",
                        keyword_id=kws["RPA おすすめ"].id, article_type="recommendation_roundup",
                        published_url="https://example.com/rpa/"))
    run = SearchConsoleImportRun(property_uri="sc-domain:example.com",
                                 start_date=date(2026, 9, 20), end_date=date(2026, 9, 28),
                                 status="succeeded", dimensions_json="[]",
                                 import_identity_hash="q" * 64)
    session.add(run)
    session.flush()
    for q, imp in (("rpa ライセンス", 4), ("rpa 比較", 3), ("chatgpt enterprise 価格", 2)):
        session.add(SearchConsoleQueryDaily(property_uri="sc-domain:example.com",
                                            metric_date=date(2026, 9, 25),
                                            page="https://example.com/rpa/", query=q, clicks=0,
                                            impressions=imp, ctr=0.0, position=9.0,
                                            source_import_run_id=run.id))
    session.commit()
    return kws


def _counts(session) -> dict:
    names = [r[0] for r in session.execute(text(
        "select name from sqlite_master where type='table'"))]  # fmt: skip
    return {n: session.execute(text(f'select count(*) from "{n}"')).scalar() for n in names}


def _service(session, **kw):
    return NightlyAnalysisService(session, settings=_Settings(), **kw)


# == PLAN ==========================================================================================
def test_the_plan_writes_nothing_and_does_not_pad_the_budget(session, site) -> None:
    before = _counts(session)
    plan = _service(session).plan(now=_NOW)
    assert _counts(session) == before
    c = plan["counts"]
    assert c["budget"] == 80 and c["analyzed"] == c["eligible"] < 80  # 埋めない
    assert c["deferred_by_budget"] == 0
    assert plan["creates_articles"] is False and plan["creates_keywords"] is False
    assert plan["external_calls"] == 0 and plan["composite_score"] is None
    topics = {n["topic"]: n for n in plan["next_articles"]}
    assert "AI 議事録 おすすめ" not in topics  # 記事がある語は候補にしない
    assert "rpa ライセンス" in topics and topics["rpa ライセンス"]["handoff"]["readiness"] == (
        "needs_keyword_promotion")
    comparison = topics["AI 議事録 比較"]
    assert comparison["identity"].startswith("create_new_article:keyword:keyword:")
    assert comparison["selection"]["tier"] in ("existing_keyword_gap", "existing_keyword")
    skipped = {s["subject"]: s for s in plan["skipped"]}
    assert skipped["discovery:rpa比較"]["reason"] == "prefilter: existing_keyword"


def test_a_small_budget_defers_with_reasons_and_refresh_is_one_batch(session, site) -> None:
    plan = _service(session).plan(now=_NOW, budget=2)
    assert plan["counts"]["analyzed"] == 2 and plan["counts"]["deferred_by_budget"] >= 1
    assert all(d["reason"] == "beyond the analysis budget for this run"
               for d in plan["deferred"])
    tiers = [n["selection"]["tier"] for n in plan["next_articles"]]
    assert tiers[0].startswith("existing_keyword")  # 説明できる順 (既にある keyword が先)
    ads = _service(session).plan(now=_NOW)["refresh_plan"]["google_ads"]
    assert ads["calls_if_run"] == 1 and ads["terms"] >= 2  # 候補ごとに呼ばない
    assert ads["status"].startswith("PENDING HUMAN")


def test_repeated_plans_are_identical(session, site) -> None:
    a = _service(session).plan(now=_NOW)
    b = _service(session).plan(now=_NOW)
    strip = lambda p: {k: v for k, v in p.items() if k != "sources"}  # noqa: E731
    assert strip(a) == strip(b)


# == EXECUTE =======================================================================================
def test_execute_records_the_run_and_discovery_idempotently(session, site) -> None:
    keywords_before = session.scalar(select(func.count()).select_from(Keyword))
    articles_before = session.scalar(select(func.count()).select_from(Article))
    result = _service(session).execute(now=_NOW, trigger="scheduler")
    assert result["executed"] is True
    [run] = session.scalars(select(NightlyAnalysisRun)).all()
    assert (run.run_key, run.status, run.attempt_count) == ("nightly:2026-10-02", "succeeded", 1)
    assert run.analyzed_count == result["plan_counts"]["analyzed"]
    assert run.refresh_plan_json["google_ads"]["calls_if_run"] == 1
    rows = {r.phrase_key: r for r in session.scalars(select(ContentDiscoveryCandidate))}
    assert rows["rpaライセンス"].status == "tracked" and rows["rpaライセンス"].seen_count == 1
    assert rows["rpa比較"].status == "suppressed"
    assert session.scalar(select(func.count()).select_from(Keyword)) == keywords_before
    assert session.scalar(select(func.count()).select_from(Article)) == articles_before
    replay = _service(session).execute(now=_NOW + timedelta(hours=1))
    assert replay["idempotent_replay"] is True
    assert session.scalar(select(func.count()).select_from(NightlyAnalysisRun)) == 1
    session.refresh(rows["rpaライセンス"])
    assert rows["rpaライセンス"].seen_count == 1
    _service(session).execute(now=_NOW + timedelta(days=1))
    session.refresh(rows["rpaライセンス"])
    assert rows["rpaライセンス"].seen_count == 2  # 日が変わったときだけ
    assert session.scalar(select(func.count()).select_from(NightlyAnalysisRun)) == 2


def test_dismissed_candidates_stay_dismissed_and_new_keywords_promote(session, site) -> None:
    _service(session).execute(now=_NOW)
    row = session.scalars(select(ContentDiscoveryCandidate).where(
        ContentDiscoveryCandidate.phrase_key == "rpaライセンス")).one()
    row.status = "dismissed"
    enterprise = session.scalars(select(ContentDiscoveryCandidate).where(
        ContentDiscoveryCandidate.phrase_key == "chatgptenterprise価格")).one()
    assert enterprise.status == "tracked"
    session.add(Keyword(keyword="ChatGPT Enterprise 価格"))  # 人が Keyword にした
    session.commit()
    _service(session).execute(now=_NOW + timedelta(days=1))
    session.refresh(row)
    session.refresh(enterprise)
    assert row.status == "dismissed"
    assert enterprise.status == "promoted" and enterprise.keyword_id is not None


def test_a_failed_run_is_recorded_and_can_be_retried(session, site, monkeypatch) -> None:
    service = _service(session)
    monkeypatch.setattr(service, "_upsert_discovery",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("disk full")))
    with pytest.raises(NightlyAnalysisError, match="disk full"):
        service.execute(now=_NOW)
    [run] = session.scalars(select(NightlyAnalysisRun)).all()
    assert run.status == "failed" and "disk full" in run.failure_reason
    assert session.scalar(select(func.count()).select_from(ContentDiscoveryCandidate)) == 0
    retry = _service(session).execute(now=_NOW + timedelta(minutes=10))
    assert retry["executed"] and retry["run"]["attempt_count"] == 2
    assert retry["run"]["status"] == "succeeded"


def test_execute_refuses_before_the_migration(session, site) -> None:
    session.execute(text("drop table content_discovery_candidates"))
    session.execute(text("drop table nightly_analysis_runs"))
    session.commit()
    with pytest.raises(NightlyAnalysisError, match="c1d0e233e180"):
        _service(session).execute(now=_NOW)
    assert _service(session).plan(now=_NOW)["counts"]["eligible"] > 0  # PLAN は動く


# == 次の記事の重なり ==============================================================================
def test_an_overlapping_keyword_is_blocked_not_planned(session, site) -> None:
    session.add(Keyword(keyword="AI 議事録 ランキング"))  # 既にある「おすすめ」の記事と同じ意図
    session.commit()
    plan = _service(session).plan(now=_NOW)
    item = next(n for n in plan["next_articles"] if n["topic"] == "AI 議事録 ランキング")
    assert item["handoff"]["readiness"] == "blocked"
    assert item["cannibalization"][0]["ref"] == "article:1"


# == search_demand の欠測 =========================================================================
def test_a_stored_zero_without_history_is_missing_for_scoring(session, site) -> None:
    from app.exceptions import IncompleteSignalSetError
    from app.keyword.scoring import COMPONENT_NAMES
    from app.services.keyword_scoring_service import KeywordScoringService

    kw = site["RPA 比較"]
    for name in COMPONENT_NAMES:
        raw = ({"avg_monthly_searches": 0, "monthly_search_volumes": []}
               if name == "search_demand" else {})
        value = 0.0 if name == "search_demand" else 50.0
        session.add(KeywordSignal(keyword_id=kw.id, component=name, normalized_value=value,
                                  provider="google_ads",
                                  observed_at=_NOW - timedelta(days=3), raw_data=raw))
    session.commit()
    with pytest.raises(IncompleteSignalSetError) as caught:
        KeywordScoringService(session).score_keyword_from_latest_signals(kw.id)
    assert "search_demand" in str(caught.value)


# == C6 の索引 =====================================================================================
def test_c6_receives_saved_index_state_without_calls(session, site) -> None:
    from app.services.index_state_service import IndexStateService

    run = OperationsRun(profile="weekly", policy_version="c8", effective_date=date(2026, 9, 27),
                        timezone_name="Asia/Tokyo", status="succeeded")
    session.add(run)
    session.flush()
    session.add(OperationsStepRun(
        operations_run_id=run.id, step_name="check_indexability", status="succeeded",
        finished_at=_NOW - timedelta(days=4),
        result_json={"inspect": True, "articles": [
            {"article_id": 1, "google_index_state": "GSC_INDEXED", "live_state": "LIVE_HEALTHY",
             "sitemap_state": "SITEMAP_PRESENT"},
            {"article_id": 2, "google_index_state": "GSC_DISCOVERED_NOT_INDEXED",
             "live_state": "LIVE_HEALTHY", "sitemap_state": "SITEMAP_PRESENT"}]}))
    session.commit()
    c6 = IndexStateService(session).as_c6_indexability(now=_NOW)
    rows = {r.article_id: r for r in c6.articles}
    assert rows[2].google_index_state == "GSC_DISCOVERED_NOT_INDEXED"
    assert rows[1].live_state == "LIVE_HEALTHY"
    assert c6.generated_at == _NOW - timedelta(days=4)
    later = IndexStateService(session).as_c6_indexability(now=_NOW + timedelta(days=10))
    # 古い調べは分からないと言う (索引されていない、とは言わない)。
    assert {r.google_index_state for r in later.articles} == {"GSC_UNKNOWN"}


# == CLI ===========================================================================================
def test_the_cli_is_plan_by_default(session, site, capsys) -> None:
    from scripts import plan_next_articles, run_nightly_analysis
    from tests.integration.test_threads_proposal_stock_service import _factory

    before = _counts(session)
    assert run_nightly_analysis.main(["--as-of", _NOW.isoformat()],
                                     session_factory=_factory(session), settings=_Settings()) == 0
    out = capsys.readouterr().out
    assert "external refresh plan (not called)" in out and "database writes = 0" in out
    assert plan_next_articles.main(["--as-of", _NOW.isoformat()],
                                   session_factory=_factory(session), settings=_Settings()) == 0
    assert "no article is created" in capsys.readouterr().out
    assert run_nightly_analysis.main(["--section", "schedule"]) == 0
    schedule = capsys.readouterr().out
    assert "registered: False" in schedule and "03:30" in schedule
    assert _counts(session) == before
