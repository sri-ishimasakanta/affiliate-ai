"""Search Console の日付境界ヘルパー。

Search Console の ``startDate`` / ``endDate`` は **Pacific Time** の暦日である。
JST / naive local / UTC 暦日から変換なしで日付を導出してはならない。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

SEARCH_CONSOLE_TZ = ZoneInfo("America/Los_Angeles")


def search_console_today(now: datetime | None = None) -> date:
    """現在の Search Console (Pacific Time) 暦日を返す。"""

    now = now or datetime.now(tz=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(SEARCH_CONSOLE_TZ).date()


def recent_window(
    *, days: int = 7, end_lag_days: int = 1, now: datetime | None = None
) -> tuple[date, date]:
    """PT 暦で「前日以前で終わる」直近 ``days`` 日の (start_date, end_date) を返す。

    end_date = 今日(PT) - end_lag_days、start_date = end_date - (days - 1)。
    inclusive な期間なので days=7, end_lag_days=1 なら「前日を末尾とする 7 日間」。
    """

    if days < 1:
        raise ValueError("days must be >= 1")
    if end_lag_days < 0:
        raise ValueError("end_lag_days must be >= 0")
    end_date = search_console_today(now) - timedelta(days=end_lag_days)
    start_date = end_date - timedelta(days=days - 1)
    return start_date, end_date
