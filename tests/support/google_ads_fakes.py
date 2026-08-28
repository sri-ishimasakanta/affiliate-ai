"""Google Ads を実通信せずにテストするための fake / helper。

- ``FakeGoogleAdsClient``: Google Ads SDK client の最小形 (``_call_api`` 検証用)
- ``FakeGoogleAdsProvider``: Provider の最小形 (Collection Service / API 検証用)
- ``dummy_google_ads_settings``: dummy credential を持つ Settings

実 credential は絶対に書かない。dummy 値のみ。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.config.settings import Settings
from app.keyword.providers.google_ads import GoogleAdsKeywordMetrics


def dummy_google_ads_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "google_ads_developer_token": "dummy-developer-token",
        "google_ads_client_id": "dummy-client-id",
        "google_ads_client_secret": "dummy-client-secret",
        "google_ads_refresh_token": "dummy-refresh-token",
        "google_ads_customer_id": "1234567890",
    }
    base.update(overrides)
    return Settings(**base)


def unconfigured_settings() -> Settings:
    return Settings(
        google_ads_developer_token=None,
        google_ads_client_id=None,
        google_ads_client_secret=None,
        google_ads_refresh_token=None,
        google_ads_customer_id=None,
    )


# --- SDK client fake ------------------------------------------------------
class _FakeRequest:
    def __init__(self) -> None:
        self.customer_id: str | None = None
        self.keywords: list[str] = []
        self.geo_target_constants: list[str] = []
        self.language: str | None = None
        self.keyword_plan_network: str | None = None


class _FakeEnums:
    class KeywordPlanNetworkEnum:
        GOOGLE_SEARCH = "GOOGLE_SEARCH"


class FakeGoogleAdsClient:
    """`get_service` が返すサービスも自分自身が兼ねる最小 fake。"""

    def __init__(self, *, response: Any = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.captured_request: _FakeRequest | None = None
        self.enums = _FakeEnums()

    def get_service(self, name: str) -> FakeGoogleAdsClient:
        assert name == "KeywordPlanIdeaService"
        return self

    def get_type(self, name: str) -> _FakeRequest:
        assert name == "GenerateKeywordHistoricalMetricsRequest"
        return _FakeRequest()

    def generate_keyword_historical_metrics(self, *, request: _FakeRequest) -> Any:
        self.captured_request = request
        if self._error is not None:
            raise self._error
        return self._response


# --- response builders --------------------------------------------------
def month_volume(name: str, year: int, monthly_searches: int) -> SimpleNamespace:
    return SimpleNamespace(
        year=year,
        month=SimpleNamespace(name=name),
        monthly_searches=monthly_searches,
    )


def keyword_metrics(
    *,
    avg_monthly_searches: int | None = 1234,
    competition: str | None = "LOW",
    competition_index: int | None = 42,
    low_top_of_page_bid_micros: int | None = 100_000,
    high_top_of_page_bid_micros: int | None = 500_000,
    monthly_search_volumes: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        avg_monthly_searches=avg_monthly_searches,
        competition=(
            SimpleNamespace(name=competition) if competition is not None else None
        ),
        competition_index=competition_index,
        low_top_of_page_bid_micros=low_top_of_page_bid_micros,
        high_top_of_page_bid_micros=high_top_of_page_bid_micros,
        monthly_search_volumes=monthly_search_volumes or [],
    )


def historical_metrics_response(
    rows: list[tuple[str, SimpleNamespace | None]],
) -> SimpleNamespace:
    return SimpleNamespace(
        results=[
            SimpleNamespace(text=text, keyword_metrics=metrics)
            for text, metrics in rows
        ]
    )


# --- Provider fake -----------------------------------------------------
class FakeGoogleAdsProvider:
    def __init__(
        self,
        *,
        metrics: list[GoogleAdsKeywordMetrics] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._metrics = list(metrics) if metrics is not None else []
        self._error = error
        self.calls: list[list[str]] = []

    def fetch_historical_metrics(
        self,
        keywords: list[str],
    ) -> list[GoogleAdsKeywordMetrics]:
        self.calls.append(list(keywords))
        if self._error is not None:
            raise self._error
        return list(self._metrics)
