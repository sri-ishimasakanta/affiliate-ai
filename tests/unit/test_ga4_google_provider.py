"""app.analytics.google_provider の単体テスト (C5.2)。

``httpx.MockTransport`` で実 Google に触れずに検証する。pin する契約:

- 全トラフィックとオーガニック検索を **別々の照会** として送り、後者にだけ
  ``sessionDefaultChannelGroup`` の filter を付ける。
- 行の ``channel_scope`` で区別し、片方からもう片方を推定しない。
- ``userEngagementDuration`` (合計秒) を active users で割って平均に直す。
- エラー本文は status と短い message だけを載せ、credential は一切載せない。
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from app.analytics.google_provider import GoogleGa4Provider, normalize_property_id
from app.exceptions import ExternalProviderDataError, ExternalProviderError


class _Settings:
    search_console_credentials_file = "/nonexistent.json"


def _provider(handler, monkeypatch) -> GoogleGa4Provider:
    provider = GoogleGa4Provider(
        _Settings(), http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(provider, "_token", lambda: "test-token")
    return provider


def _report(rows: list[tuple[str, str, list[str]]]) -> dict:
    return {
        "rows": [
            {
                "dimensionValues": [{"value": d} for d in (day, path)],
                "metricValues": [{"value": v} for v in metrics],
            }
            for day, path, metrics in rows
        ],
        "rowCount": len(rows),
    }


_METRICS = ["10", "8", "6", "5", "0.5", "800", "12"]


def test_property_id_is_normalized() -> None:
    assert normalize_property_id("properties/123") == "123"
    assert normalize_property_id(" 123 ") == "123"
    with pytest.raises(ExternalProviderError):
        normalize_property_id("G-ABC123")


def test_both_channel_scopes_are_requested_separately(monkeypatch) -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=_report([("20260922", "/rpa-tools/", _METRICS)]))

    rows = _provider(handler, monkeypatch).fetch_page_daily(
        property_id="123", start_date=date(2026, 9, 22), end_date=date(2026, 9, 22)
    )

    assert len(payloads) == 2
    assert "dimensionFilter" not in payloads[0]
    filter_field = payloads[1]["dimensionFilter"]["filter"]
    assert filter_field["fieldName"] == "sessionDefaultChannelGroup"
    assert filter_field["stringFilter"]["value"] == "Organic Search"
    assert [r.channel_scope for r in rows] == ["all", "organic_search"]


def test_average_engagement_time_is_per_active_user(monkeypatch) -> None:
    def handler(request):
        return httpx.Response(200, json=_report([("20260922", "/a/", _METRICS)]))

    rows = _provider(handler, monkeypatch).fetch_page_daily(
        property_id="123", start_date=date(2026, 9, 22), end_date=date(2026, 9, 22)
    )
    # userEngagementDuration 800 秒 / activeUsers 8 = 100 秒
    assert rows[0].average_engagement_time_seconds == 100.0


def test_zero_active_users_does_not_divide_by_zero(monkeypatch) -> None:
    metrics = ["0", "0", "0", "0", "0", "0", "0"]

    def handler(request):
        return httpx.Response(200, json=_report([("20260922", "/a/", metrics)]))

    rows = _provider(handler, monkeypatch).fetch_page_daily(
        property_id="123", start_date=date(2026, 9, 22), end_date=date(2026, 9, 22)
    )
    assert rows[0].average_engagement_time_seconds == 0.0


def test_query_string_is_stripped_from_page_path(monkeypatch) -> None:
    def handler(request):
        return httpx.Response(200, json=_report([("20260922", "/a/?utm_source=x", _METRICS)]))

    rows = _provider(handler, monkeypatch).fetch_page_daily(
        property_id="123", start_date=date(2026, 9, 22), end_date=date(2026, 9, 22)
    )
    assert rows[0].page_path == "/a/"


def test_http_error_is_wrapped_without_credentials(monkeypatch) -> None:
    def handler(request):
        return httpx.Response(
            403,
            json={"error": {"status": "PERMISSION_DENIED", "message": "no access"}},
        )

    with pytest.raises(ExternalProviderError) as excinfo:
        _provider(handler, monkeypatch).fetch_page_daily(
            property_id="123", start_date=date(2026, 9, 22), end_date=date(2026, 9, 22)
        )
    message = str(excinfo.value)
    assert "PERMISSION_DENIED" in message
    assert "test-token" not in message
    assert "credentials" not in message.lower()


def test_malformed_row_is_rejected(monkeypatch) -> None:
    def handler(request):
        return httpx.Response(
            200,
            json={
                "rows": [{"dimensionValues": [{"value": "20260922"}], "metricValues": []}],
                "rowCount": 1,
            },
        )

    with pytest.raises(ExternalProviderDataError):
        _provider(handler, monkeypatch).fetch_page_daily(
            property_id="123", start_date=date(2026, 9, 22), end_date=date(2026, 9, 22)
        )


def test_timezone_lookup_failure_returns_none(monkeypatch) -> None:
    def handler(request):
        return httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED"}})

    assert _provider(handler, monkeypatch).fetch_property_timezone(property_id="123") is None


def test_timezone_is_returned_when_available(monkeypatch) -> None:
    def handler(request):
        return httpx.Response(200, json={"timeZone": "Asia/Tokyo"})

    assert (
        _provider(handler, monkeypatch).fetch_property_timezone(property_id="123") == "Asia/Tokyo"
    )
