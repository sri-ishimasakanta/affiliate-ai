"""Google Ads search_demand collector API の統合テスト (実通信なし)。"""

from collections.abc import Callable, Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.dependencies import get_keyword_metrics_collection_service
from app.config.database import get_session
from app.exceptions import ExternalProviderError
from app.keyword.providers.google_ads import (
    GoogleAdsKeywordMetrics,
    GoogleAdsKeywordMetricsProvider,
    MonthlySearchVolume,
)
from app.main import app
from app.services.keyword_metrics_collection_service import (
    KeywordMetricsCollectionService,
)
from tests.support.google_ads_fakes import (
    FakeGoogleAdsProvider,
    dummy_google_ads_settings,
    unconfigured_settings,
)

_URL = "/api/v1/keywords/{kid}/signals/google-ads/search-demand"
_CI_URL = "/api/v1/keywords/{kid}/signals/google-ads/commercial-intent"
_TREND_URL = "/api/v1/keywords/{kid}/signals/google-ads/trend"

UseCollector = Callable[..., TestClient]


def _metrics(keyword: str, *, avg: int | None = 1000) -> GoogleAdsKeywordMetrics:
    return GoogleAdsKeywordMetrics(
        keyword=keyword,
        avg_monthly_searches=avg,
        monthly_search_volumes=(
            MonthlySearchVolume(year=2025, month=1, monthly_searches=avg or 0),
        ),
        competition="HIGH",
        competition_index=90,
        low_top_of_page_bid_micros=100_000,
        high_top_of_page_bid_micros=800_000,
    )


def _trend_metrics(
    keyword: str, *, values: list[int] | None = None
) -> GoogleAdsKeywordMetrics:
    """trend 計算に足りる 6 か月以上の monthly_search_volumes を持つ metrics。"""

    series = values if values is not None else [100, 100, 100, 150, 150, 150]
    volumes = tuple(
        MonthlySearchVolume(year=2025, month=i + 1, monthly_searches=v)
        for i, v in enumerate(series)
    )
    return GoogleAdsKeywordMetrics(
        keyword=keyword,
        avg_monthly_searches=1000,
        monthly_search_volumes=volumes,
        competition="HIGH",
        competition_index=90,
        low_top_of_page_bid_micros=100_000,
        high_top_of_page_bid_micros=800_000,
    )


@pytest.fixture
def collector_api(session: Session) -> Generator[UseCollector, None, None]:
    """provider を差し替えて collector API を叩く helper を返す。"""

    clients: list[TestClient] = []

    def _session_override() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[get_session] = _session_override

    def _use(provider: object, *, settings: object | None = None) -> TestClient:
        def _svc_override() -> KeywordMetricsCollectionService:
            return KeywordMetricsCollectionService(
                session,
                provider=provider,
                settings=settings or dummy_google_ads_settings(),
            )

        app.dependency_overrides[get_keyword_metrics_collection_service] = _svc_override
        client = TestClient(app)
        clients.append(client)
        return client

    yield _use

    for client in clients:
        client.close()
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_keyword_metrics_collection_service, None)


def _new_keyword(client: TestClient, keyword: str = "kw") -> int:
    resp = client.post("/api/v1/keywords", json={"keyword": keyword})
    assert resp.status_code == 201
    return resp.json()["id"]


def _assert_error_shape(body: dict, code: str) -> None:
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    assert body["error"]["message"]


def test_collect_returns_201_and_signal_body(collector_api: UseCollector) -> None:
    client = collector_api(FakeGoogleAdsProvider(metrics=[_metrics("running shoes")]))
    keyword_id = _new_keyword(client, "running shoes")

    resp = client.post(_URL.format(kid=keyword_id))

    assert resp.status_code == 201
    body = resp.json()
    assert body["keyword_id"] == keyword_id
    assert body["component"] == "search_demand"
    assert body["provider"] == "google_ads"
    assert body["normalized_value"] == 60.01
    assert body["raw_data"]["avg_monthly_searches"] == 1000
    assert body["raw_data"]["normalizer"] == {"name": "search_demand", "version": "v1"}
    assert body["raw_data"]["geo_target_id"] == 2392
    assert body["source_reference"]


