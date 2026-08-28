"""KeywordMetricsCollectionService (Google Ads search_demand 収集) の検証。"""

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
from app.keyword.schemas import KeywordSignalCreate
from app.models import Keyword
from app.models.enums import KeywordSignalComponent
from app.repositories.keyword_signal_repository import KeywordSignalRepository
from app.services.keyword_metrics_collection_service import (
    KeywordMetricsCollectionService,
)
from app.services.keyword_signal_service import KeywordSignalService
from tests.support.google_ads_fakes import (
    FakeGoogleAdsProvider,
    dummy_google_ads_settings,
    unconfigured_settings,
)


def _metrics(
    keyword: str = "kw",
    *,
    avg: int | None = 1000,
    volumes: list[MonthlySearchVolume] | None = None,
) -> GoogleAdsKeywordMetrics:
    return GoogleAdsKeywordMetrics(
        keyword=keyword,
        avg_monthly_searches=avg,
        monthly_search_volumes=tuple(
            volumes
            or [
                MonthlySearchVolume(year=2024, month=11, monthly_searches=900),
                MonthlySearchVolume(year=2025, month=1, monthly_searches=1100),
                MonthlySearchVolume(year=2025, month=3, monthly_searches=1000),
            ]
        ),
        competition="HIGH",
        competition_index=88,
        low_top_of_page_bid_micros=200_000,
        high_top_of_page_bid_micros=900_000,
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
    # SQLite は tz を保持しないため比較用に naive へ正規化する
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def test_collect_creates_search_demand_signal(session: Session) -> None:
    keyword = _make_keyword(session, "running shoes")
    provider = FakeGoogleAdsProvider(metrics=[_metrics("running shoes", avg=1000)])
    service = _service(session, provider)

    before = datetime.now(UTC).replace(tzinfo=None)
    read = service.collect_google_ads_search_demand(keyword.id)
    after = datetime.now(UTC).replace(tzinfo=None)

    assert read.component == KeywordSignalComponent.SEARCH_DEMAND
    assert read.provider == "google_ads"
    assert read.normalized_value == 60.01  # normalize_search_demand(1000)
    assert read.source_reference == GOOGLE_ADS_SOURCE_REFERENCE
    assert before <= _naive(read.observed_at) <= after
    assert provider.calls == [["running shoes"]]

    # raw_data の内容
    raw = read.raw_data
    assert raw["avg_monthly_searches"] == 1000
    assert raw["competition"] == "HIGH"
    assert raw["competition_index"] == 88
    assert raw["low_top_of_page_bid_micros"] == 200_000
    assert raw["high_top_of_page_bid_micros"] == 900_000
    assert raw["geo_target_id"] == 2392
    assert raw["language_id"] == 1005
    assert raw["normalizer"] == {"name": "search_demand", "version": "v1"}
    assert raw["monthly_search_volumes"][0] == {
        "year": 2024,
        "month": 11,
        "monthly_searches": 900,
    }

    # period_start / period_end = 最古月 〜 最新月
    assert _naive(read.period_start) == datetime(2024, 11, 1)
    assert _naive(read.period_end) == datetime(2025, 3, 1)


def test_collect_persists_and_is_retrievable(session: Session) -> None:
    keyword = _make_keyword(session, "kw")
    service = _service(session, FakeGoogleAdsProvider(metrics=[_metrics("kw")]))

    read = service.collect_google_ads_search_demand(keyword.id)
    session.rollback()  # commit 済みなら残る

    stored = KeywordSignalRepository(session).get_by_id(read.id)
    assert stored is not None
    assert stored.provider == "google_ads"


def test_collect_moves_discovered_keyword_is_not_done_here(session: Session) -> None:
    # collector は status を変えない (score 作成時のみ)
    keyword = _make_keyword(session, "kw", status="discovered")
    service = _service(session, FakeGoogleAdsProvider(metrics=[_metrics("kw")]))

    service.collect_google_ads_search_demand(keyword.id)

    session.expire_all()
    assert session.get(Keyword, keyword.id).status == "discovered"


def test_collect_nonexistent_keyword_raises(session: Session) -> None:
    service = _service(session, FakeGoogleAdsProvider(metrics=[_metrics()]))
    with pytest.raises(EntityNotFoundError):
        service.collect_google_ads_search_demand(999999)


def test_collect_provider_not_configured_raises(session: Session) -> None:
    keyword = _make_keyword(session, "kw")
    real_provider = GoogleAdsKeywordMetricsProvider(unconfigured_settings())
    service = KeywordMetricsCollectionService(
        session, provider=real_provider, settings=unconfigured_settings()
    )

    with pytest.raises(ProviderNotConfiguredError):
        service.collect_google_ads_search_demand(keyword.id)

    assert KeywordSignalRepository(session).list_by_keyword(keyword.id) == []


def test_collect_provider_communication_failure_propagates(session: Session) -> None:
    keyword = _make_keyword(session, "kw")
    provider = FakeGoogleAdsProvider(
        error=ExternalProviderError("google_ads", "Google Ads API request failed")
    )
    service = _service(session, provider)

    with pytest.raises(ExternalProviderError):
        service.collect_google_ads_search_demand(keyword.id)


def test_collect_no_metrics_raises_data_error(session: Session) -> None:
    keyword = _make_keyword(session, "kw")
    service = _service(session, FakeGoogleAdsProvider(metrics=[]))

    with pytest.raises(ExternalProviderDataError):
        service.collect_google_ads_search_demand(keyword.id)

    assert KeywordSignalRepository(session).list_by_keyword(keyword.id) == []


def test_collect_metrics_without_avg_raises_data_error(session: Session) -> None:
    keyword = _make_keyword(session, "kw")
    service = _service(
        session, FakeGoogleAdsProvider(metrics=[_metrics("kw", avg=None)])
    )

    with pytest.raises(ExternalProviderDataError):
        service.collect_google_ads_search_demand(keyword.id)


def test_collect_commit_failure_rolls_back(session: Session, monkeypatch) -> None:
    keyword = _make_keyword(session, "kw")
    service = _service(session, FakeGoogleAdsProvider(metrics=[_metrics("kw")]))

    def _boom() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(session, "commit", _boom)
    with pytest.raises(RuntimeError):
        service.collect_google_ads_search_demand(keyword.id)

    monkeypatch.undo()
    assert KeywordSignalRepository(session).list_by_keyword(keyword.id) == []


def test_recollect_appends_history_and_latest_is_newest_observed(session: Session) -> None:
    keyword = _make_keyword(session, "kw")

    # 先に古い observed_at の manual signal を入れる
    KeywordSignalService(session).create_signal(
        keyword.id,
        KeywordSignalCreate(
            component="search_demand",
            normalized_value=5.0,
            provider="manual",
            observed_at=datetime(2020, 1, 1, tzinfo=UTC),
        ),
    )

    provider = FakeGoogleAdsProvider(metrics=[_metrics("kw", avg=100)])
    service = _service(session, provider)
    service.collect_google_ads_search_demand(keyword.id)
    service.collect_google_ads_search_demand(keyword.id)

    repo = KeywordSignalRepository(session)
    history = repo.list_by_component(keyword.id, "search_demand")
    assert len(history) == 3  # manual 1 + google_ads 2

    latest = repo.get_latest(keyword.id, "search_demand")
    assert latest.provider == "google_ads"
    assert latest.normalized_value == 40.09  # normalize_search_demand(100)
