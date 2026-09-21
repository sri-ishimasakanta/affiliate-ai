"""app/affiliate/make_client.py — MakeAffiliateClient (mocked transport)。

実ネットワークなし。Authorization ヘッダ / GET-only / query パラメータ /
timeout・接続エラー写像 / redirect 拒否 / token 非露出 を確認する。
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from app.affiliate.make_client import MakeAffiliateClient
from app.config.settings import Settings
from app.exceptions import AffiliateCommissionImportError, ProviderNotConfiguredError

_TOKEN = "synthetic-make-api-token-not-real"
# zone-specific base (Phase E1.2 §5) -- MAKE_API_BASE_URL は "/api/v2" まで含む。
_BASE = "https://eu1.make.test/api/v2"
_DATES = {"date_from": "2026-09-01", "date_to": "2026-09-30"}


def _commissions_page(rows: list[dict]) -> dict:
    return {"commissions": rows}


def _settings(**over) -> Settings:
    base = {"make_api_base_url": _BASE, "make_api_token": _TOKEN}
    base.update(over)
    return Settings(**base)


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _row(rid: int, **over) -> dict:
    base = {
        "id": rid,
        "organization_id": 1,
        "type": "commission",
        "status": "requested",
        "commission": 1.0,
        "source": "referral",
        "created": "2026-09-01T00:00:00+00:00",
        "payout_requested": None,
        "payout_approved": None,
        "payout_realized": None,
    }
    base.update(over)
    return base


# ==================== configuration =====================================
def test_not_configured_raises() -> None:
    with pytest.raises(ProviderNotConfiguredError):
        MakeAffiliateClient(Settings(make_api_base_url=None, make_api_token=None))


def test_missing_token_only_raises() -> None:
    with pytest.raises(ProviderNotConfiguredError):
        MakeAffiliateClient(Settings(make_api_base_url=_BASE, make_api_token=None))


def test_non_https_base_url_rejected() -> None:
    with pytest.raises(AffiliateCommissionImportError, match="https"):
        MakeAffiliateClient(_settings(make_api_base_url="http://api.make.test"))


# ==================== request construction ================================
def test_authorization_header_sent_correctly() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_commissions_page([]))

    client = MakeAffiliateClient(_settings(), transport=_transport(handler))
    client.get_commissions(**_DATES, limit=10)
    assert seen["auth"] == f"Token {_TOKEN}"


def test_get_commissions_uses_get_method_and_correct_path() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json=_commissions_page([]))

    client = MakeAffiliateClient(_settings(), transport=_transport(handler))
    client.get_commissions(**_DATES, limit=10)
    assert seen["method"] == "GET"
    # base URL 自体が "/api/v2" を含む前提なので、path はそれを含まない
    # (二重付与しない、Phase E1.2 §5)。
    assert seen["path"] == "/api/v2/affiliate/commissions"


def test_get_commissions_query_params_exact() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=_commissions_page([]))

    client = MakeAffiliateClient(_settings(), transport=_transport(handler))
    client.get_commissions(
        date_from="2026-09-01",
        date_to="2026-09-30",
        status_id="3",
        offset=20,
        limit=10,
        sort_by="created",
        sort_dir="desc",
    )
    assert seen["params"] == {
        "pg[offset]": "20",
        "pg[limit]": "10",
        "dateFrom": "2026-09-01",
        "dateTo": "2026-09-30",
        "statusId": "3",
        "pg[sortBy]": "created",
        "pg[sortDir]": "desc",
    }


def test_get_commissions_limit_bounds_enforced() -> None:
    client = MakeAffiliateClient(
        _settings(),
        transport=_transport(lambda r: httpx.Response(200, json=_commissions_page([]))),
    )
    with pytest.raises(AffiliateCommissionImportError, match="limit"):
        client.get_commissions(**_DATES, limit=0)
    with pytest.raises(AffiliateCommissionImportError, match="limit"):
        client.get_commissions(**_DATES, limit=100000)


def test_get_commissions_offset_must_be_non_negative() -> None:
    client = MakeAffiliateClient(
        _settings(),
        transport=_transport(lambda r: httpx.Response(200, json=_commissions_page([]))),
    )
    with pytest.raises(AffiliateCommissionImportError, match="offset"):
        client.get_commissions(**_DATES, offset=-1, limit=10)


# ==================== response handling ====================================
def test_get_commissions_returns_validated_rows() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_commissions_page([_row(1), _row(2)]))

    client = MakeAffiliateClient(_settings(), transport=_transport(handler))
    rows, has_more = client.get_commissions(**_DATES, limit=10)
    assert [r.source_commission_id for r in rows] == ["1", "2"]
    assert has_more is False


def test_get_commissions_amounts_are_exact_decimal_through_real_json_roundtrip() -> None:
    """Phase E1.1: JSON body -> httpx -> client の実経路で、binary float の
    round-trip を経由せず厳密な Decimal になることを証明する
    (parse_float=Decimal の配線そのものを検証する -- 純粋な validator 単体
    テストではなく、実際の JSON エンコード/デコードを経由させる)。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_commissions_page([_row(1, commission=0.1), _row(2, commission=0.2)]),
        )

    client = MakeAffiliateClient(_settings(), transport=_transport(handler))
    rows, _has_more = client.get_commissions(**_DATES, limit=10)
    total = rows[0].commission_amount + rows[1].commission_amount
    assert total == Decimal("0.3")