def test_collected_signal_is_persisted_and_listed(collector_api: UseCollector) -> None:
    client = collector_api(FakeGoogleAdsProvider(metrics=[_metrics("kw")]))
    keyword_id = _new_keyword(client)

    client.post(_URL.format(kid=keyword_id))

    listed = client.get(f"/api/v1/keywords/{keyword_id}/signals").json()
    assert len(listed) == 1
    assert listed[0]["provider"] == "google_ads"
    latest = client.get(
        f"/api/v1/keywords/{keyword_id}/signals/search_demand/latest"
    ).json()
    assert latest["provider"] == "google_ads"


def test_collect_keyword_not_found_returns_404(collector_api: UseCollector) -> None:
    client = collector_api(FakeGoogleAdsProvider(metrics=[_metrics("kw")]))
    resp = client.post(_URL.format(kid=999999))
    assert resp.status_code == 404
    _assert_error_shape(resp.json(), "entity_not_found")


def test_collect_provider_not_configured_returns_503(collector_api: UseCollector) -> None:
    real_provider = GoogleAdsKeywordMetricsProvider(unconfigured_settings())
    client = collector_api(real_provider, settings=unconfigured_settings())
    keyword_id = _new_keyword(client)

    resp = client.post(_URL.format(kid=keyword_id))

    assert resp.status_code == 503
    _assert_error_shape(resp.json(), "provider_not_configured")
    assert "token" not in resp.json()["error"]["message"].lower()


def test_collect_provider_error_returns_502(collector_api: UseCollector) -> None:
    provider = FakeGoogleAdsProvider(
        error=ExternalProviderError("google_ads", "Google Ads API request failed")
    )
    client = collector_api(provider)
    keyword_id = _new_keyword(client)

    resp = client.post(_URL.format(kid=keyword_id))

    assert resp.status_code == 502
    _assert_error_shape(resp.json(), "external_provider_error")


def test_collect_no_metrics_returns_502_data_error(collector_api: UseCollector) -> None:
    client = collector_api(FakeGoogleAdsProvider(metrics=[]))
    keyword_id = _new_keyword(client)

    resp = client.post(_URL.format(kid=keyword_id))

    assert resp.status_code == 502
    _assert_error_shape(resp.json(), "external_provider_data_error")
    assert client.get(f"/api/v1/keywords/{keyword_id}/signals").json() == []


def test_collect_ignores_request_body(collector_api: UseCollector) -> None:
    client = collector_api(FakeGoogleAdsProvider(metrics=[_metrics("kw")]))
    keyword_id = _new_keyword(client)

    # body なし仕様。余計な body を送っても 201 (無視される)。
    resp = client.post(_URL.format(kid=keyword_id), json={"unexpected": "value"})
    assert resp.status_code == 201


def test_end_to_end_google_ads_then_score_from_signals(
    collector_api: UseCollector,
) -> None:
    client = collector_api(FakeGoogleAdsProvider(metrics=[_metrics("kw", avg=1000)]))
    keyword_id = _new_keyword(client)

    manual = {
        "commercial_intent": 90,
        "affiliate_opportunity": 80,
        "competition_ease": 70,
        "trend": 60,
        "originality": 50,
        "site_relevance": 40,
    }
    for component, value in manual.items():
        r = client.post(
            f"/api/v1/keywords/{keyword_id}/signals",
            json={
                "component": component,
                "normalized_value": value,
                "provider": "manual",
                "observed_at": "2026-01-01T00:00:00Z",
            },
        )
        assert r.status_code == 201

    sd = client.post(_URL.format(kid=keyword_id))
    assert sd.status_code == 201
    assert sd.json()["normalized_value"] == 60.01

    score = client.post(f"/api/v1/keywords/{keyword_id}/scores/from-signals")
    assert score.status_code == 201
    score_body = score.json()
    assert score_body["search_demand"] == 60.01
    assert score_body["input_source"] == "signals"

    prov = client.get(
        f"/api/v1/keywords/{keyword_id}/scores/{score_body['id']}/signals"
    ).json()
    assert len(prov) == 7
    assert {row["component"] for row in prov} == {"search_demand", *manual.keys()}
    providers = {row["component"]: row["provider"] for row in prov}
    assert providers["search_demand"] == "google_ads"

    kw = client.get(f"/api/v1/keywords/{keyword_id}").json()
    assert kw["opportunity_score"] == score_body["total_score"]
    assert kw["status"] == "analyzed"


