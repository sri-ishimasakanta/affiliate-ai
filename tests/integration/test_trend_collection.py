"""KeywordMetricsCollectionService の Google Ads trend 収集の検証。

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

_GROWTH = [100, 100, 100, 150, 150, 150]  # -> trend 70.0


def _volumes(
    values: list[int | None], *, start_year: int = 2025, start_month: int = 1
) -> tuple[MonthlySearchVolume, ...]:
    out: list[MonthlySearchVolume] = []
    year, month = start_year, start_month
    for value in values:
        out.append(MonthlySearchVolume(year=year, month=month, monthly_searches=value))
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return tuple(out)


def _metrics(
    keyword: str = "kw", *, values: list[int | None] | None = None
) -> GoogleAdsKeywordMetrics:
    return GoogleAdsKeywordMetrics(
        keyword=keyword,
        avg_monthly_searches=1000,
        monthly_search_volumes=_volumes(values if values is not None else _GROWTH),
        competition="HIGH",
        competition_index=80,
        low_top_of_page_bid_micros=200_000_000,
        high_top_of_page_bid_micros=900_000_000,
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


def test_collect_creates_trend_signal(session: Session) -> None:
    keyword = _make_keyword(session, "running shoes")
    provider = FakeGoogleAdsProvider(metrics=[_metrics("running shoes")])
    service = _service(session, provider)

    before = datetime.now(UTC).replace(tzinfo=None)
    read = service.collect_google_ads_trend(keyword.id)
    after = datetime.now(UTC).replace(tzinfo=None)

    assert read.component == KeywordSignalComponent.TREND
    assert read.provider == "google_ads"
    assert read.source_reference == GOOGLE_ADS_SOURCE_REFERENCE
    assert before <= _naive(read.observed_at) <= after
    assert provider.calls == [["running shoes"]]
    assert read.normalized_value == 70.0  # [100,100,100,150,150,150]

    raw = read.raw_data
    assert raw["previous_3_average"] == 100.0
    assert raw["recent_3_average"] == 150.0
    assert raw["change_ratio"] == 0.4
    assert raw["months_used"] == 6
    assert raw["available_months"] == 6
    assert raw["geo_target_id"] == 2392
    assert raw["language_id"] == 1005
    assert raw["normalizer_version"] == "v1"
    assert raw["normalizer"] == {"name": "trend", "version": "v1"}
    assert raw["monthly_search_volumes"][0] == {
        "year": 2025,
        "month": 1,
        "monthly_searches": 100,
    }
    assert len(raw["monthly_search_volumes"]) == 6
    # 秘密情報は保存しない
    assert "customer_id" not in raw
    assert "developer_token" not in raw

    # period は全 monthly data の最古〜最新月 (search_demand / commercial_intent と同じ)
    assert _naive(read.period_start) == datetime(2025, 1, 1)
    assert _naive(read.period_end) == datetime(2025, 6, 1)


def test_collect_uses_latest_6_months_when_more_available(session: Session) -> None:
    keyword = _make_keyword(session, "kw")
    values = [10, 20, 30, 100, 100, 100, 150, 150, 150]  # 9 か月
    service = _service(session, FakeGoogleAdsProvider(metrics=[_metrics("kw", values=values)]))

    read = service.collect_google_ads_trend(keyword.id)

    assert read.normalized_value == 70.0
    assert read.raw_data["available_months"] == 9
    assert read.raw_data["months_used"] == 6
    assert len(read.raw_data["monthly_search_volumes"]) == 6
    # period は全 9 か月分
    assert _naive(read.period_start) == datetime(2025, 1, 1)
    assert _naive(read.period_end) == datetime(2025, 9, 1)


def test_collect_insufficient_months_raises_data_error(session: Session) -> None:
    keyword = _make_keyword(session, "kw")
    service = _service(
        session, FakeGoogleAdsProvider(metrics=[_metrics("kw", values=[1, 2, 3, 4, 5])])
    )

    with pytest.raises(ExternalProviderDataError):
        service.collect_google_ads_trend(keyword.id)

    assert KeywordSignalRepository(session).list_by_keyword(keyword.id) == []


def test_collect_negative_month_raises_data_error(session: Session) -> None:
    keyword = _make_keyword(session, "kw")
    service = _service(
        session,
        FakeGoogleAdsProvider(
            metrics=[_metrics("kw", values=[100, 100, 100, 150, 150, -5])]
        ),
    )

    with pytest.raises(ExternalProviderDataError):
        service.collect_google_ads_trend(keyword.id)

    assert KeywordSignalRepository(session).list_by_keyword(keyword.id) == []


def test_collect_none_month_excluded_then_ok(session: Session) -> None:
    keyword = _make_keyword(session, "kw")
    values = [None, 100, 100, 100, 150, 150, 150]  # 有効 6
    service = _service(session, FakeGoogleAdsProvider(metrics=[_metrics("kw", values=values)]))

    read = service.collect_google_ads_trend(keyword.id)

    assert read.normalized_value == 70.0
    assert read.raw_data["available_months"] == 6


def test_collect_persists_and_is_retrievable(session: Session) -> None:
    keyword = _make_keyword(session, "kw")
    service = _service(session, FakeGoogleAdsProvider(metrics=[_metrics("kw")]))

    read = service.collect_google_ads_trend(keyword.id)
    session.rollback()  # commit 済みなら残る

    stored = KeywordSignalRepository(session).get_by_id(read.id)
    assert stored is not None
    assert stored.component == "trend"
    assert stored.provider == "google_ads"
    assert stored.normalized_value == read.normalized_value


def test_collect_does_not_change_keyword_status(session: Session) -> None:
    keyword = _make_keyword(session, "kw", status="discovered")
    service = _service(session, FakeGoogleAdsProvider(metrics=[_metrics("kw")]))

    service.collect_google_ads_trend(keyword.id)

    session.expire_all()
    assert session.get(Keyword, keyword.id).status == "discovered"


def test_collect_passes_keyword_text_to_provider(session: Session) -> None:
    keyword = _make_keyword(session, "AI 議事録 おすすめ")
    provider = FakeGoogleAdsProvider(metrics=[_metrics("AI 議事録 おすすめ")])

    _service(session, provider).collect_google_ads_trend(keyword.id)

    assert provider.calls == [["AI 議事録 おすすめ"]]


def test_collect_nonexistent_keyword_raises(session: Session) -> None:
    service = _service(session, FakeGoogleAdsProvider(metrics=[_metrics()]))
    with pytest.raises(EntityNotFoundError):
        service.collect_google_ads_trend(999999)


def test_collect_provider_not_configured_raises(session: Session) -> None:
    keyword = _make_keyword(session, "kw")
    service = KeywordMetricsCollectionService(
        session,
        provider=GoogleAdsKeywordMetricsProvider(unconfigured_settings()),
        settings=unconfigured_settings(),
    )

    with pytest.raises(ProviderNotConfiguredError):
        service.collect_google_ads_trend(keyword.id)

    assert KeywordSignalRepository(session).list_by_keyword(keyword.id) == []


def test_collect_provider_error_propagates(session: Session) -> None:
    keyword = _make_keyword(session, "kw")
    provider = FakeGoogleAdsProvider(
        error=ExternalProviderError("google_ads", "Google Ads API request failed")
    )

    with pytest.raises(ExternalProviderError):
        _service(session, provider).collect_google_ads_trend(keyword.id)


def test_collect_no_metrics_raises_data_error(session: Session) -> None:
    keyword = _make_keyword(session, "kw")
    service = _service(session, FakeGoogleAdsProvider(metrics=[]))

    with pytest.raises(ExternalProviderDataError):
        service.collect_google_ads_trend(keyword.id)

    assert KeywordSignalRepository(session).list_by_keyword(keyword.id) == []


def test_collect_commit_failure_rolls_back(session: Session, monkeypatch) -> None:
    keyword = _make_keyword(session, "kw")
    service = _service(session, FakeGoogleAdsProvider(metrics=[_metrics("kw")]))

    def _boom() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(session, "commit", _boom)
    with pytest.raises(RuntimeError):
        service.collect_google_ads_trend(keyword.id)

    monkeypatch.undo()
    assert KeywordSignalRepository(session).list_by_keyword(keyword.id) == []


def test_trend_coexists_with_search_demand_and_commercial_intent(session: Session) -> None:
    keyword = _make_keyword(session, "AI 議事録 比較")
    service = _service(
        session, FakeGoogleAdsProvider(metrics=[_metrics("AI 議事録 比較")])
    )

    sd = service.collect_google_ads_search_demand(keyword.id)
    ci = service.collect_google_ads_commercial_intent(keyword.id)
    tr = service.collect_google_ads_trend(keyword.id)

    repo = KeywordSignalRepository(session)
    assert repo.get_latest(keyword.id, "search_demand").id == sd.id
    assert repo.get_latest(keyword.id, "commercial_intent").id == ci.id
    assert repo.get_latest(keyword.id, "trend").id == tr.id
    assert tr.component == KeywordSignalComponent.TREND
    # trend collector は competition_ease Signal を作らない
    assert repo.get_latest(keyword.id, "competition_ease") is None
    # search_demand の normalized_value は従来どおり (回帰防止)
    assert sd.normalized_value == 60.01
    assert len(repo.list_by_keyword(keyword.id)) == 3