def test_http_error_status_mapped_safely() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    client = MakeAffiliateClient(_settings(), transport=_transport(handler))
    with pytest.raises(AffiliateCommissionImportError) as exc_info:
        client.get_commissions(**_DATES, limit=10)
    assert exc_info.value.http_status == 401
    assert _TOKEN not in str(exc_info.value)


def test_redirect_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://elsewhere.test"})

    client = MakeAffiliateClient(_settings(), transport=_transport(handler))
    with pytest.raises(AffiliateCommissionImportError, match="redirect"):
        client.get_commissions(**_DATES, limit=10)


def test_non_json_response_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    client = MakeAffiliateClient(_settings(), transport=_transport(handler))
    with pytest.raises(AffiliateCommissionImportError, match="JSON"):
        client.get_commissions(**_DATES, limit=10)


def test_transport_error_mapped_safely() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = MakeAffiliateClient(_settings(), transport=_transport(handler))
    with pytest.raises(AffiliateCommissionImportError, match="connection failed"):
        client.get_commissions(**_DATES, limit=10)


def test_timeout_mapped_safely() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timeout", request=request)

    client = MakeAffiliateClient(_settings(), transport=_transport(handler))
    with pytest.raises(AffiliateCommissionImportError, match="timed out"):
        client.get_commissions(**_DATES, limit=10)


def test_no_write_method_exposed() -> None:
    client = MakeAffiliateClient(
        _settings(),
        transport=_transport(lambda r: httpx.Response(200, json=_commissions_page([]))),
    )
    for banned in ("post", "put", "patch", "delete", "request_payout", "create_payout"):
        assert not hasattr(client, banned), banned


# ==================== commission-info / stats (lightweight) ===============
def test_get_commission_info_requires_json_object() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    client = MakeAffiliateClient(_settings(), transport=_transport(handler))
    with pytest.raises(AffiliateCommissionImportError, match="object"):
        client.get_commission_info(**_DATES)


def test_get_commission_info_returns_payload() -> None:
    payload = {"availablePayout": 100.0, "isPayoutAvailable": True}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/affiliate/commission-info"
        return httpx.Response(200, json=payload)

    client = MakeAffiliateClient(_settings(), transport=_transport(handler))
    assert client.get_commission_info(**_DATES) == payload


def test_get_stats_requires_json_object() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    client = MakeAffiliateClient(_settings(), transport=_transport(handler))
    with pytest.raises(AffiliateCommissionImportError, match="object"):
        client.get_stats()


def test_get_stats_missing_stats_key_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "the stats key"})

    client = MakeAffiliateClient(_settings(), transport=_transport(handler))
    with pytest.raises(AffiliateCommissionImportError, match="stats"):
        client.get_stats()


def test_get_stats_value_not_array_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"stats": "not-a-list"})

    client = MakeAffiliateClient(_settings(), transport=_transport(handler))
    with pytest.raises(AffiliateCommissionImportError, match="stats"):
        client.get_stats()


def test_get_stats_returns_rows() -> None:
    rows = [{"date": "2026-09-01", "visits": 10, "registrations": 1, "commission": 5.0}]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/affiliate/stats"
        return httpx.Response(200, json={"stats": rows})

    client = MakeAffiliateClient(_settings(), transport=_transport(handler))
    assert client.get_stats() == rows