def test_recollect_uses_newest_observed_at_for_latest(
    collector_api: UseCollector,
) -> None:
    client = collector_api(FakeGoogleAdsProvider(metrics=[_metrics("kw", avg=100000)]))
    keyword_id = _new_keyword(client)

    # 古い observed_at の manual search_demand が既にあっても新しい方が latest
    client.post(
        f"/api/v1/keywords/{keyword_id}/signals",
        json={
            "component": "search_demand",
            "normalized_value": 1.0,
            "provider": "manual",
            "observed_at": "2019-01-01T00:00:00Z",
        },
    )
    client.post(_URL.format(kid=keyword_id))

    latest = client.get(
        f"/api/v1/keywords/{keyword_id}/signals/search_demand/latest"
    ).json()
    assert latest["provider"] == "google_ads"
    assert latest["normalized_value"] == 100.0


# -- Google Ads commercial_intent collector (Phase 2B-3) --------------
def test_commercial_intent_returns_201_and_signal_body(
    collector_api: UseCollector,
) -> None:
    client = collector_api(FakeGoogleAdsProvider(metrics=[_metrics("AI 議事録 比較")]))
    keyword_id = _new_keyword(client, "AI 議事録 比較")

    resp = client.post(_CI_URL.format(kid=keyword_id))

    assert resp.status_code == 201
    body = resp.json()
    assert body["keyword_id"] == keyword_id
    assert body["component"] == "commercial_intent"
    assert body["provider"] == "google_ads"
    assert 0.0 <= body["normalized_value"] <= 100.0
    raw = body["raw_data"]
    assert raw["query_intent_type"] == "compare"
    assert raw["query_intent_score"] == 90.0
    assert raw["market_evidence_available"] is True
    assert raw["evidence_coverage"] == 1.0
    assert raw["currency_assumption"] == "JPY"
    assert raw["normalizer_version"] == "v1"
    assert raw["normalizer"] == {"name": "commercial_intent", "version": "v1"}
    # 広告 competition は raw_data に残すが competition_ease には流用しない
    assert raw["competition_index"] == 90
    assert body["source_reference"]


def test_commercial_intent_persisted_and_listed(collector_api: UseCollector) -> None:
    client = collector_api(FakeGoogleAdsProvider(metrics=[_metrics("kw")]))
    keyword_id = _new_keyword(client)

    client.post(_CI_URL.format(kid=keyword_id))

    listed = client.get(
        f"/api/v1/keywords/{keyword_id}/signals?component=commercial_intent"
    ).json()
    assert len(listed) == 1
    assert listed[0]["component"] == "commercial_intent"
    latest = client.get(
        f"/api/v1/keywords/{keyword_id}/signals/commercial_intent/latest"
    ).json()
    assert latest["provider"] == "google_ads"


def test_commercial_intent_keyword_not_found_returns_404(
    collector_api: UseCollector,
) -> None:
    client = collector_api(FakeGoogleAdsProvider(metrics=[_metrics("kw")]))
    resp = client.post(_CI_URL.format(kid=999999))
    assert resp.status_code == 404
    _assert_error_shape(resp.json(), "entity_not_found")


def test_commercial_intent_provider_not_configured_returns_503(
    collector_api: UseCollector,
) -> None:
    real_provider = GoogleAdsKeywordMetricsProvider(unconfigured_settings())
    client = collector_api(real_provider, settings=unconfigured_settings())
    keyword_id = _new_keyword(client)

    resp = client.post(_CI_URL.format(kid=keyword_id))

    assert resp.status_code == 503
    _assert_error_shape(resp.json(), "provider_not_configured")
    assert "token" not in resp.json()["error"]["message"].lower()


def test_commercial_intent_provider_error_returns_502(
    collector_api: UseCollector,
) -> None:
    provider = FakeGoogleAdsProvider(
        error=ExternalProviderError("google_ads", "Google Ads API request failed")
    )
    client = collector_api(provider)
    keyword_id = _new_keyword(client)

    resp = client.post(_CI_URL.format(kid=keyword_id))

    assert resp.status_code == 502
    _assert_error_shape(resp.json(), "external_provider_error")


