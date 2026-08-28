"""KeywordMetricsCollectionService.collect_google_ads_signals_bulk の検証 (実通信なし)。

1 回の bulk fetch から search_demand / commercial_intent / trend の 3 Signal を導出し、
既存の単体 collector と同じ normalizer 結果になることを確認する (計算式コピーなし)。
"""

import pytest
from sqlalchemy.orm import Session

from app.exceptions import ExternalProviderError, ProviderNotConfiguredError
from app.keyword.providers.google_ads import (
    GoogleAdsKeywordMetrics,
    GoogleAdsKeywordMetricsProvider,
    MonthlySearchVolume,
)
from app.models import Keyword
from app.repositories.keyword_signal_repository import KeywordSignalRepository
from app.services.keyword_metrics_collection_service import (
    GOOGLE_ADS_BUNDLE_COMPONENTS,
    KeywordMetricsCollectionService,
)
from tests.support.google_ads_fakes import (
    FakeGoogleAdsProvider,
    dummy_google_ads_settings,
    unconfigured_settings,
)


def _metrics(keyword: str, *, avg: int | None = 1000) -> GoogleAdsKeywordMetrics:
    volumes = tuple(
        MonthlySearchVolume(year=2025, month=i + 1, monthly_searches=(avg or 0) + i * 20)
        for i in range(8)
    )
    return GoogleAdsKeywordMetrics(
        keyword=keyword,
        avg_monthly_searches=avg,
        monthly_search_volumes=volumes,
        competition="HIGH",
        competition_index=80,
        low_top_of_page_bid_micros=200_000_000,
        high_top_of_page_bid_micros=900_000_000,
    )


def _kw(session: Session, text: str) -> Keyword:
    entity = Keyword(keyword=text)
    entity.status = "analyzed"
    session.add(entity)
    session.flush()
    session.commit()
    return entity


def _service(session: Session, provider: object) -> KeywordMetricsCollectionService:
    return KeywordMetricsCollectionService(
        session, provider=provider, settings=dummy_google_ads_settings()
    )


def test_bundle_components_constant() -> None:
    assert GOOGLE_ADS_BUNDLE_COMPONENTS == (
        "search_demand",
        "commercial_intent",
        "trend",
    )


def test_single_bulk_fetch_for_multiple_keywords(session: Session) -> None:
    a = _kw(session, "AI 議事録 おすすめ")
    b = _kw(session, "ChatGPT 料金")
    provider = FakeGoogleAdsProvider(
        metrics=[_metrics("AI 議事録 おすすめ", avg=1000), _metrics("ChatGPT 料金", avg=8000)]
    )
    service = _service(session, provider)

    results = service.collect_google_ads_signals_bulk(
        [(a.id, GOOGLE_ADS_BUNDLE_COMPONENTS), (b.id, GOOGLE_ADS_BUNDLE_COMPONENTS)]
    )

    # keyword ごとに 3 回ではなく 1 回だけ provider を叩く
    assert provider.calls == [["AI 議事録 おすすめ", "ChatGPT 料金"]]
    assert len(results) == 2
    assert set(results[0].created) == {"search_demand", "commercial_intent", "trend"}

    repo = KeywordSignalRepository(session)
    for kw in (a, b):
        for component in GOOGLE_ADS_BUNDLE_COMPONENTS:
            assert repo.get_latest(kw.id, component) is not None


def test_bundle_values_match_single_collectors(session: Session) -> None:
    a = _kw(session, "AI 議事録 おすすめ")
    b = _kw(session, "AI 議事録 比較")
    m = _metrics("AI 議事録 おすすめ", avg=1000)

    # bulk
    _service(session, FakeGoogleAdsProvider(metrics=[m])).collect_google_ads_signals_bulk(
        [(a.id, GOOGLE_ADS_BUNDLE_COMPONENTS)]
    )
    # single (別 keyword、同じ metrics 形状)
    single_service = _service(
        session, FakeGoogleAdsProvider(metrics=[_metrics("AI 議事録 比較", avg=1000)])
    )
    single_service.collect_google_ads_search_demand(b.id)
    single_service.collect_google_ads_trend(b.id)

    repo = KeywordSignalRepository(session)
    assert (
        repo.get_latest(a.id, "search_demand").normalized_value
        == repo.get_latest(b.id, "search_demand").normalized_value
    )
    assert (
        repo.get_latest(a.id, "trend").normalized_value
        == repo.get_latest(b.id, "trend").normalized_value
    )
    # raw_data の normalizer metadata も一致
    assert (
        repo.get_latest(a.id, "search_demand").raw_data["normalizer"]
        == repo.get_latest(b.id, "search_demand").raw_data["normalizer"]
    )


