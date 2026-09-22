"""GA4 provider が返す 1 行の typed 表現と厳格な検証 (pure)。

DB / network 非依存。GA4 の値は文字列で返るため、ここで型を確定させ、契約違反を
永続化前に弾く。エラー文言に credential / raw response は含めない。

``page_path`` の正規化方針 (明示):

- **query string と fragment は落とす**。GA4 の ``pagePath`` には ``?utm_source=...``
  等が付きうるが、同じ記事の計測をパラメータ違いで分断しないため、集計の単位は
  パス自身とする。パラメータ別の分析が必要になったら別 scope/別テーブルで扱う。
- 末尾スラッシュは **保持する** (URL 正規化は :mod:`app.seo.url_normalization` の
  比較キー側で吸収する)。ここで元の値を書き換えない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from app.exceptions import ExternalProviderDataError

_PROVIDER = "ga4"
_MAX_PATH_LENGTH = 2048


@dataclass(frozen=True)
class Ga4PageRow:
    """GA4 の (date, pagePath) x channel scope 1 行。"""

    metric_date: date
    page_path: str
    channel_scope: str
    sessions: int
    active_users: int
    new_users: int
    engaged_sessions: int
    engagement_rate: float
    average_engagement_time_seconds: float
    screen_page_views: int


def _err(reason: str) -> ExternalProviderDataError:
    return ExternalProviderDataError(_PROVIDER, reason)


def parse_ga4_date(value: object) -> date:
    """GA4 の ``date`` dimension (``YYYYMMDD``) を ``date`` にする。"""

    if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
        raise _err("date dimension is not in YYYYMMDD form")
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        raise _err("date dimension is not a real calendar date") from None


def normalize_page_path(value: object) -> str:
    """``pagePath`` を集計キーに正規化する (query / fragment を落とす)。"""

    if not isinstance(value, str) or not value.strip():
        raise _err("pagePath is missing or empty")
    path = value.strip().split("#", 1)[0].split("?", 1)[0]
    if not path:
        path = "/"
    if not path.startswith("/"):
        raise _err("pagePath must be an absolute path")
    if len(path) > _MAX_PATH_LENGTH:
        raise _err("pagePath is too long")
    return path


def coerce_count(value: object, *, field: str) -> int:
    """GA4 の整数メトリクス (文字列で返る) を非負の int にする。"""

    if value is None or value == "":
        return 0
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise _err(f"{field} is not numeric") from None
    if number < 0:
        raise _err(f"{field} is negative")
    if number != int(number):
        raise _err(f"{field} is not an integer")
    return int(number)


def coerce_ratio(value: object, *, field: str) -> float:
    """``engagementRate`` のような 0..1 の比率。"""

    if value is None or value == "":
        return 0.0
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise _err(f"{field} is not numeric") from None
    if number < 0 or number > 1:
        raise _err(f"{field} is outside 0..1")
    return number


def coerce_seconds(value: object, *, field: str) -> float:
    if value is None or value == "":
        return 0.0
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise _err(f"{field} is not numeric") from None
    if number < 0:
        raise _err(f"{field} is negative")
    return number


def validate_page_row(row: Ga4PageRow) -> Ga4PageRow:
    """永続化前の最終検証 (provider 実装に関わらず不変条件を守る)。"""

    if not isinstance(row.metric_date, date):
        raise _err("metric_date is not a date")
    if not row.page_path.startswith("/"):
        raise _err("page_path must be an absolute path")
    if row.channel_scope not in ("all", "organic_search"):
        raise _err(f"unsupported channel_scope: {row.channel_scope!r}")
    for field, value in (
        ("sessions", row.sessions),
        ("active_users", row.active_users),
        ("new_users", row.new_users),
        ("engaged_sessions", row.engaged_sessions),
        ("screen_page_views", row.screen_page_views),
    ):
        if value < 0:
            raise _err(f"{field} is negative")
    if row.engaged_sessions > row.sessions:
        raise _err("engaged_sessions exceeds sessions")
    if not 0.0 <= row.engagement_rate <= 1.0:
        raise _err("engagement_rate is outside 0..1")
    if row.average_engagement_time_seconds < 0:
        raise _err("average_engagement_time_seconds is negative")
    return row
