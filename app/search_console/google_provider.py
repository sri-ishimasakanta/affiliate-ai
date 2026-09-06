"""Google Search Console **read-only** Search Analytics provider (C1-B)。

``SearchConsoleProvider`` Protocol の具象実装。

- endpoint は固定: ``POST /webmasters/v3/sites/{siteUrl}/searchAnalytics/query``。
  HTTP は POST だが Search Console の **query** であって mutation ではない。
- OAuth scope は :data:`SEARCH_CONSOLE_READONLY_SCOPE` 固定。設定で広げられない。
- credential 検証 / 読み込み / auth transport は C1-A (:mod:`app.search_console.
  credentials` / :mod:`app.search_console.google_client`) を再利用する。
- private_key / private_key_id / access token / Authorization / credential JSON を
  print / log / 例外文言 / snapshot に一切含めない。
- sites.add / sites.delete / sitemaps.* / URL inspection / user 権限変更や
  任意 endpoint 実行の surface を持たない。PUT / PATCH / DELETE を出さない。

**完全性に関する注意**: Search Analytics API は内部的に上限があり、pagination を
完遂しても Google は「取得可能な全行」を返すことを保証しない (特に query 次元)。
したがって ``pagination 完了 != Search Console query データ完全``。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from urllib.parse import quote

import httpx

from app.exceptions import ExternalProviderDataError, ExternalProviderError
from app.search_console.credentials import (
    SEARCH_CONSOLE_READONLY_SCOPE,
    load_readonly_credentials,
)
from app.search_console.google_client import _HttpxAuthRequest  # C1-A auth transport 再利用
from app.search_console.import_identity import (
    AGGREGATION_TYPE,
    DATA_STATE,
    PAGE_DIMENSIONS,
    QUERY_DIMENSIONS,
    SEARCH_TYPE,
)
from app.search_console.rows import (
    SearchConsolePageRow,
    SearchConsoleQueryRow,
    coerce_count,
    coerce_ratio,
)

_PROVIDER = "search_console"
_QUERY_URL = "https://www.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query"
_TIMEOUT_SECONDS = 30.0

#: production の 1 ページあたり行数。identity には含めない (意味を変えないため)。
PRODUCTION_ROW_LIMIT = 25_000

#: 無限ループに対する防御的不変条件。超過時は **truncate せず** 例外にする。
_MAX_PAGES = 1_000

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class _RawQueryResponse:
    rows: list[dict]
    response_aggregation_type: str | None


@dataclass(frozen=True)
class ProbeQueryResult:
    """LIVE PROBE 専用の安全な結果。生の行や検索クエリ文字列は保持しない。"""

    dimensions: tuple[str, ...]
    http_ok: bool
    row_count: int
    rows_parsed_ok: bool
    response_aggregation_type: str | None


def _parse_provider_date(value: object) -> date:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ExternalProviderDataError(_PROVIDER, "row date key is not YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ExternalProviderDataError(
            _PROVIDER, "row date key is not a valid date"
        ) from None


def _keys_of(raw: object, *, expected: int) -> list:
    if not isinstance(raw, dict):
        raise ExternalProviderDataError(_PROVIDER, "search analytics row is not an object")
    keys = raw.get("keys")
    if not isinstance(keys, list) or len(keys) != expected:
        raise ExternalProviderDataError(
            _PROVIDER, f"search analytics row must have exactly {expected} keys"
        )
    return keys


def _require_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExternalProviderDataError(_PROVIDER, "row page key is empty")
    return value


class GoogleSearchConsoleProvider:
    """検証済み service-account credential を使う read-only Search Analytics provider。"""

    def __init__(
        self,
        *,
        credentials_file: str | None,
        transport: httpx.BaseTransport | None = None,
        row_limit: int = PRODUCTION_ROW_LIMIT,
    ) -> None:
        self._transport = transport
        self._row_limit = int(row_limit)
        self._credentials, self._facts = load_readonly_credentials(credentials_file)
        if list(getattr(self._credentials, "scopes", []) or []) != [
            SEARCH_CONSOLE_READONLY_SCOPE
        ]:
            raise ExternalProviderError(
                _PROVIDER, "credentials are not scoped to webmasters.readonly only"
            )
        self._search_console_request_count = 0
        self._token_refresh_count = 0

    # -- observability (secret を含まない) ---------------------------
    @property
    def service_account_email(self) -> str:
        return self._facts.client_email

    @property
    def search_console_request_count(self) -> int:
        """このインスタンスが送った Search Console API リクエスト数 (token 交換を除く)。"""

        return self._search_console_request_count

    @property
    def token_refresh_count(self) -> int:
        return self._token_refresh_count

    # -- SearchConsoleProvider Protocol -----------------------------
    def fetch_page_daily(
        self, *, property_uri: str, start_date: date, end_date: date
    ) -> list[SearchConsolePageRow]:
        raws = self._paginate(
            property_uri=property_uri,
            start_date=start_date,
            end_date=end_date,
            dimensions=PAGE_DIMENSIONS,
        )
        return [self._to_page_row(r) for r in raws]

    def fetch_query_daily(
        self, *, property_uri: str, start_date: date, end_date: date
    ) -> list[SearchConsoleQueryRow]:
        raws = self._paginate(
            property_uri=property_uri,
            start_date=start_date,
            end_date=end_date,
            dimensions=QUERY_DIMENSIONS,
        )
        return [self._to_query_row(r) for r in raws]

    # -- LIVE PROBE 専用 (1 リクエスト・pagination なし) --------------
    def probe_query(
        self,
        *,
        property_uri: str,
        start_date: date,
        end_date: date,
        dimensions: Sequence[str],
    ) -> ProbeQueryResult:
        dims = tuple(dimensions)
        raw = self._execute_query(
            property_uri=property_uri,
            start_date=start_date,
            end_date=end_date,
            dimensions=dims,
            row_limit=1,
            start_row=0,
        )
        parsed_ok = True
        try:
            for r in raw.rows:
                if len(dims) == len(QUERY_DIMENSIONS):
                    self._to_query_row(r)
                else:
                    self._to_page_row(r)
        except ExternalProviderDataError:
            parsed_ok = False
        return ProbeQueryResult(
            dimensions=dims,
            http_ok=True,
            row_count=len(raw.rows),
            rows_parsed_ok=parsed_ok,
            response_aggregation_type=raw.response_aggregation_type,
        )

    # -- internals -------------------------------------------------
    def _build_body(
        self,
        *,
        start_date: date,
        end_date: date,
        dimensions: Sequence[str],
        row_limit: int,
        start_row: int,
    ) -> dict:
        return {
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "dimensions": list(dimensions),
            "type": SEARCH_TYPE,
            "aggregationType": AGGREGATION_TYPE,
            "dataState": DATA_STATE,
            "rowLimit": row_limit,
            "startRow": start_row,
        }

    def _query_url(self, property_uri: str) -> str:
        return _QUERY_URL.format(site=quote(property_uri, safe=""))

    def _paginate(
        self,
        *,
        property_uri: str,
        start_date: date,
        end_date: date,
        dimensions: Sequence[str],
    ) -> list[dict]:
        if start_date > end_date:
            raise ExternalProviderError(_PROVIDER, "start_date is after end_date")
        out: list[dict] = []
        start_row = 0
        for _ in range(_MAX_PAGES):
            resp = self._execute_query(
                property_uri=property_uri,
                start_date=start_date,
                end_date=end_date,
                dimensions=dimensions,
                row_limit=self._row_limit,
                start_row=start_row,
            )
            out.extend(resp.rows)
            if len(resp.rows) < self._row_limit:
                return out
            start_row += len(resp.rows)
        raise ExternalProviderDataError(
            _PROVIDER, "search analytics pagination exceeded the defensive page limit"
        )

    def _execute_query(
        self,
        *,
        property_uri: str,
        start_date: date,
        end_date: date,
        dimensions: Sequence[str],
        row_limit: int,
        start_row: int,
    ) -> _RawQueryResponse:
        token = self._obtain_access_token()
        body = self._build_body(
            start_date=start_date,
            end_date=end_date,
            dimensions=dimensions,
            row_limit=row_limit,
            start_row=start_row,
        )
        url = self._query_url(property_uri)

        self._search_console_request_count += 1
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=_TIMEOUT_SECONDS,
                follow_redirects=False,
            ) as client:
                resp = client.post(
                    url, json=body, headers={"Authorization": f"Bearer {token}"}
                )
        except httpx.TimeoutException as exc:
            raise ExternalProviderError(_PROVIDER, "request timed out") from exc
        except httpx.TransportError as exc:
            raise ExternalProviderError(_PROVIDER, "connection failed") from exc
        except httpx.HTTPError as exc:  # pragma: no cover - 予備
            raise ExternalProviderError(_PROVIDER, "request failed") from exc

        if resp.status_code == 401:
            raise ExternalProviderError(_PROVIDER, "authentication failed (401)")
        if resp.status_code == 403:
            raise ExternalProviderError(_PROVIDER, "permission denied (403)")
        if resp.status_code != 200:
            raise ExternalProviderDataError(
                _PROVIDER, f"unexpected response status {resp.status_code}"
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise ExternalProviderDataError(
                _PROVIDER, "response was not valid JSON"
            ) from exc
        if not isinstance(data, dict):
            raise ExternalProviderDataError(_PROVIDER, "unexpected response shape")

        rows_raw = data.get("rows")
        if rows_raw is None:
            rows: list[dict] = []
        elif isinstance(rows_raw, list):
            rows = rows_raw
        else:
            raise ExternalProviderDataError(
                _PROVIDER, "search analytics rows is not a list"
            )

        agg = data.get("responseAggregationType")
        agg = agg if isinstance(agg, str) else None
        return _RawQueryResponse(rows=rows, response_aggregation_type=agg)

    def _to_page_row(self, raw: dict) -> SearchConsolePageRow:
        keys = _keys_of(raw, expected=len(PAGE_DIMENSIONS))
        return SearchConsolePageRow(
            metric_date=_parse_provider_date(keys[0]),
            page=_require_url(keys[1]),
            clicks=coerce_count(raw.get("clicks"), field="clicks"),
            impressions=coerce_count(raw.get("impressions"), field="impressions"),
            ctr=coerce_ratio(raw.get("ctr"), field="ctr", low=0.0, high=1.0),
            position=coerce_ratio(raw.get("position"), field="position", low=0.0),
        )

    def _to_query_row(self, raw: dict) -> SearchConsoleQueryRow:
        keys = _keys_of(raw, expected=len(QUERY_DIMENSIONS))
        query = keys[2]
        if not isinstance(query, str) or not query.strip():
            raise ExternalProviderDataError(_PROVIDER, "row query key is empty")
        return SearchConsoleQueryRow(
            metric_date=_parse_provider_date(keys[0]),
            page=_require_url(keys[1]),
            query=query,
            clicks=coerce_count(raw.get("clicks"), field="clicks"),
            impressions=coerce_count(raw.get("impressions"), field="impressions"),
            ctr=coerce_ratio(raw.get("ctr"), field="ctr", low=0.0, high=1.0),
            position=coerce_ratio(raw.get("position"), field="position", low=0.0),
        )

    def _obtain_access_token(self) -> str:
        if not getattr(self._credentials, "valid", False):
            try:
                self._credentials.refresh(_HttpxAuthRequest())
            except ExternalProviderError:
                raise
            except Exception as exc:  # RefreshError 等。token / key は載せない。
                raise ExternalProviderError(
                    _PROVIDER, "service-account authentication failed"
                ) from exc
            self._token_refresh_count += 1
        token = getattr(self._credentials, "token", None)
        if not token:
            raise ExternalProviderError(_PROVIDER, "no access token obtained")
        return token