def test_per_keyword_component_filter(session: Session) -> None:
    a = _kw(session, "AI 議事録 おすすめ")
    service = _service(
        session, FakeGoogleAdsProvider(metrics=[_metrics("AI 議事録 おすすめ", avg=1000)])
    )

    results = service.collect_google_ads_signals_bulk([(a.id, {"trend"})])

    assert set(results[0].created) == {"trend"}
    repo = KeywordSignalRepository(session)
    assert repo.get_latest(a.id, "trend") is not None
    assert repo.get_latest(a.id, "search_demand") is None
    assert repo.get_latest(a.id, "commercial_intent") is None


def test_keyword_without_metrics_is_skipped_not_failed_batch(session: Session) -> None:
    a = _kw(session, "AI 議事録 おすすめ")
    b = _kw(session, "unknown kw")
    # provider は a / c の 2 件だけ返す (len != 1 なので "unknown kw" は単一 fallback で拾われない)
    service = _service(
        session,
        FakeGoogleAdsProvider(
            metrics=[_metrics("AI 議事録 おすすめ", avg=1000), _metrics("other kw", avg=2000)]
        ),
    )

    results = service.collect_google_ads_signals_bulk(
        [(a.id, GOOGLE_ADS_BUNDLE_COMPONENTS), (b.id, GOOGLE_ADS_BUNDLE_COMPONENTS)]
    )
    by_kw = {r.keyword_id: r for r in results}
    assert set(by_kw[a.id].created) == {"search_demand", "commercial_intent", "trend"}
    assert by_kw[b.id].created == {}
    assert set(by_kw[b.id].skipped) == {"search_demand", "commercial_intent", "trend"}
    assert by_kw[b.id].skipped["search_demand"] == "no_metrics"


def test_no_avg_monthly_searches_skips_only_search_demand(session: Session) -> None:
    a = _kw(session, "kw noavg")
    service = _service(
        session, FakeGoogleAdsProvider(metrics=[_metrics("kw noavg", avg=None)])
    )
    [result] = service.collect_google_ads_signals_bulk(
        [(a.id, GOOGLE_ADS_BUNDLE_COMPONENTS)]
    )
    assert result.skipped["search_demand"] == "no_avg_monthly_searches"
    assert "commercial_intent" in result.created
    assert "trend" in result.created


def test_insufficient_months_skips_only_trend(session: Session) -> None:
    a = _kw(session, "kw shorthist")
    short = GoogleAdsKeywordMetrics(
        keyword="kw shorthist",
        avg_monthly_searches=500,
        monthly_search_volumes=(
            MonthlySearchVolume(year=2025, month=1, monthly_searches=500),
            MonthlySearchVolume(year=2025, month=2, monthly_searches=520),
        ),
        competition="LOW",
        competition_index=20,
        low_top_of_page_bid_micros=None,
        high_top_of_page_bid_micros=None,
    )
    service = _service(session, FakeGoogleAdsProvider(metrics=[short]))
    [result] = service.collect_google_ads_signals_bulk(
        [(a.id, GOOGLE_ADS_BUNDLE_COMPONENTS)]
    )
    assert result.skipped["trend"] == "insufficient_monthly_volumes"
    assert "search_demand" in result.created
    assert "commercial_intent" in result.created


def test_empty_requests_does_not_call_provider(session: Session) -> None:
    provider = FakeGoogleAdsProvider(metrics=[_metrics("x")])
    service = _service(session, provider)
    assert service.collect_google_ads_signals_bulk([]) == []
    assert provider.calls == []


def test_provider_error_propagates(session: Session) -> None:
    a = _kw(session, "kw err")
    provider = FakeGoogleAdsProvider(
        error=ExternalProviderError("google_ads", "Google Ads API request failed")
    )
    with pytest.raises(ExternalProviderError):
        _service(session, provider).collect_google_ads_signals_bulk(
            [(a.id, GOOGLE_ADS_BUNDLE_COMPONENTS)]
        )


def test_not_configured_propagates(session: Session) -> None:
    a = _kw(session, "kw nc")
    service = KeywordMetricsCollectionService(
        session,
        provider=GoogleAdsKeywordMetricsProvider(unconfigured_settings()),
        settings=unconfigured_settings(),
    )
    with pytest.raises(ProviderNotConfiguredError):
        service.collect_google_ads_signals_bulk([(a.id, GOOGLE_ADS_BUNDLE_COMPONENTS)])


# -- Phase 2C-1.1: CJK re-tokenised bulk response --------------------------
# 実 Google Ads E2E で観測した「分かち書きし直された」応答表記。
_CJK_ECHO = {
    "AI 議事録 おすすめ": "ai 議事 録 おすすめ",
    "ChatGPT 料金": "chatgpt 料金",
    "AI 業務効率化": "ai 業務 効率 化",
    "生成AI ツール 比較": "生成 ai ツール 比較",
    "RPA 比較": "rpa 比較",
}


