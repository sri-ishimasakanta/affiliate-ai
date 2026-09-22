"""Google Analytics Data API (GA4) の **read-only** provider。

- endpoint は固定: ``POST /v1beta/properties/{property}:runReport``。HTTP は POST
  だが **レポート照会** であって mutation ではない。プロパティ設定を変更する
  surface は持たない (Admin API は timezone の読み取りにのみ使う)。
- OAuth scope は :data:`ANALYTICS_READONLY_SCOPE` 固定。設定で広げられない。
- private_key / access token / Authorization を print / log / 例外文言に含めない。
- 全トラフィック (``all``) と オーガニック検索 (``organic_search``) を **別々の
  レポート照会** として取得し、行の ``channel_scope`` で区別する。同じ照会結果を
  按分したり推定したりはしない。

**完全性に関する注意**: GA4 は cardinality が高い場合に ``(other)`` 行へ丸める
ことがある。pagination を完遂しても「全ページの完全な一覧」を保証はしない。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import httpx

from app.analytics.credentials import load_analytics_readonly_credentials
from app.analytics.import_identity import (
    CHANNEL_SCOPE_ALL,
    CHANNEL_SCOPE_ORGANIC_SEARCH,
    ORGANIC_DIMENSION,
    ORGANIC_DIMENSION_VALUE,
    PAGE_DIMENSIONS,
    PAGE_METRICS,
)
from app.analytics.rows import (
    Ga4PageRow,
    coerce_count,
    coerce_ratio,
    coerce_seconds,
    normalize_page_path,
    parse_ga4_date,
)
from app.exceptions import ExternalProviderDataError, ExternalProviderError
from app.search_console.google_client import _HttpxAuthRequest  # C1-A auth transport 再利用

_PROVIDER = "ga4"
_DATA_API = "https://analyticsdata.googleapis.com/v1beta"
_ADMIN_API = "https://analyticsadmin.googleapis.com/v1beta"
_TIMEOUT_SECONDS = 60.0
_PAGE_SIZE = 100_000


def normalize_property_id(value: str) -> str:
    """``properties/123`` / ``123`` のどちらでも数値 id に正規化する。"""

    text = (value or "").strip()
    if text.startswith("properties/"):
        text = text[len("properties/") :]
    if not text.isdigit():
        raise ExternalProviderError(_PROVIDER, "GA4 property id must be numeric")
    return text


class GoogleGa4Provider:
    def __init__(self, settings, *, http_client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._http_client = http_client
        self._credentials = None

    # -- auth ----------------------------------------------------------------
    def _token(self) -> str:
        if self._credentials is None:
            self._credentials, _facts = load_analytics_readonly_credentials(
                self._settings.search_console_credentials_file
            )
        if not self._credentials.valid:
            try:
                self._credentials.refresh(_HttpxAuthRequest())
            except Exception as exc:  # noqa: BLE001 - 中身 (鍵) は載せない
                raise ExternalProviderError(_PROVIDER, "failed to obtain an access token") from exc
        token = self._credentials.token
        if not token:
            raise ExternalProviderError(_PROVIDER, "failed to obtain an access token")
        return token

    def _post(self, url: str, payload: dict) -> dict:
        owns = self._http_client is None
        http = self._http_client or httpx.Client(timeout=_TIMEOUT_SECONDS)
        try:
            response = http.post(
                url, json=payload, headers={"Authorization": f"Bearer {self._token()}"}
            )
        except httpx.HTTPError as exc:
            if owns:
                http.close()
            raise ExternalProviderError(_PROVIDER, "transport failure") from exc
        try:
            if response.status_code != 200:
                raise ExternalProviderError(_PROVIDER, _safe_error(response, "runReport failed"))
            try:
                body = response.json()
            except ValueError as exc:
                raise ExternalProviderDataError(_PROVIDER, "response is not JSON") from exc
            if not isinstance(body, dict):
                raise ExternalProviderDataError(_PROVIDER, "response is not an object")
            return body
        finally:
            if owns:
                http.close()

    # -- Ga4Provider ---------------------------------------------------------
    def fetch_property_timezone(self, *, property_id: str) -> str | None:
        """Admin API から property のタイムゾーンを読む (失敗しても ``None``)。"""

        numeric = normalize_property_id(property_id)
        owns = self._http_client is None
        http = self._http_client or httpx.Client(timeout=_TIMEOUT_SECONDS)
        try:
            response = http.get(
                f"{_ADMIN_API}/properties/{numeric}",
                headers={"Authorization": f"Bearer {self._token()}"},
            )
            if response.status_code != 200:
                return None
            body = response.json()
            zone = body.get("timeZone") if isinstance(body, dict) else None
            return zone if isinstance(zone, str) and zone else None
        except Exception:  # noqa: BLE001 - timezone は補助情報。失敗しても取り込みは続行
            return None
        finally:
            if owns:
                http.close()

    def fetch_page_daily(
        self, *, property_id: str, start_date: date, end_date: date
    ) -> Sequence[Ga4PageRow]:
        numeric = normalize_property_id(property_id)
        rows: list[Ga4PageRow] = []
        rows.extend(self._run_report(numeric, start_date, end_date, CHANNEL_SCOPE_ALL))
        rows.extend(self._run_report(numeric, start_date, end_date, CHANNEL_SCOPE_ORGANIC_SEARCH))
        return rows

    # -- internals -----------------------------------------------------------
    def _run_report(
        self, property_id: str, start_date: date, end_date: date, channel_scope: str
    ) -> list[Ga4PageRow]:
        rows: list[Ga4PageRow] = []
        offset = 0
        while True:
            payload: dict = {
                "dateRanges": [
                    {"startDate": start_date.isoformat(), "endDate": end_date.isoformat()}
                ],
                "dimensions": [{"name": d} for d in PAGE_DIMENSIONS],
                "metrics": [{"name": m} for m in PAGE_METRICS],
                "limit": _PAGE_SIZE,
                "offset": offset,
                "keepEmptyRows": False,
            }
            if channel_scope == CHANNEL_SCOPE_ORGANIC_SEARCH:
                payload["dimensionFilter"] = {
                    "filter": {
                        "fieldName": ORGANIC_DIMENSION,
                        "stringFilter": {
                            "matchType": "EXACT",
                            "value": ORGANIC_DIMENSION_VALUE,
                            "caseSensitive": True,
                        },
                    }
                }
            body = self._post(f"{_DATA_API}/properties/{property_id}:runReport", payload)
            batch = body.get("rows") or []
            if not isinstance(batch, list):
                raise ExternalProviderDataError(_PROVIDER, "rows is not a list")
            for raw in batch:
                rows.append(_parse_row(raw, channel_scope))
            total = body.get("rowCount")
            offset += len(batch)
            if not batch or not isinstance(total, int) or offset >= total:
                break
        return rows


def _parse_row(raw: object, channel_scope: str) -> Ga4PageRow:
    if not isinstance(raw, dict):
        raise ExternalProviderDataError(_PROVIDER, "row is not an object")
    dimensions = raw.get("dimensionValues")
    metrics = raw.get("metricValues")
    if not isinstance(dimensions, list) or len(dimensions) != len(PAGE_DIMENSIONS):
        raise ExternalProviderDataError(_PROVIDER, "unexpected dimension count")
    if not isinstance(metrics, list) or len(metrics) != len(PAGE_METRICS):
        raise ExternalProviderDataError(_PROVIDER, "unexpected metric count")
    values = [d.get("value") if isinstance(d, dict) else None for d in dimensions]
    numbers = [m.get("value") if isinstance(m, dict) else None for m in metrics]
    sessions = coerce_count(numbers[0], field="sessions")
    engagement_duration = coerce_seconds(numbers[5], field="userEngagementDuration")
    active_users = coerce_count(numbers[1], field="activeUsers")
    return Ga4PageRow(
        metric_date=parse_ga4_date(values[0]),
        page_path=normalize_page_path(values[1]),
        channel_scope=channel_scope,
        sessions=sessions,
        active_users=active_users,
        new_users=coerce_count(numbers[2], field="newUsers"),
        engaged_sessions=coerce_count(numbers[3], field="engagedSessions"),
        engagement_rate=coerce_ratio(numbers[4], field="engagementRate"),
        # GA4 の userEngagementDuration は合計秒。1 ユーザーあたりの平均に直す
        # (GA4 UI の "平均エンゲージメント時間" と同じ定義)。
        average_engagement_time_seconds=(
            engagement_duration / active_users if active_users else 0.0
        ),
        screen_page_views=coerce_count(numbers[6], field="screenPageViews"),
    )


def _safe_error(response: httpx.Response, fallback: str) -> str:
    """Google のエラー本文から **status と短い message だけ** を取り出す。"""

    try:
        error = (response.json() or {}).get("error") or {}
    except Exception:  # noqa: BLE001
        return f"{fallback} (HTTP {response.status_code})"
    status = error.get("status") or f"HTTP_{response.status_code}"
    message = (error.get("message") or "")[:200]
    return f"{fallback}: {status} {message}".strip()
