"""app/search_console/google_provider.py — read-only Search Analytics provider。

実 Google リクエストは一切行わない:
- credential 境界 (``load_readonly_credentials``) は fake に差し替え。
- HTTP 境界は ``httpx.MockTransport`` に差し替え。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from app.exceptions import ExternalProviderDataError, ExternalProviderError
from app.search_console import google_provider as gp
from app.search_console.credentials import SEARCH_CONSOLE_READONLY_SCOPE
from app.search_console.rows import SearchConsolePageRow, SearchConsoleQueryRow

_PROP = "sc-domain:bizfluxlab.com"
_ENCODED = "sc-domain%3Abizfluxlab.com"
_EMAIL = "reader@example.iam.gserviceaccount.com"
_START = date(2026, 9, 1)
_END = date(2026, 9, 7)


class _FakeCreds:
    def __init__(
        self,
        *,
        scopes=(SEARCH_CONSOLE_READONLY_SCOPE,),
        token: str | None = "fake-access-token",
        refresh_exc: Exception | None = None,
    ) -> None:
        self.scopes = list(scopes)
        self.token = None
        self._token = token
        self._refresh_exc = refresh_exc
        self.refresh_calls = 0

    @property
    def valid(self) -> bool:
        return self.token is not None

    def refresh(self, request) -> None:  # noqa: ANN001
        self.refresh_calls += 1
        if self._refresh_exc is not None:
            raise self._refresh_exc
        self.token = self._token


class _FakeFacts:
    client_email = _EMAIL
    path = Path("/outside/repo/sa.json")


@pytest.fixture
def patch_loader(monkeypatch):
    creds_box: dict = {}

    def _apply(creds: _FakeCreds | None = None):
        creds = creds or _FakeCreds()
        creds_box["creds"] = creds

        def _fake_load(raw_path):  # noqa: ANN001
            return creds, _FakeFacts()

        monkeypatch.setattr(gp, "load_readonly_credentials", _fake_load)
        return creds

    _apply.box = creds_box
    return _apply


def _row(keys, *, clicks=1.0, impressions=5.0, ctr=0.2, position=3.4) -> dict:
    return {
        "keys": list(keys),
        "clicks": clicks,
        "impressions": impressions,
        "ctr": ctr,
        "position": position,
    }


def _provider(
    handler, patch_loader, *, creds=None, row_limit=None
) -> gp.GoogleSearchConsoleProvider:
    patch_loader(creds)
    kw = {
        "credentials_file": "/outside/repo/sa.json",
        "transport": httpx.MockTransport(handler),
    }
    if row_limit is not None:
        kw["row_limit"] = row_limit
    return gp.GoogleSearchConsoleProvider(**kw)


def _single(rows=None, *, agg="byProperty", status=200, body=None):
    """1 応答を返し、受信 request を calls に記録する handler ファクトリ。"""

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if status != 200:
            return httpx.Response(status, json={})
        payload = {} if body is None else dict(body)
        if rows is not None:
            payload["rows"] = rows
        if agg is not None:
            payload["responseAggregationType"] = agg
        return httpx.Response(200, json=payload)

    return handler, calls


# ==========================================================================
# construction / scope
# ==========================================================================
def test_rejects_extra_scope(patch_loader) -> None:
    handler, _ = _single(rows=[])
    with pytest.raises(ExternalProviderError, match="readonly only"):
        _provider(
            handler,
            patch_loader,
            creds=_FakeCreds(
                scopes=(
                    SEARCH_CONSOLE_READONLY_SCOPE,
                    "https://www.googleapis.com/auth/webmasters",
                )
            ),
        )


def test_service_account_email_is_safe_fact(patch_loader) -> None:
    handler, _ = _single(rows=[])
    provider = _provider(handler, patch_loader)
    assert provider.service_account_email == _EMAIL


# ==========================================================================
# request contract (V2)
# ==========================================================================
def test_page_request_exact_body_and_encoded_url(patch_loader) -> None:
    handler, calls = _single(rows=[])
    provider = _provider(handler, patch_loader)
    provider.fetch_page_daily(property_uri=_PROP, start_date=_START, end_date=_END)

    assert len(calls) == 1
    req = calls[0]
    assert req.method == "POST"
    assert str(req.url) == (
        f"https://www.googleapis.com/webmasters/v3/sites/{_ENCODED}/searchAnalytics/query"
    )
    assert req.headers["authorization"] == "Bearer fake-access-token"
    assert json.loads(req.content) == {
        "startDate": "2026-09-01",
        "endDate": "2026-09-07",
        "dimensions": ["date", "page"],
        "type": "web",
        "aggregationType": "auto",
        "dataState": "final",
        "rowLimit": 25000,
        "startRow": 0,
    }


def test_query_request_exact_body(patch_loader) -> None:
    handler, calls = _single(rows=[])
    provider = _provider(handler, patch_loader)
    provider.fetch_query_daily(property_uri=_PROP, start_date=_START, end_date=_END)

    assert json.loads(calls[0].content) == {
        "startDate": "2026-09-01",
        "endDate": "2026-09-07",
        "dimensions": ["date", "page", "query"],
        "type": "web",
        "aggregationType": "auto",
        "dataState": "final",
        "rowLimit": 25000,
        "startRow": 0,
    }


def test_production_row_limit_is_25000(patch_loader) -> None:
    handler, calls = _single(rows=[])
    provider = _provider(handler, patch_loader)
    provider.fetch_page_daily(property_uri=_PROP, start_date=_START, end_date=_END)
    assert json.loads(calls[0].content)["rowLimit"] == 25000
    assert json.loads(calls[0].content)["startRow"] == 0


# ==========================================================================
# pagination
# ==========================================================================
def _paging_handler(pages: list[list[dict]]):
    """pages[i] を i 番目の応答 rows として返す handler。"""

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        idx = len(calls)
        calls.append(request)
        rows = pages[idx] if idx < len(pages) else []
        return httpx.Response(200, json={"rows": rows, "responseAggregationType": "byProperty"})

    return handler, calls


def test_single_page_when_short(patch_loader) -> None:
    handler, calls = _paging_handler([[_row(["2026-09-01", "https://x/a"])]])
    provider = _provider(handler, patch_loader, row_limit=2)
    rows = provider.fetch_page_daily(property_uri=_PROP, start_date=_START, end_date=_END)
    assert len(calls) == 1
    assert len(rows) == 1


def test_empty_first_page_terminates(patch_loader) -> None:
    handler, calls = _paging_handler([[]])
    provider = _provider(handler, patch_loader, row_limit=2)
    rows = provider.fetch_page_daily(property_uri=_PROP, start_date=_START, end_date=_END)
    assert len(calls) == 1
    assert rows == []


def test_multi_page_pagination_concatenates_and_advances_startrow(patch_loader) -> None:
    full = [
        _row(["2026-09-01", "https://x/a"]),
        _row(["2026-09-01", "https://x/b"]),
    ]
    tail = [_row(["2026-09-02", "https://x/c"])]
    handler, calls = _paging_handler([full, tail])
    provider = _provider(handler, patch_loader, row_limit=2)
    rows = provider.fetch_page_daily(property_uri=_PROP, start_date=_START, end_date=_END)

    assert len(calls) == 2
    assert json.loads(calls[0].content)["startRow"] == 0
    # startRow は「実際に返った行数」だけ進む
    assert json.loads(calls[1].content)["startRow"] == 2
    assert [r.page for r in rows] == ["https://x/a", "https://x/b", "https://x/c"]


def test_exactly_full_page_triggers_next_request_then_empty_terminates(patch_loader) -> None:
    full = [_row(["2026-09-01", "https://x/a"]), _row(["2026-09-01", "https://x/b"])]
    handler, calls = _paging_handler([full, []])
    provider = _provider(handler, patch_loader, row_limit=2)
    rows = provider.fetch_page_daily(property_uri=_PROP, start_date=_START, end_date=_END)
    assert len(calls) == 2
    assert len(rows) == 2


def test_rows_key_absent_is_zero_rows(patch_loader) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"responseAggregationType": "byProperty"})

    provider = _provider(handler, patch_loader, row_limit=2)
    assert provider.fetch_page_daily(property_uri=_PROP, start_date=_START, end_date=_END) == []


def test_defensive_page_limit_raises_not_truncates(patch_loader, monkeypatch) -> None:
    monkeypatch.setattr(gp, "_MAX_PAGES", 3)

    def handler(request: httpx.Request) -> httpx.Response:
        # 常に「満杯ページ」を返し続ける
        return httpx.Response(
            200,
            json={"rows": [_row(["2026-09-01", "https://x/a"]), _row(["2026-09-01", "https://x/b"])]},
        )

    provider = _provider(handler, patch_loader, row_limit=2)
    with pytest.raises(ExternalProviderDataError, match="defensive page limit"):
        provider.fetch_page_daily(property_uri=_PROP, start_date=_START, end_date=_END)


# ==========================================================================
# row parsing / mapping
# ==========================================================================
def test_page_row_key_mapping(patch_loader) -> None:
    handler, _ = _single(
        rows=[
            _row(
                ["2026-09-03", "https://x/a"],
                clicks=2.0,
                impressions=10.0,
                ctr=0.2,
                position=4.5,
            )
        ]
    )
    provider = _provider(handler, patch_loader)
    (row,) = provider.fetch_page_daily(property_uri=_PROP, start_date=_START, end_date=_END)
    assert isinstance(row, SearchConsolePageRow)
    assert row.metric_date == date(2026, 9, 3)
    assert row.page == "https://x/a"
    assert row.clicks == 2 and isinstance(row.clicks, int)
    assert row.impressions == 10
    assert row.ctr == 0.2
    assert row.position == 4.5


def test_query_row_key_mapping(patch_loader) -> None:
    handler, _ = _single(rows=[_row(["2026-09-03", "https://x/a", "some phrase"])])
    provider = _provider(handler, patch_loader)
    (row,) = provider.fetch_query_daily(property_uri=_PROP, start_date=_START, end_date=_END)
    assert isinstance(row, SearchConsoleQueryRow)
    assert row.metric_date == date(2026, 9, 3)
    assert row.page == "https://x/a"
    assert row.query == "some phrase"


@pytest.mark.parametrize(
    "keys",
    [
        ["2026-09-01"],
        ["2026-09-01", "https://x/a", "extra"],
        [],
    ],
)
def test_page_row_wrong_key_count_rejected(patch_loader, keys) -> None:
    handler, _ = _single(rows=[_row(keys)])
    provider = _provider(handler, patch_loader)
    with pytest.raises(ExternalProviderDataError, match="exactly 2 keys"):
        provider.fetch_page_daily(property_uri=_PROP, start_date=_START, end_date=_END)


def test_query_row_wrong_key_count_rejected(patch_loader) -> None:
    handler, _ = _single(rows=[_row(["2026-09-01", "https://x/a"])])
    provider = _provider(handler, patch_loader)
    with pytest.raises(ExternalProviderDataError, match="exactly 3 keys"):
        provider.fetch_query_daily(property_uri=_PROP, start_date=_START, end_date=_END)


@pytest.mark.parametrize("bad", ["2026-13-40", "not-a-date", "20260901", "2026-9-1", 20260901])
def test_invalid_date_rejected(patch_loader, bad) -> None:
    handler, _ = _single(rows=[_row([bad, "https://x/a"])])
    provider = _provider(handler, patch_loader)
    with pytest.raises(ExternalProviderDataError, match="date key"):
        provider.fetch_page_daily(property_uri=_PROP, start_date=_START, end_date=_END)


@pytest.mark.parametrize("bad_page", ["", "   ", 123, None])
def test_invalid_page_rejected(patch_loader, bad_page) -> None:
    handler, _ = _single(rows=[_row(["2026-09-01", bad_page])])
    provider = _provider(handler, patch_loader)
    with pytest.raises(ExternalProviderDataError, match="page key"):
        provider.fetch_page_daily(property_uri=_PROP, start_date=_START, end_date=_END)


@pytest.mark.parametrize("bad_query", ["", "   ", 5, None])
def test_empty_query_rejected(patch_loader, bad_query) -> None:
    handler, _ = _single(rows=[_row(["2026-09-01", "https://x/a", bad_query])])
    provider = _provider(handler, patch_loader)
    with pytest.raises(ExternalProviderDataError, match="query key"):
        provider.fetch_query_daily(property_uri=_PROP, start_date=_START, end_date=_END)


@pytest.mark.parametrize(
    "overrides",
    [
        {"clicks": 1.5},
        {"impressions": 2.5},
        {"clicks": -1.0},
        {"impressions": -3},
        {"clicks": True},
        {"clicks": "1"},
        {"ctr": 1.5},
        {"ctr": -0.1},
        {"position": -0.5},
        {"position": True},
    ],
)
def test_numeric_field_validation_rejects(patch_loader, overrides) -> None:
    handler, _ = _single(rows=[_row(["2026-09-01", "https://x/a"], **overrides)])
    provider = _provider(handler, patch_loader)
    with pytest.raises(ExternalProviderDataError):
        provider.fetch_page_daily(property_uri=_PROP, start_date=_START, end_date=_END)


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("field", ["clicks", "impressions", "ctr", "position"])
def test_non_finite_numeric_rejected(patch_loader, field, token) -> None:
    # 実 API は NaN/Infinity を返さないが、body に混入した場合も安全に弾く。
    fields = {"clicks": "1.0", "impressions": "5.0", "ctr": "0.2", "position": "3.4"}
    fields[field] = token
    body = (
        '{"rows": [{"keys": ["2026-09-01", "https://x/a"], '
        f'"clicks": {fields["clicks"]}, "impressions": {fields["impressions"]}, '
        f'"ctr": {fields["ctr"]}, "position": {fields["position"]}}}]}}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "application/json"})

    provider = _provider(handler, patch_loader)
    with pytest.raises(ExternalProviderDataError):
        provider.fetch_page_daily(property_uri=_PROP, start_date=_START, end_date=_END)


@pytest.mark.parametrize(
    "overrides",
    [{"clicks": 0, "impressions": 0}, {"clicks": 7.0, "impressions": 100.0}],
)
def test_integral_float_counts_accepted(patch_loader, overrides) -> None:
    handler, _ = _single(rows=[_row(["2026-09-01", "https://x/a"], **overrides)])
    provider = _provider(handler, patch_loader)
    (row,) = provider.fetch_page_daily(property_uri=_PROP, start_date=_START, end_date=_END)
    assert isinstance(row.clicks, int) and isinstance(row.impressions, int)


# ==========================================================================
# HTTP / transport error mapping
# ==========================================================================
@pytest.mark.parametrize(
    ("status", "exc_type", "needle"),
    [
        (401, ExternalProviderError, "401"),
        (403, ExternalProviderError, "403"),
        (500, ExternalProviderDataError, "status 500"),
    ],
)
def test_http_status_mapping(patch_loader, status, exc_type, needle) -> None:
    handler, _ = _single(status=status)
    provider = _provider(handler, patch_loader)
    with pytest.raises(exc_type, match=needle) as exc:
        provider.fetch_page_daily(property_uri=_PROP, start_date=_START, end_date=_END)
    assert "fake-access-token" not in str(exc.value)
    assert "Bearer" not in str(exc.value)


def test_timeout_mapping(patch_loader) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow", request=request)

    provider = _provider(handler, patch_loader)
    with pytest.raises(ExternalProviderError, match="timed out"):
        provider.fetch_query_daily(property_uri=_PROP, start_date=_START, end_date=_END)


def test_connection_failure_mapping(patch_loader) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    provider = _provider(handler, patch_loader)
    with pytest.raises(ExternalProviderError, match="connection failed"):
        provider.fetch_page_daily(property_uri=_PROP, start_date=_START, end_date=_END)


def test_non_json_body_rejected(patch_loader) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>nope</html>")

    provider = _provider(handler, patch_loader)
    with pytest.raises(ExternalProviderDataError):
        provider.fetch_page_daily(property_uri=_PROP, start_date=_START, end_date=_END)


def test_non_object_json_rejected(patch_loader) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    provider = _provider(handler, patch_loader)
    with pytest.raises(ExternalProviderDataError, match="shape"):
        provider.fetch_page_daily(property_uri=_PROP, start_date=_START, end_date=_END)


def test_rows_not_a_list_rejected(patch_loader) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rows": {"nope": 1}})

    provider = _provider(handler, patch_loader)
    with pytest.raises(ExternalProviderDataError, match="rows is not a list"):
        provider.fetch_page_daily(property_uri=_PROP, start_date=_START, end_date=_END)


def test_refresh_failure_has_no_secret(patch_loader) -> None:
    handler, _ = _single(rows=[])
    provider = _provider(
        handler,
        patch_loader,
        creds=_FakeCreds(refresh_exc=RuntimeError("invalid_grant: private key rejected")),
    )
    with pytest.raises(ExternalProviderError, match="authentication failed") as exc:
        provider.fetch_page_daily(property_uri=_PROP, start_date=_START, end_date=_END)
    assert "private key rejected" not in str(exc.value)


# ==========================================================================
# token exchange behaviour
# ==========================================================================
def test_token_refreshed_once_across_two_queries(patch_loader) -> None:
    handler, calls = _paging_handler([[], []])
    creds = _FakeCreds()
    provider = _provider(handler, patch_loader, creds=creds, row_limit=2)
    provider.fetch_page_daily(property_uri=_PROP, start_date=_START, end_date=_END)
    provider.fetch_query_daily(property_uri=_PROP, start_date=_START, end_date=_END)
    assert creds.refresh_calls == 1
    assert provider.token_refresh_count == 1
    assert provider.search_console_request_count == 2


# ==========================================================================
# LIVE PROBE method
# ==========================================================================
def test_probe_query_makes_exactly_one_request_rowlimit_1_no_pagination(patch_loader) -> None:
    full = [_row(["2026-09-01", "https://x/a"]), _row(["2026-09-01", "https://x/b"])]
    handler, calls = _paging_handler([full, full])  # would paginate if it were fetch_*
    provider = _provider(handler, patch_loader, row_limit=2)
    result = provider.probe_query(
        property_uri=_PROP,
        start_date=_START,
        end_date=_END,
        dimensions=("date", "page"),
    )
    assert len(calls) == 1
    body = json.loads(calls[0].content)
    assert body["rowLimit"] == 1
    assert body["startRow"] == 0
    assert body["dimensions"] == ["date", "page"]
    assert result.http_ok is True
    assert result.row_count == 2
    assert result.rows_parsed_ok is True


def test_probe_query_reports_response_aggregation_type(patch_loader) -> None:
    handler, _ = _single(rows=[], agg="byProperty")
    provider = _provider(handler, patch_loader)
    result = provider.probe_query(
        property_uri=_PROP,
        start_date=_START,
        end_date=_END,
        dimensions=("date", "page", "query"),
    )
    assert result.response_aggregation_type == "byProperty"
    assert result.row_count == 0


def test_probe_query_403_raises_provider_error(patch_loader) -> None:
    handler, _ = _single(status=403)
    provider = _provider(handler, patch_loader)
    with pytest.raises(ExternalProviderError, match="403"):
        provider.probe_query(
            property_uri=_PROP,
            start_date=_START,
            end_date=_END,
            dimensions=("date", "page"),
        )


# ==========================================================================
# read-only guarantees (static)
# ==========================================================================
def test_provider_public_surface_is_read_only() -> None:
    public = {n for n in dir(gp.GoogleSearchConsoleProvider) if not n.startswith("_")}
    assert public == {
        "fetch_page_daily",
        "fetch_query_daily",
        "probe_query",
        "service_account_email",
        "search_console_request_count",
        "token_refresh_count",
    }


def test_module_issues_no_mutating_http_verbs() -> None:
    src = Path(gp.__file__).read_text(encoding="utf-8")
    for verb in (".put(", ".patch(", ".delete("):
        assert verb not in src, f"unexpected mutating verb {verb!r}"
    # POST は search analytics query endpoint 1 か所のみ
    assert src.count(".post(") == 1
    assert "searchAnalytics/query" in src


def test_module_does_not_touch_database() -> None:
    src = Path(gp.__file__).read_text(encoding="utf-8").lower()
    assert "sqlalchemy" not in src
    assert "session" not in src


def test_satisfies_search_console_provider_protocol(patch_loader) -> None:
    from app.search_console.provider import SearchConsoleProvider

    handler, _ = _single(rows=[])
    provider = _provider(handler, patch_loader)
    assert isinstance(provider, SearchConsoleProvider)