# ==================== Phase E1.2: zone-specific base URL ===================
def _capture_request(seen: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["path"] = request.url.path
        return httpx.Response(200, json=_commissions_page([]))

    return handler


@pytest.mark.parametrize(
    "zone_base",
    [
        "https://eu1.make.com/api/v2",
        "https://eu2.make.com/api/v2",
        "https://us1.make.com/api/v2",
    ],
)
def test_zone_specific_base_urls_construct_correct_endpoints(zone_base: str) -> None:
    """MAKE_API_BASE_URL は zone 依存 (eu1/eu2/us1 等) -- ハードコードしない。
    base に既に含まれる "/api/v2" を二重付与しない。"""

    seen: dict = {}
    client = MakeAffiliateClient(
        _settings(make_api_base_url=zone_base), transport=_transport(_capture_request(seen))
    )
    client.get_commissions(**_DATES, limit=10)
    assert seen["path"] == "/api/v2/affiliate/commissions"
    assert seen["url"].startswith(zone_base)
    # "/api/v2" が二重に現れていないこと。
    assert seen["url"].count("/api/v2") == 1


# ==================== Phase E1.4: dateFrom/dateTo are mandatory ===========
def _no_http_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP request expected: dates are validated before sending")

    return httpx.MockTransport(handler)


def test_commissions_without_dates_fails_before_http() -> None:
    client = MakeAffiliateClient(_settings(), transport=_no_http_transport())
    with pytest.raises(AffiliateCommissionImportError, match="both required"):
        client.get_commissions(date_from=None, date_to=None, limit=10)


def test_commission_info_without_dates_fails_before_http() -> None:
    client = MakeAffiliateClient(_settings(), transport=_no_http_transport())
    with pytest.raises(AffiliateCommissionImportError, match="both required"):
        client.get_commission_info(date_from=None, date_to=None)


def test_dates_are_required_keyword_arguments_no_default() -> None:
    """日付をデフォルト化しない -- 引数自体を省略すると呼び出しが成立しない。"""

    client = MakeAffiliateClient(_settings(), transport=_no_http_transport())
    with pytest.raises(TypeError):
        client.get_commissions(limit=10)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        client.get_commission_info()  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "only",
    [{"date_from": "2026-09-01", "date_to": None}, {"date_from": None, "date_to": "2026-09-30"}],
)
def test_one_date_only_fails_before_http(only: dict) -> None:
    client = MakeAffiliateClient(_settings(), transport=_no_http_transport())
    with pytest.raises(AffiliateCommissionImportError, match="both required"):
        client.get_commissions(**only, limit=10)
    with pytest.raises(AffiliateCommissionImportError, match="both required"):
        client.get_commission_info(**only)


@pytest.mark.parametrize(
    ("date_from", "date_to", "match"),
    [
        ("2026/09/01", "2026-09-30", "date_from"),
        ("2026-09-01", "not-a-date", "date_to"),
        ("2026-09-30", "2026-09-01", "must not be after"),
    ],
)
def test_malformed_or_reversed_dates_fail_before_http(date_from, date_to, match) -> None:
    client = MakeAffiliateClient(_settings(), transport=_no_http_transport())
    with pytest.raises(AffiliateCommissionImportError, match=match):
        client.get_commissions(date_from=date_from, date_to=date_to, limit=10)
    with pytest.raises(AffiliateCommissionImportError, match=match):
        client.get_commission_info(date_from=date_from, date_to=date_to)


def test_commissions_sends_both_dates_and_pagination_only() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=_commissions_page([]))

    client = MakeAffiliateClient(_settings(), transport=_transport(handler))
    client.get_commissions(**_DATES, offset=0, limit=2)
    assert seen["params"] == {
        "dateFrom": "2026-09-01",
        "dateTo": "2026-09-30",
        "pg[offset]": "0",
        "pg[limit]": "2",
    }
    # returnTotalCount は別途 live 検証するまで有効化しない。
    assert "pg[returnTotalCount]" not in seen["params"]


def test_commission_info_sends_both_dates() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"available_payout": 0, "earnings_total": 0})

    client = MakeAffiliateClient(_settings(), transport=_transport(handler))
    info = client.get_commission_info(**_DATES)
    assert seen["path"] == "/api/v2/affiliate/commission-info"
    assert seen["params"] == {"dateFrom": "2026-09-01", "dateTo": "2026-09-30"}
    assert info == {"available_payout": 0, "earnings_total": 0}  # snake_case は素通し


# ==================== Phase E1.4: live "commissions": null ================
def test_null_commissions_is_empty_list_through_client() -> None:
    """live の該当行なし成功レスポンス: {"commissions": null, "pg": {...}}。"""

    live_empty = {
        "commissions": None,
        "pg": {
            "limit": 2,
            "offset": 0,
            "returnTotalCount": False,
            "sortBy": "id",
            "sortDir": "asc",
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=live_empty)

    client = MakeAffiliateClient(_settings(), transport=_transport(handler))
    rows, has_more = client.get_commissions(**_DATES, limit=2)
    assert rows == []
    assert has_more is False


def test_malformed_commissions_shapes_still_rejected_through_client() -> None:
    for body in ({"pg": {}}, {"commissions": "x"}, {"commissions": {}}, {"commissions": 5}, [1]):

        def handler(request: httpx.Request, body=body) -> httpx.Response:
            return httpx.Response(200, json=body)

        client = MakeAffiliateClient(_settings(), transport=_transport(handler))
        with pytest.raises(AffiliateCommissionImportError):
            client.get_commissions(**_DATES, limit=2)