def test_commercial_intent_no_metrics_returns_502_data_error(
    collector_api: UseCollector,
) -> None:
    client = collector_api(FakeGoogleAdsProvider(metrics=[]))
    keyword_id = _new_keyword(client)

    resp = client.post(_CI_URL.format(kid=keyword_id))

    assert resp.status_code == 502
    _assert_error_shape(resp.json(), "external_provider_data_error")
    assert client.get(f"/api/v1/keywords/{keyword_id}/signals").json() == []


def test_search_demand_unchanged_when_commercial_intent_added(
    collector_api: UseCollector,
) -> None:
    # 回帰: commercial_intent を足しても search_demand の挙動は変わらない
    client = collector_api(FakeGoogleAdsProvider(metrics=[_metrics("kw", avg=1000)]))
    keyword_id = _new_keyword(client)

    sd = client.post(_URL.format(kid=keyword_id))
    assert sd.status_code == 201
    assert sd.json()["normalized_value"] == 60.01
    assert sd.json()["component"] == "search_demand"

    ci = client.post(_CI_URL.format(kid=keyword_id))
    assert ci.status_code == 201
    assert ci.json()["component"] == "commercial_intent"

    signals = client.get(f"/api/v1/keywords/{keyword_id}/signals").json()
    assert {s["component"] for s in signals} == {"search_demand", "commercial_intent"}


def test_google_ads_search_demand_plus_commercial_intent_feed_from_signals(
    collector_api: UseCollector,
) -> None:
    client = collector_api(
        FakeGoogleAdsProvider(metrics=[_metrics("AI 議事録 比較", avg=1000)])
    )
    keyword_id = _new_keyword(client, "AI 議事録 比較")

    assert client.post(_URL.format(kid=keyword_id)).status_code == 201
    assert client.post(_CI_URL.format(kid=keyword_id)).status_code == 201

    manual = {
        "affiliate_opportunity": 80,
        "competition_ease": 70,
        "trend": 60,
        "originality": 50,
        "site_relevance": 40,
    }
    for component, value in manual.items():
        r = client.post(
            f"/api/v1/keywords/{keyword_id}/signals",
            json={
                "component": component,
                "normalized_value": value,
                "provider": "manual",
                "observed_at": "2020-01-01T00:00:00Z",
            },
        )
        assert r.status_code == 201

    score = client.post(f"/api/v1/keywords/{keyword_id}/scores/from-signals")
    assert score.status_code == 201
    body = score.json()
    assert body["input_source"] == "signals"
    assert body["search_demand"] == 60.01

    prov = client.get(
        f"/api/v1/keywords/{keyword_id}/scores/{body['id']}/signals"
    ).json()
    assert len(prov) == 7
    providers = {row["component"]: row["provider"] for row in prov}
    assert providers["search_demand"] == "google_ads"
    assert providers["commercial_intent"] == "google_ads"


# -- Google Ads trend collector (Phase 2B-4) -------------------------
def test_trend_returns_201_and_signal_body(collector_api: UseCollector) -> None:
    client = collector_api(
        FakeGoogleAdsProvider(metrics=[_trend_metrics("running shoes")])
    )
    keyword_id = _new_keyword(client, "running shoes")

    resp = client.post(_TREND_URL.format(kid=keyword_id))

    assert resp.status_code == 201
    body = resp.json()
    assert body["keyword_id"] == keyword_id
    assert body["component"] == "trend"
    assert body["provider"] == "google_ads"
    assert body["normalized_value"] == 70.0  # [100,100,100,150,150,150]
    raw = body["raw_data"]
    assert raw["previous_3_average"] == 100.0
    assert raw["recent_3_average"] == 150.0
    assert raw["change_ratio"] == 0.4
    assert raw["months_used"] == 6
    assert raw["available_months"] == 6
    assert len(raw["monthly_search_volumes"]) == 6
    assert raw["normalizer_version"] == "v1"
    assert raw["normalizer"] == {"name": "trend", "version": "v1"}
    assert body["source_reference"]


def test_trend_persisted_listed_and_latest(collector_api: UseCollector) -> None:
    client = collector_api(FakeGoogleAdsProvider(metrics=[_trend_metrics("kw")]))
    keyword_id = _new_keyword(client)

    client.post(_TREND_URL.format(kid=keyword_id))

    listed = client.get(
        f"/api/v1/keywords/{keyword_id}/signals?component=trend"
    ).json()
    assert len(listed) == 1
    assert listed[0]["component"] == "trend"
    latest = client.get(
        f"/api/v1/keywords/{keyword_id}/signals/trend/latest"
    ).json()
    assert latest["provider"] == "google_ads"
    assert latest["normalized_value"] == 70.0


