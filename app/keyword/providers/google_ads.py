"""Google Ads Keyword Historical Metrics 連携。

責務:
- Google Ads client の生成
- ``KeywordPlanIdeaService.GenerateKeywordHistoricalMetrics`` の実行
- Google Ads レスポンスを **provider 固有の内部 DTO** へ変換

Opportunity Score 計算 / KeywordSignal の DB 保存 / status 変更は行わない。
Google Ads SDK のオブジェクト・enum を Service / Normalizer / DB 層へ漏らさない。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.config.settings import Settings
from app.exceptions import ExternalProviderError, ProviderNotConfiguredError

_PROVIDER = "google_ads"

# KeywordSignal.source_reference に入れる安定識別子。外部 URL は生成しない。
GOOGLE_ADS_SOURCE_REFERENCE = "google-ads:keyword-plan-idea:historical-metrics"

_MONTH_NAME_TO_INT = {
    "JANUARY": 1,
    "FEBRUARY": 2,
    "MARCH": 3,
    "APRIL": 4,
    "MAY": 5,
    "JUNE": 6,
    "JULY": 7,
    "AUGUST": 8,
    "SEPTEMBER": 9,
    "OCTOBER": 10,
    "NOVEMBER": 11,
    "DECEMBER": 12,
}


# --- 内部 DTO ---------------------------------------------------------------
@dataclass(frozen=True)
class MonthlySearchVolume:
    year: int
    month: int  # 1-12 (0 = 不明)
    monthly_searches: int


@dataclass(frozen=True)
class GoogleAdsKeywordMetrics:
    keyword: str
    avg_monthly_searches: int | None
    monthly_search_volumes: tuple[MonthlySearchVolume, ...] = ()
    competition: str | None = None
    competition_index: int | None = None
    low_top_of_page_bid_micros: int | None = None
    high_top_of_page_bid_micros: int | None = None


@dataclass(frozen=True)
class GoogleAdsRequestParams:
    """SDK に依存しない request パラメータ表現 (テスト・検証用)。"""

    customer_id: str
    keywords: tuple[str, ...]
    geo_target_constants: tuple[str, ...]
    language: str
    keyword_plan_network: str = "GOOGLE_SEARCH"
    extras: dict[str, Any] = field(default_factory=dict)


# --- 変換ヘルパ (SDK enum/object -> primitive) ----------------------------
def _opt_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _enum_name(value: object) -> str | None:
    if value is None:
        return None
    name = getattr(value, "name", None)
    return str(name) if name is not None else str(value)


def _month_to_int(value: object) -> int:
    if value is None:
        return 0
    name = getattr(value, "name", None)
    if isinstance(name, str) and name in _MONTH_NAME_TO_INT:
        return _MONTH_NAME_TO_INT[name]
    number = _opt_int(value)
    if number is None:
        return 0
    # Google Ads MonthOfYear enum: JANUARY=2 .. DECEMBER=13
    if 2 <= number <= 13:
        return number - 1
    if 1 <= number <= 12:
        return number
    return 0


def _map_monthly_volume(item: object) -> MonthlySearchVolume:
    return MonthlySearchVolume(
        year=_opt_int(getattr(item, "year", None)) or 0,
        month=_month_to_int(getattr(item, "month", None)),
        monthly_searches=_opt_int(getattr(item, "monthly_searches", None)) or 0,
    )


class GoogleAdsKeywordMetricsProvider:
    def __init__(self, settings: Settings, *, client: object | None = None) -> None:
        self._settings = settings
        self._client = client  # 明示注入時はそれを使う (テスト用)。None なら遅延生成。

    # -- public ---------------------------------------------------------
    def build_request_params(self, keywords: Sequence[str]) -> GoogleAdsRequestParams:
        self._require_configured()
        cleaned = tuple(k for k in keywords if k and k.strip())
        if not cleaned:
            raise ValueError("keywords must contain at least one non-empty value")
        return GoogleAdsRequestParams(
            customer_id=str(self._settings.google_ads_customer_id),
            keywords=cleaned,
            geo_target_constants=(
                f"geoTargetConstants/{self._settings.google_ads_geo_target_id}",
            ),
            language=f"languageConstants/{self._settings.google_ads_language_id}",
            keyword_plan_network="GOOGLE_SEARCH",
        )

    def fetch_historical_metrics(
        self,
        keywords: Sequence[str],
    ) -> list[GoogleAdsKeywordMetrics]:
        """複数 keyword を受け取れる。今回の API 収集は 1 keyword 単位で呼ぶ。"""

        params = self.build_request_params(keywords)
        client = self._client if self._client is not None else self._build_client()
        try:
            response = self._call_api(client, params)
        except (ProviderNotConfiguredError, ExternalProviderError):
            raise
        except Exception as exc:  # SDK/credential/token 等の内部詳細は露出させない
            raise ExternalProviderError(
                _PROVIDER, "Google Ads API request failed"
            ) from exc
        return self._map_response(response)

    # -- internal -----------------------------------------------------
    def _require_configured(self) -> None:
        if not self._settings.google_ads_configured:
            raise ProviderNotConfiguredError(_PROVIDER)

    def _build_client(self) -> object:
        self._require_configured()
        try:
            from google.ads.googleads.client import GoogleAdsClient  # 遅延 import
        except ImportError as exc:  # pragma: no cover - 依存未導入時のみ
            raise ExternalProviderError(
                _PROVIDER, "Google Ads client library is not available"
            ) from exc

        config: dict[str, Any] = {
            "developer_token": self._settings.google_ads_developer_token,
            "client_id": self._settings.google_ads_client_id,
            "client_secret": self._settings.google_ads_client_secret,
            "refresh_token": self._settings.google_ads_refresh_token,
            "use_proto_plus": True,
        }
        if self._settings.google_ads_login_customer_id:
            config["login_customer_id"] = str(
                self._settings.google_ads_login_customer_id
            )
        try:
            return GoogleAdsClient.load_from_dict(config)
        except Exception as exc:
            raise ExternalProviderError(
                _PROVIDER, "Failed to initialise Google Ads client"
            ) from exc

    def _call_api(self, client: Any, params: GoogleAdsRequestParams) -> Any:
        service = client.get_service("KeywordPlanIdeaService")
        request = client.get_type("GenerateKeywordHistoricalMetricsRequest")
        request.customer_id = params.customer_id
        request.keywords.extend(params.keywords)
        request.geo_target_constants.extend(params.geo_target_constants)
        request.language = params.language
        request.keyword_plan_network = (
            client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH
        )
        return service.generate_keyword_historical_metrics(request=request)

    def _map_response(self, response: Any) -> list[GoogleAdsKeywordMetrics]:
        mapped: list[GoogleAdsKeywordMetrics] = []
        for row in getattr(response, "results", None) or []:
            metrics = getattr(row, "keyword_metrics", None)
            volumes = getattr(metrics, "monthly_search_volumes", None) or []
            mapped.append(
                GoogleAdsKeywordMetrics(
                    keyword=str(getattr(row, "text", "") or ""),
                    avg_monthly_searches=_opt_int(
                        getattr(metrics, "avg_monthly_searches", None)
                    ),
                    monthly_search_volumes=tuple(
                        _map_monthly_volume(v) for v in volumes
                    ),
                    competition=_enum_name(getattr(metrics, "competition", None)),
                    competition_index=_opt_int(
                        getattr(metrics, "competition_index", None)
                    ),
                    low_top_of_page_bid_micros=_opt_int(
                        getattr(metrics, "low_top_of_page_bid_micros", None)
                    ),
                    high_top_of_page_bid_micros=_opt_int(
                        getattr(metrics, "high_top_of_page_bid_micros", None)
                    ),
                )
            )
        return mapped