def test_bulk_matches_cjk_retokenised_response_one_call(session: Session) -> None:
    requested = list(_CJK_ECHO)
    keywords = [_kw(session, text) for text in requested]
    provider = FakeGoogleAdsProvider(
        metrics=[_metrics(echo, avg=500 + i * 100) for i, echo in enumerate(_CJK_ECHO.values())]
    )
    service = _service(session, provider)

    results = service.collect_google_ads_signals_bulk(
        [(kw.id, GOOGLE_ADS_BUNDLE_COMPONENTS) for kw in keywords]
    )

    # provider は 1 回だけ / 5 keyword 全部で 3 component 生成
    assert provider.calls == [requested]
    assert len(provider.calls) == 1
    repo = KeywordSignalRepository(session)
    for kw in keywords:
        created = {r.keyword_id: r for r in results}[kw.id].created
        assert set(created) == {"search_demand", "commercial_intent", "trend"}
        for component in GOOGLE_ADS_BUNDLE_COMPONENTS:
            assert repo.get_latest(kw.id, component) is not None


def test_bulk_cjk_normalized_values_equal_single_collector(session: Session) -> None:
    # matching fix は照合だけを直す。normalized_value は既存 normalizer 結果と同一。
    bulk_kw = _kw(session, "生成AI ツール 比較")
    single_kw = _kw(session, "生成AI ツール 比較(単体)")
    echo = "生成 ai ツール 比較"

    _service(
        session, FakeGoogleAdsProvider(metrics=[_metrics(echo, avg=1234)])
    ).collect_google_ads_signals_bulk([(bulk_kw.id, GOOGLE_ADS_BUNDLE_COMPONENTS)])

    single = _service(session, FakeGoogleAdsProvider(metrics=[_metrics(echo, avg=1234)]))
    single.collect_google_ads_search_demand(single_kw.id)
    single.collect_google_ads_commercial_intent(single_kw.id)
    single.collect_google_ads_trend(single_kw.id)

    repo = KeywordSignalRepository(session)
    for component in GOOGLE_ADS_BUNDLE_COMPONENTS:
        assert (
            repo.get_latest(bulk_kw.id, component).normalized_value
            == repo.get_latest(single_kw.id, component).normalized_value
        )


def test_bulk_requested_side_compact_collision_is_not_guessed(session: Session) -> None:
    # requested 側で compact key が衝突 -> whitespace 無視の照合はせず、
    # 空白差だけの応答は誤割当しない (完全一致だけ許可)。
    a = _kw(session, "AI 議事録")
    b = _kw(session, "AI議事録")  # compact key が a と同じ
    provider = FakeGoogleAdsProvider(metrics=[_metrics("ai 議事 録", avg=800)])
    service = _service(session, provider)

    results = service.collect_google_ads_signals_bulk(
        [(a.id, GOOGLE_ADS_BUNDLE_COMPONENTS), (b.id, GOOGLE_ADS_BUNDLE_COMPONENTS)]
    )
    by_kw = {r.keyword_id: r for r in results}
    assert by_kw[a.id].created == {}
    assert by_kw[b.id].created == {}
    assert by_kw[a.id].skipped["search_demand"] == "no_metrics"
    # decoy として b に完全一致行を足すと b だけ拾える (決定論)
    provider2 = FakeGoogleAdsProvider(
        metrics=[_metrics("ai 議事 録", avg=800), _metrics("ai議事録", avg=900)]
    )
    results2 = _service(session, provider2).collect_google_ads_signals_bulk(
        [(a.id, GOOGLE_ADS_BUNDLE_COMPONENTS), (b.id, GOOGLE_ADS_BUNDLE_COMPONENTS)]
    )
    by_kw2 = {r.keyword_id: r for r in results2}
    assert set(by_kw2[b.id].created) == {"search_demand", "commercial_intent", "trend"}
    assert by_kw2[a.id].created == {}


def test_bulk_single_response_not_misassigned_via_fallback(session: Session) -> None:
    # 複数 requested に応答 1 件のみ。明示的に一致する keyword だけが metric を得る。
    match_kw = _kw(session, "ChatGPT 料金")
    other1 = _kw(session, "RPA 比較")
    other2 = _kw(session, "AI 業務効率化")
    provider = FakeGoogleAdsProvider(metrics=[_metrics("chatgpt 料金", avg=6600)])
    service = _service(session, provider)

    results = service.collect_google_ads_signals_bulk(
        [
            (match_kw.id, GOOGLE_ADS_BUNDLE_COMPONENTS),
            (other1.id, GOOGLE_ADS_BUNDLE_COMPONENTS),
            (other2.id, GOOGLE_ADS_BUNDLE_COMPONENTS),
        ]
    )
    by_kw = {r.keyword_id: r for r in results}
    assert set(by_kw[match_kw.id].created) == {"search_demand", "commercial_intent", "trend"}
    assert by_kw[other1.id].created == {}
    assert by_kw[other2.id].created == {}
    assert by_kw[other1.id].skipped["search_demand"] == "no_metrics"
    assert by_kw[other2.id].skipped["trend"] == "no_metrics"
