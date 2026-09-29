"""Google Ads の値の取り直し (C10-2)。偽の provider だけ (本物は呼ばない)。

pin する契約:

- PLAN は呼ばない・書かない。期待値が違えば呼ばない。
- 既存の Keyword と発見の候補の語を **1 回** の呼び出しにまとめる (語ごとに呼ばない)。
- 発見の候補は Keyword にしない (証拠を候補に保存するだけ)。記事は作らない。
- 既定値の 0 (平均 0・履歴無し・入札 0・competition UNSPECIFIED) は欠測として保存する。
- 取った語は次の計画に入らない (返ってこなかった語も)。夜の分析の upsert は証拠を消さない。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.keyword.providers.google_ads import GoogleAdsKeywordMetrics, MonthlySearchVolume
from app.models import Article, Keyword, KeywordSignal
from app.models.content_discovery import ContentDiscoveryCandidate
from app.services.google_ads_refresh_service import GoogleAdsRefreshError, GoogleAdsRefreshService
from app.services.keyword_metrics_collection_service import KeywordMetricsCollectionService
from tests.support.google_ads_fakes import FakeGoogleAdsProvider, dummy_google_ads_settings

_NOW = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)


def _metrics(text, avg, *, months=12, bid=200_000_000, competition="MEDIUM", index=40):
    return GoogleAdsKeywordMetrics(
        keyword=text, avg_monthly_searches=avg,
        monthly_search_volumes=tuple(MonthlySearchVolume(2025, m, avg or 0)
                                     for m in range(1, months + 1)) if months else (),
        competition=competition, competition_index=index,
        low_top_of_page_bid_micros=bid, high_top_of_page_bid_micros=bid)


@pytest.fixture
def seeded(session):
    kw = Keyword(keyword="Zapier 使い方")
    session.add(kw)
    session.flush()
    for phrase, key in (("rpa ライセンス", "rpaライセンス"),
                        ("hubspot 導入費用", "hubspot導入費用"),
                        ("業務自動化 比較", "業務自動化比較")):
        session.add(ContentDiscoveryCandidate(
            phrase_key=key, phrase=phrase, status="tracked", duplicate_state="new",
            sources_json=[{"source": "gsc_query"}], evidence_json={"gsc_impressions": 3},
            refresh_needs_json=["google_ads"], first_seen_at=_NOW, last_seen_at=_NOW,
            seen_count=1, updated_at=_NOW))
    session.commit()
    return kw


def _service(session, provider):
    settings = dummy_google_ads_settings()
    collection = KeywordMetricsCollectionService(session, settings=settings, provider=provider)
    return GoogleAdsRefreshService(session, settings=settings, collection=collection)


def _provider():
    return FakeGoogleAdsProvider(metrics=[
        _metrics("zapier 使い方", 880),
        _metrics("rpa ライセンス", 170),
        # Google Ads の既定値 (推定が無い): 平均 0・履歴無し・入札 0・UNSPECIFIED
        _metrics("hubspot 導入費用", 0, months=0, bid=0, competition="UNSPECIFIED", index=0),
    ])  # 「業務自動化 比較」は返ってこない


def test_one_batched_call_refreshes_keywords_and_discovery_without_new_keywords(
        session, seeded) -> None:  # fmt: skip
    provider = _provider()
    service = _service(session, provider)
    plan = service.plan(now=_NOW)
    assert (plan["terms"], plan["calls_if_run"], plan["creates_keywords"]) == (4, 1, False)
    assert provider.calls == []  # PLAN は呼ばない
    with pytest.raises(GoogleAdsRefreshError, match="expected 3"):
        service.execute(expect_terms=3, now=_NOW)
    assert provider.calls == []
    keywords_before = session.scalar(select(func.count()).select_from(Keyword))
    result = service.execute(expect_terms=4, now=_NOW)
    assert result["provider_calls"] == 1 and len(provider.calls) == 1
    assert sorted(provider.calls[0]) == sorted(["Zapier 使い方", "rpa ライセンス",
                                                "hubspot 導入費用", "業務自動化 比較"])
    assert session.scalar(select(func.count()).select_from(Keyword)) == keywords_before
    assert session.scalar(select(func.count()).select_from(Article)) == 0
    assert result["keywords_created"] == 0
    components = {s.component for s in session.scalars(select(KeywordSignal).where(
        KeywordSignal.keyword_id == seeded.id))}
    assert components == {"search_demand", "commercial_intent", "trend"}
    rows = {r.phrase_key: r for r in session.scalars(select(ContentDiscoveryCandidate))}
    lic = rows["rpaライセンス"].evidence_json["google_ads"]
    assert lic["returned"] and lic["avg_monthly_searches"] == 170
    assert lic["search_volume_evidence"] == "observed" and lic["search_demand"] > 0
    assert rows["rpaライセンス"].refresh_needs_json == []
    default = rows["hubspot導入費用"].evidence_json["google_ads"]
    assert default["search_volume_evidence"] == "missing" and default["search_demand"] is None
    assert default["market_evidence_state"] == "missing"  # 0 を本当の 0 にしない
    assert rows["業務自動化比較"].evidence_json["google_ads"]["returned"] is False
    assert result["discovery_phrases"]["evidence"] == {"observed": 1, "missing": 1,
                                                       "not_returned": 1}
    assert rows["rpaライセンス"].status == "tracked"  # Keyword にしない
    again = service.plan(now=_NOW + timedelta(hours=1))
    assert again["terms"] == 0  # 取った語は次の計画に入らない


def test_the_nightly_upsert_keeps_the_cached_evidence(session, seeded) -> None:
    from datetime import date

    from app.models import SearchConsoleImportRun, SearchConsoleQueryDaily
    from app.services.nightly_analysis_service import NightlyAnalysisService

    _service(session, _provider()).execute(expect_terms=4, now=_NOW)
    run = SearchConsoleImportRun(property_uri="sc-domain:example.com", start_date=date(2026, 9, 25),
                                 end_date=date(2026, 9, 25), status="succeeded",
                                 dimensions_json="[]", import_identity_hash="r" * 64)
    session.add(run)
    session.flush()
    session.add(SearchConsoleQueryDaily(property_uri="sc-domain:example.com",
                                        metric_date=date(2026, 9, 25), page="https://e.com/p",
                                        query="rpa ライセンス", clicks=0, impressions=5, ctr=0.0,
                                        position=9.0, source_import_run_id=run.id))
    session.commit()

    class _Settings:
        wordpress_base_url = "https://example.com"
        ga4_property_id = None

    NightlyAnalysisService(session, settings=_Settings()).execute(now=_NOW + timedelta(days=1))
    row = session.scalars(select(ContentDiscoveryCandidate).where(
        ContentDiscoveryCandidate.phrase_key == "rpaライセンス")).first()
    assert row.last_seen_at.replace(tzinfo=UTC) > _NOW  # 夜の分析がこの候補を更新した
    assert row.evidence_json["gsc_impressions"] == 5  # 新しい観測は入る
    assert row.evidence_json["google_ads"]["avg_monthly_searches"] == 170  # 外の証拠は残る
    assert "google_ads" not in (row.refresh_needs_json or [])


def test_the_refresh_cli_is_plan_by_default(session, seeded, capsys) -> None:
    from scripts import refresh_google_ads_metrics as cli
    from tests.integration.test_threads_proposal_stock_service import _factory

    provider = _provider()
    settings = dummy_google_ads_settings()
    collection = KeywordMetricsCollectionService(session, settings=settings, provider=provider)
    assert cli.main([], session_factory=_factory(session), settings=settings,
                    collection=collection) == 0
    assert "external calls = 0" in capsys.readouterr().out and provider.calls == []
    with pytest.raises(SystemExit):
        cli.main(["--execute"], session_factory=_factory(session), settings=settings)
