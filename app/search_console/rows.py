"""Search Console provider が返す行の typed 表現と検証 (pure)。

DB / network 非依存。provider 実装 (C1 で Google client) はこの型を返す。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from app.exceptions import ExternalProviderDataError

_PROVIDER = "search_console"


def coerce_count(value: object, *, field: str) -> int:
    """Google の double 型 count (clicks / impressions) を厳格に int へ変換する。

    受理: 数値かつ bool でなく finite かつ >= 0 かつ数学的に整数 (0, 1, 1.0, 123.0)。
    拒否: -1 / 1.5 / NaN / Infinity / "1" / True。
    ``int(value)`` を検証前に呼ばない (切り捨て・丸めをしない)。
    """

    if isinstance(value, bool):
        raise ExternalProviderDataError(_PROVIDER, f"row {field} is a bool")
    if not isinstance(value, int | float):
        raise ExternalProviderDataError(_PROVIDER, f"row {field} is not numeric")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExternalProviderDataError(_PROVIDER, f"row {field} is not finite")
        if not value.is_integer():
            raise ExternalProviderDataError(
                _PROVIDER, f"row {field} is not an integral value"
            )
    if value < 0:
        raise ExternalProviderDataError(_PROVIDER, f"row {field} is negative")
    return int(value)


def coerce_ratio(
    value: object, *, field: str, low: float, high: float | None = None
) -> float:
    """Google の double 型 (ctr / position) を厳格に float へ変換する。

    provider が返した浮動小数点値をそのまま保つ (CTR は再計算しない / position は
    整数に丸めない)。bool / 非数値 / 非 finite / 範囲外は拒否する。
    """

    if isinstance(value, bool):
        raise ExternalProviderDataError(_PROVIDER, f"row {field} is a bool")
    if not isinstance(value, int | float):
        raise ExternalProviderDataError(_PROVIDER, f"row {field} is not numeric")
    v = float(value)
    if not math.isfinite(v):
        raise ExternalProviderDataError(_PROVIDER, f"row {field} is not finite")
    if v < low or (high is not None and v > high):
        raise ExternalProviderDataError(_PROVIDER, f"row {field} is out of range")
    return v


@dataclass(frozen=True)
class SearchConsolePageRow:
    """dimensions = ["date", "page"] の 1 行。"""

    metric_date: date
    page: str
    clicks: int
    impressions: int
    ctr: float
    position: float


@dataclass(frozen=True)
class SearchConsoleQueryRow:
    """dimensions = ["date", "page", "query"] の 1 行。"""

    metric_date: date
    page: str
    query: str
    clicks: int
    impressions: int
    ctr: float
    position: float


def _validate_common(
    *, metric_date: object, page: object, clicks: object, impressions: object,
    ctr: object, position: object,
) -> None:
    if not isinstance(metric_date, date):
        raise ExternalProviderDataError(_PROVIDER, "row date is not a date")
    if not isinstance(page, str) or not page.strip() or not page.startswith(("http://", "https://")):
        raise ExternalProviderDataError(_PROVIDER, "row page is not an http(s) URL")
    if not isinstance(clicks, int) or isinstance(clicks, bool) or clicks < 0:
        raise ExternalProviderDataError(_PROVIDER, "row clicks is not a non-negative int")
    if not isinstance(impressions, int) or isinstance(impressions, bool) or impressions < 0:
        raise ExternalProviderDataError(
            _PROVIDER, "row impressions is not a non-negative int"
        )
    if not isinstance(ctr, int | float) or isinstance(ctr, bool) or not (0.0 <= float(ctr) <= 1.0):
        raise ExternalProviderDataError(_PROVIDER, "row ctr is not within [0, 1]")
    if not isinstance(position, int | float) or isinstance(position, bool) or float(position) < 0.0:
        raise ExternalProviderDataError(_PROVIDER, "row position is negative")


def validate_page_row(row: SearchConsolePageRow) -> SearchConsolePageRow:
    _validate_common(
        metric_date=row.metric_date,
        page=row.page,
        clicks=row.clicks,
        impressions=row.impressions,
        ctr=row.ctr,
        position=row.position,
    )
    return row


def validate_query_row(row: SearchConsoleQueryRow) -> SearchConsoleQueryRow:
    _validate_common(
        metric_date=row.metric_date,
        page=row.page,
        clicks=row.clicks,
        impressions=row.impressions,
        ctr=row.ctr,
        position=row.position,
    )
    if not isinstance(row.query, str) or not row.query.strip():
        raise ExternalProviderDataError(_PROVIDER, "query row has an empty query")
    return row
