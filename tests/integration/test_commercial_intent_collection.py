"""KeywordMetricsCollectionService の Google Ads commercial_intent 収集の検証。

Google Ads への実通信はしない (Fake provider / dummy Settings のみ)。
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.exceptions import (
    EntityNotFoundError,
    ExternalProviderDataError,
    ExternalProviderError,
    ProviderNotConfiguredError,
)
from app.keyword.providers.google_ads import (
    GOOGLE_ADS_SOURCE_REFERENCE,
    GoogleAdsKeywordMetrics,
    GoogleAdsKeywordMetricsProvider,
    MonthlySearchVolume,
)
from app.models import Keyword
from app.models.enums import KeywordSignalComponent
from app.repositories.keyword_signal_repository import KeywordSignalRepository
from app.services.keyword_metrics_collection_service import (
    KeywordMetricsCollectionService,
)
from tests.support.google_ads_fakes import (
    FakeGoogleAdsProvider,
    dummy_google_ads_settings,
    unconfigured_settings,
)


def _yen(amount: int) -> int:
    return amount * 1_000_000


def _metrics(
    keyword: str = "kw",
    *,
    low_micros: int | None = _yen(250),
    high_micros: int | None = _yen(4000),
    competition_index: int | None = 80,
    competition: str | None = "HIGH",
) -> GoogleAdsKeywordMetrics:
    return GoogleAdsKeywordMetrics(
        keyword=keyword,
        avg_monthly_searches=1000,
        monthly_search_volumes=(
            MonthlySearchVolume(year=2024, month=11, monthly_searches=900),
            MonthlySearchVolume(year=2025, month=2, monthly_searches=1100),
        ),
        competition=competition,
        competition_index=competition_index,
        low_top_of_page_bid_micros=low_micros,
        high_top_of_page_bid_micros=high_micros,
    )


def _make_keyword(session: Session, text: str = "kw", *, status: str | None = None) -> Keyword:
    entity = Keyword(keyword=text)
    if status is not None:
        entity.status = status
    session.add(entity)
    session.flush()
    session.commit()
    return entity


def _service(session: Session, provider: object) -> KeywordMetricsCollectionService:
    return KeywordMetricsCollectionService(
        session, provider=provider, settings=dummy_google_ads_settings()
    )


def _naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def test_collect_creates_commercial_intent_signal(session: Session) -> None:
    keyword = _make_keyword(session, "AI 議事録 比較")
    provider = FakeGoogleAdsProvider(metrics=[_metrics("AI 議事録 比較")])
    service = _service(session, provider)

    before = datetime.now(UTC).replace(tzinfo=None)
    read = service.collect_google_ads_commercial_intent(keyword.id)
    after = datetime.now(UTC).replace(tzinfo=None)

    assert read.component == KeywordSignalComponent.COMMERCIAL_INTENT
    assert read.provider == "google_ads"
    assert read.source_reference == GOOGLE_ADS_SOURCE_REFERENCE
    assert before <= _naive(read.observed_at) <= after
    assert provider.calls == [["AI 議事録 比較"]]

    # compare(90) / cpc 250円(63.21) / competition 80  -> coverage 1.0
    assert read.normalized_value == pytest.approx(80.96, abs=0.01)

    raw = read.raw_data
    assert raw["query_intent_type"] == "compare"
    assert raw["query_intent_score"] == 90.0
    assert raw["cpc_score"] == 63.21
    assert raw["ad_competition_score"] == 80.0
    assert raw["query_intent_weight"] == 0.60
    assert raw["cpc_weight"] == 0.30
    assert raw["ad_competition_weight"] == 0.10
    assert raw["available_weight"] == 1.0
    assert raw["evidence_coverage"] == 1.0
    assert raw["market_evidence_available"] is True
    assert raw["low_top_of_page_bid_micros"] == _yen(250)
    assert raw["high_top_of_page_bid_micros"] == _yen(4000)
    assert raw["competition"] == "HIGH"
    assert raw["competition_index"] == 80
    assert raw["geo_target_id"] == 2392
    assert raw["language_id"] == 1005
    assert raw["currency_assumption"] == "JPY"
    assert raw["normalizer_version"] == "v1"
    assert raw["normalizer"] == {"name": "commercial_intent", "version": "v1"}

    # period は search_demand collector と同じく monthly volumes の最古〜最新月
    assert _naive(read.period_start) == datetime(2024, 11, 1)
    assert _naive(read.period_end) == datetime(2025, 2, 1)


def test_collect_without_market_data_uses_query_only(session: Session) -> None:
    keyword = _make_keyword(session, "生成AI とは")
    provider = FakeGoogleAdsProvider(
        metrics=[_metrics("生成AI とは", low_micros=None, competition_index=None)]
    )

    read = _service(session, provider).collect_google_ads_commercial_intent(keyword.id)

    assert read.normalized_value == 10.0  # informational, query-only
    assert read.raw_data["cpc_score"] is None
    assert read.raw_data["ad_competition_score"] is None
    assert read.raw_data["evidence_coverage"] == 0.6
    assert read.raw_data["market_evidence_available"] is False


def test_collect_persists_and_is_retrievable(session: Session) -> None:
    keyword = _make_keyword(session, "kw")
    service = _service(session, FakeGoogleAdsProvider(metrics=[_metrics("kw")]))

    read = service.collect_google_ads_commercial_intent(keyword.id)
    session.rollback()  # commit 済みなら残る

    stored = KeywordSignalRepository(session).get_by_id(read.id)
    assert stored is not None
    assert stored.component == "commercial_intent"
    assert stored.provider == "google_ads"


def test_collect_does_not_change_keyword_status(session: Session) -> None:
    keyword = _make_keyword(session, "kw", status="discovered")
    service = _service(session, FakeGoogleAdsProvider(metrics=[_metrics("kw")]))

    service.collect_google_ads_commercial_intent(keyword.id)

    session.expire_all()
    assert session.get(Keyword, keyword.id).status == "discovered"


def test_collect_passes_keyword_text_to_provider(session: Session) -> None:
    keyword = _make_keyword(session, "ChatGPT 料金")
    provider = FakeGoogleAdsProvider(metrics=[_metrics("ChatGPT 料金")])

    _service(session, provider).collect_google_ads_commercial_intent(keyword.id)

    assert provider.calls == [["ChatGPT 料金"]]


def test_collect_nonexistent_keyword_raises(session: Session) -> None:
    service = _service(session, FakeGoogleAdsProvider(metrics=[_metrics()]))
    with pytest.raises(EntityNotFoundError):
        service.collect_google_ads_commercial_intent(999999)


def test_collect_provider_not_configured_raises(session: Session) -> None:
    keyword = _make_keyword(session, "kw")
    service = KeywordMetricsCollectionService(
        session,
        provider=GoogleAdsKeywordMetricsProvider(unconfigured_settings()),
        settings=unconfigured_settings(),
    )

    with pytest.raises(ProviderNotConfiguredError):
        service.collect_google_ads_commercial_intent(keyword.id)

    assert KeywordSignalRepository(session).list_by_keyword(keyword.id) == []


def test_collect_provider_error_propagates(session: Session) -> None:
    keyword = _make_keyword(session, "kw")
    provider = FakeGoogleAdsProvider(
        error=ExternalProviderError("google_ads", "Google Ads API request failed")
    )

    with pytest.raises(ExternalProviderError):
        _service(session, provider).collect_google_ads_commercial_intent(keyword.id)


def test_collect_no_metrics_raises_data_error(session: Session) -> None:
    keyword = _make_keyword(session, "kw")
    service = _service(session, FakeGoogleAdsProvider(metrics=[]))

    with pytest.raises(ExternalProviderDataError):
        service.collect_google_ads_commercial_intent(keyword.id)

    assert KeywordSignalRepository(session).list_by_keyword(keyword.id) == []


def test_collect_commit_failure_rolls_back(session: Session, monkeypatch) -> None:
    keyword = _make_keyword(session, "kw")
    service = _service(session, FakeGoogleAdsProvider(metrics=[_metrics("kw")]))

    def _boom() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(session, "commit", _boom)
    with pytest.raises(RuntimeError):
        service.collect_google_ads_commercial_intent(keyword.id)

    monkeypatch.undo()
    assert KeywordSignalRepository(session).list_by_keyword(keyword.id) == []


def test_search_demand_and_commercial_intent_coexist(session: Session) -> None:
    # 既存 search_demand collector と同居できる (competition_ease は作らない)
    keyword = _make_keyword(session, "AI 議事録 比較")
    service = _service(
        session, FakeGoogleAdsProvider(metrics=[_metrics("AI 議事録 比較")])
    )

    sd = service.collect_google_ads_search_demand(keyword.id)
    ci = service.collect_google_ads_commercial_intent(keyword.id)

    repo = KeywordSignalRepository(session)
    assert repo.get_latest(keyword.id, "search_demand").id == sd.id
    assert repo.get_latest(keyword.id, "commercial_intent").id == ci.id
    assert sd.component == KeywordSignalComponent.SEARCH_DEMAND
    assert ci.component == KeywordSignalComponent.COMMERCIAL_INTENT
    # commercial_intent collector は competition_ease Signal を作らない
    assert repo.get_latest(keyword.id, "competition_ease") is None
    # search_demand の normalized_value は従来どおり (回帰防止)
    assert sd.normalized_value == 60.01