def test_trend_keyword_not_found_returns_404(collector_api: UseCollector) -> None:
    client = collector_api(FakeGoogleAdsProvider(metrics=[_trend_metrics("kw")]))
    resp = client.post(_TREND_URL.format(kid=999999))
    assert resp.status_code == 404
    _assert_error_shape(resp.json(), "entity_not_found")


def test_trend_provider_not_configured_returns_503(collector_api: UseCollector) -> None:
    real_provider = GoogleAdsKeywordMetricsProvider(unconfigured_settings())
    client = collector_api(real_provider, settings=unconfigured_settings())
    keyword_id = _new_keyword(client)

    resp = client.post(_TREND_URL.format(kid=keyword_id))

    assert resp.status_code == 503
    _assert_error_shape(resp.json(), "provider_not_configured")
    assert "token" not in resp.json()["error"]["message"].lower()


def test_trend_provider_error_returns_502(collector_api: UseCollector) -> None:
    provider = FakeGoogleAdsProvider(
        error=ExternalProviderError("google_ads", "Google Ads API request failed")
    )
    client = collector_api(provider)
    keyword_id = _new_keyword(client)

    resp = client.post(_TREND_URL.format(kid=keyword_id))

    assert resp.status_code == 502
    _assert_error_shape(resp.json(), "external_provider_error")


def test_trend_insufficient_months_returns_502_data_error(
    collector_api: UseCollector,
) -> None:
    client = collector_api(
        FakeGoogleAdsProvider(metrics=[_trend_metrics("kw", values=[1, 2, 3, 4, 5])])
    )
    keyword_id = _new_keyword(client)

    resp = client.post(_TREND_URL.format(kid=keyword_id))

    assert resp.status_code == 502
    _assert_error_shape(resp.json(), "external_provider_data_error")
    assert client.get(f"/api/v1/keywords/{keyword_id}/signals").json() == []


def test_all_three_google_ads_collectors_coexist(collector_api: UseCollector) -> None:
    client = collector_api(FakeGoogleAdsProvider(metrics=[_trend_metrics("kw")]))
    keyword_id = _new_keyword(client)

    assert client.post(_URL.format(kid=keyword_id)).status_code == 201
    assert client.post(_CI_URL.format(kid=keyword_id)).status_code == 201
    assert client.post(_TREND_URL.format(kid=keyword_id)).status_code == 201

    signals = client.get(f"/api/v1/keywords/{keyword_id}/signals").json()
    assert {s["component"] for s in signals} == {
        "search_demand",
        "commercial_intent",
        "trend",
    }
    # competition_ease は自動生成されない
    assert (
        client.get(
            f"/api/v1/keywords/{keyword_id}/signals?component=competition_ease"
        ).json()
        == []
    )

    # 3/7 component しか無いので from-signals は 409 のまま (既存仕様)
    score = client.post(f"/api/v1/keywords/{keyword_id}/scores/from-signals")
    assert score.status_code == 409
    _assert_error_shape(score.json(), "incomplete_signal_set")


def test_trend_does_not_change_search_demand_or_commercial_intent(
    collector_api: UseCollector,
) -> None:
    # 回帰: trend 追加後も search_demand / commercial_intent の値は不変
    client = collector_api(FakeGoogleAdsProvider(metrics=[_trend_metrics("kw")]))
    keyword_id = _new_keyword(client)

    sd = client.post(_URL.format(kid=keyword_id)).json()
    ci = client.post(_CI_URL.format(kid=keyword_id)).json()
    client.post(_TREND_URL.format(kid=keyword_id))

    assert sd["normalized_value"] == 60.01  # normalize_search_demand(1000)
    assert sd["component"] == "search_demand"
    assert ci["component"] == "commercial_intent"

    sd_latest = client.get(
        f"/api/v1/keywords/{keyword_id}/signals/search_demand/latest"
    ).json()
    ci_latest = client.get(
        f"/api/v1/keywords/{keyword_id}/signals/commercial_intent/latest"
    ).json()
    assert sd_latest["normalized_value"] == sd["normalized_value"]
    assert ci_latest["normalized_value"] == ci["normalized_value"]
