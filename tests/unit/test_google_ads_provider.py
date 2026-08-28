"""GoogleAdsKeywordMetricsProvider の unit テスト (Google Ads 実通信なし)。"""

from types import SimpleNamespace

import pytest

from app.exceptions import ExternalProviderError, ProviderNotConfiguredError
from app.keyword.providers.google_ads import (
    GoogleAdsKeywordMetrics,
    GoogleAdsKeywordMetricsProvider,
    MonthlySearchVolume,
)
from tests.support.google_ads_fakes import (
    FakeGoogleAdsClient,
    dummy_google_ads_settings,
    historical_metrics_response,
    keyword_metrics,
    month_volume,
    unconfigured_settings,
)


def _provider(client: object | None = None, **settings_overrides: object):
    return GoogleAdsKeywordMetricsProvider(
        dummy_google_ads_settings(**settings_overrides), client=client
    )


# -- request mapping --------------------------------------------------
def test_build_request_params_maps_settings() -> None:
    provider = _provider()

    params = provider.build_request_params(["  ", "iphone 15", "  macbook  "])

    assert params.customer_id == "1234567890"
    assert params.keywords == ("iphone 15", "  macbook  ")
    assert params.geo_target_constants == ("geoTargetConstants/2392",)
    assert params.language == "languageConstants/1005"
    assert params.keyword_plan_network == "GOOGLE_SEARCH"


def test_build_request_params_custom_geo_language() -> None:
    provider = _provider(google_ads_geo_target_id=2840, google_ads_language_id=1000)
    params = provider.build_request_params(["kw"])
    assert params.geo_target_constants == ("geoTargetConstants/2840",)
    assert params.language == "languageConstants/1000"


def test_build_request_params_empty_keywords_raises() -> None:
    with pytest.raises(ValueError, match="at least one"):
        _provider().build_request_params(["", "   "])


def test_unconfigured_provider_raises_not_configured() -> None:
    provider = GoogleAdsKeywordMetricsProvider(unconfigured_settings())
    with pytest.raises(ProviderNotConfiguredError) as exc:
        provider.build_request_params(["kw"])
    assert exc.value.provider == "google_ads"


def test_call_api_populates_sdk_request() -> None:
    client = FakeGoogleAdsClient(response=historical_metrics_response([]))
    provider = _provider(client=client)

    provider.fetch_historical_metrics(["running shoes"])

    req = client.captured_request
    assert req is not None
    assert req.customer_id == "1234567890"
    assert list(req.keywords) == ["running shoes"]
    assert list(req.geo_target_constants) == ["geoTargetConstants/2392"]
    assert req.language == "languageConstants/1005"
    assert req.keyword_plan_network == "GOOGLE_SEARCH"


def test_provider_accepts_multiple_keywords() -> None:
    client = FakeGoogleAdsClient(response=historical_metrics_response([]))
    provider = _provider(client=client)
    provider.fetch_historical_metrics(["a", "b", "c"])
    assert list(client.captured_request.keywords) == ["a", "b", "c"]


# -- response mapping ----------------------------------------------
def test_map_response_to_primitive_dto() -> None:
    response = historical_metrics_response(
        [
            (
                "running shoes",
                keyword_metrics(
                    avg_monthly_searches=8100,
                    competition="MEDIUM",
                    competition_index=55,
                    low_top_of_page_bid_micros=120_000,
                    high_top_of_page_bid_micros=640_000,
                    monthly_search_volumes=[
                        month_volume("JANUARY", 2025, 7000),
                        month_volume("FEBRUARY", 2025, 9000),
                    ],
                ),
            )
        ]
    )
    provider = _provider(client=FakeGoogleAdsClient(response=response))

    [metrics] = provider.fetch_historical_metrics(["running shoes"])

    assert isinstance(metrics, GoogleAdsKeywordMetrics)
    assert metrics.keyword == "running shoes"
    assert metrics.avg_monthly_searches == 8100
    assert metrics.competition == "MEDIUM"  # enum -> plain string
    assert metrics.competition_index == 55
    assert metrics.low_top_of_page_bid_micros == 120_000
    assert metrics.high_top_of_page_bid_micros == 640_000
    assert metrics.monthly_search_volumes == (
        MonthlySearchVolume(year=2025, month=1, monthly_searches=7000),
        MonthlySearchVolume(year=2025, month=2, monthly_searches=9000),
    )
    # すべて primitive (JSON-safe)
    for volume in metrics.monthly_search_volumes:
        assert isinstance(volume.year, int)
        assert isinstance(volume.month, int)
        assert isinstance(volume.monthly_searches, int)


def test_map_response_handles_missing_and_none_fields() -> None:
    response = historical_metrics_response(
        [
            ("no metrics kw", None),
            (
                "partial kw",
                SimpleNamespace(
                    avg_monthly_searches=None,
                    competition=None,
                    competition_index=None,
                    low_top_of_page_bid_micros=None,
                    high_top_of_page_bid_micros=None,
                    monthly_search_volumes=[],
                ),
            ),
        ]
    )
    provider = _provider(client=FakeGoogleAdsClient(response=response))

    first, second = provider.fetch_historical_metrics(["a", "b"])

    assert first.keyword == "no metrics kw"
    assert first.avg_monthly_searches is None
    assert first.monthly_search_volumes == ()
    assert first.competition is None
    assert second.avg_monthly_searches is None
    assert second.competition_index is None


def test_map_response_month_enum_int_fallback() -> None:
    # month が enum int (JANUARY=2) で来た場合も 1..12 に正規化される
    volume = SimpleNamespace(year=2024, month=2, monthly_searches=500)
    response = historical_metrics_response(
        [("kw", keyword_metrics(monthly_search_volumes=[volume]))]
    )
    provider = _provider(client=FakeGoogleAdsClient(response=response))

    [metrics] = provider.fetch_historical_metrics(["kw"])
    assert metrics.monthly_search_volumes[0].month == 1


def test_empty_results_returns_empty_list() -> None:
    provider = _provider(client=FakeGoogleAdsClient(response=SimpleNamespace(results=[])))
    assert provider.fetch_historical_metrics(["kw"]) == []


# -- API exception conversion ---------------------------------------
def test_sdk_exception_is_wrapped_without_leaking_details() -> None:
    secret = "developer_token=SUPER_SECRET_TOKEN refresh_token=RT"
    client = FakeGoogleAdsClient(error=RuntimeError(secret))
    provider = _provider(client=client)

    with pytest.raises(ExternalProviderError) as exc:
        provider.fetch_historical_metrics(["kw"])

    assert exc.value.provider == "google_ads"
    assert "SUPER_SECRET_TOKEN" not in str(exc.value)
    assert str(exc.value) == "google_ads: Google Ads API request failed"
    # 元例外は __cause__ にのみ保持される
    assert isinstance(exc.value.__cause__, RuntimeError)
