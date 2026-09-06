"""app/search_console/date_window.py — Pacific Time 暦日セマンティクス。"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.search_console.date_window import recent_window, search_console_today


def test_utc_to_pt_calendar_boundary() -> None:
    # 2026-09 は PDT (UTC-7)。UTC 07:00 が PT 00:00。
    before = datetime(2026, 9, 7, 6, 59, tzinfo=UTC)
    after = datetime(2026, 9, 7, 7, 1, tzinfo=UTC)
    assert search_console_today(before) == date(2026, 9, 6)
    assert search_console_today(after) == date(2026, 9, 7)


def test_naive_now_is_treated_as_utc() -> None:
    assert search_console_today(datetime(2026, 9, 7, 6, 0)) == date(2026, 9, 6)


def test_recent_window_is_inclusive_seven_days_ending_yesterday_pt() -> None:
    now = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)  # PT 05:00 -> today = 2026-09-07
    start, end = recent_window(days=7, end_lag_days=1, now=now)
    assert end == date(2026, 9, 6)
    assert start == date(2026, 8, 31)
    assert (end - start).days == 6  # inclusive 7 日


def test_recent_window_validates_inputs() -> None:
    with pytest.raises(ValueError):
        recent_window(days=0)
    with pytest.raises(ValueError):
        recent_window(end_lag_days=-1)
