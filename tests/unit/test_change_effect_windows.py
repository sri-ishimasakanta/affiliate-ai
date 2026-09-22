"""効果追跡の窓と成熟度 (C9.3 / C9.5、pure)。

pin する契約:

- 暦日は **レポートタイムゾーン** で決める (UTC の暦日ではない)。
- 変更当日はどちらの窓にも入らない。
- 窓が経過していない/取り込みが届いていなければ ``insufficient_data``。
- 変更前に観測が無ければ ``insufficient_data``。
- 露出が少なすぎれば ``insufficient_data``。
- 片側が欠損している指標の差は ``None`` (0 を作らない)。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.change.effect import (
    BELOW_MINIMUM_VOLUME,
    EFFECT_INSUFFICIENT_DATA,
    EFFECT_OBSERVED,
    NO_PRE_DATA,
    POST_WINDOW_NOT_COVERED,
    POST_WINDOW_NOT_ELAPSED,
    assess_maturity,
    build_windows,
    delta,
    local_effective_date,
)

_TOKYO = ZoneInfo("Asia/Tokyo")
_LA = ZoneInfo("America/Los_Angeles")
_UTC = ZoneInfo("UTC")

#: 変更は UTC で 6/1 12:00 (= JST 6/1 21:00)。暦日は両者で同じ。
_CHANGE = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _windows(*, today: date, coverage: date | None, days: int = 7, at=_CHANGE, tz=_TOKYO):
    return build_windows(
        change_at=at,
        reporting_timezone=tz,
        today=today,
        window_days=days,
        coverage_through=coverage,
    )


# -- C9.5: calendar boundary uses the reporting timezone -----------------------
def test_a_late_utc_change_belongs_to_the_next_local_day() -> None:
    """本番で起きた形: UTC では 9/22 だが Asia/Tokyo では 9/23。"""

    moment = datetime(2026, 9, 22, 17, 43, 41, tzinfo=UTC)

    assert moment.date() == date(2026, 9, 22)
    assert local_effective_date(moment, _TOKYO) == date(2026, 9, 23)


def test_a_naive_timestamp_is_read_as_utc() -> None:
    """SQLite は tzinfo を落とすので、naive は UTC とみなす。"""

    naive = datetime(2026, 9, 22, 17, 43, 41)

    assert local_effective_date(naive, _TOKYO) == date(2026, 9, 23)


def test_production_application_2_windows() -> None:
    """application 2 (2026-09-22T17:43:41Z) の窓は 9/23 を除外する。"""

    windows = _windows(
        at=datetime(2026, 9, 22, 17, 43, 41, tzinfo=UTC),
        today=date(2026, 11, 1),
        coverage=date(2026, 11, 1),
        days=28,
    )

    assert windows.effective_date == date(2026, 9, 23)
    assert windows.timezone_name == "Asia/Tokyo"
    assert (windows.pre.start, windows.pre.end) == (date(2026, 8, 26), date(2026, 9, 22))
    assert (windows.post.start, windows.post.end) == (date(2026, 9, 24), date(2026, 10, 21))
    # 変更当日 (JST) は post 窓に入らない -- これが C9.5 の要点。
    assert not windows.post.contains(date(2026, 9, 23))
    assert not windows.pre.contains(date(2026, 9, 23))


def test_another_timezone_shifts_the_boundary_the_other_way() -> None:
    """UTC 早朝の変更は America/Los_Angeles では前日になる。"""

    moment = datetime(2026, 9, 23, 3, 0, tzinfo=UTC)

    assert local_effective_date(moment, _LA) == date(2026, 9, 22)
    assert local_effective_date(moment, _TOKYO) == date(2026, 9, 23)
    assert local_effective_date(moment, _UTC) == date(2026, 9, 23)

    windows = _windows(at=moment, tz=_LA, today=date(2026, 11, 1), coverage=date(2026, 11, 1))
    assert windows.effective_date == date(2026, 9, 22)
    assert windows.post.start == date(2026, 9, 23)


def test_the_conversion_survives_a_dst_transition() -> None:
    """手で +N 時間足していないこと (DST のある tz でも正しい)。"""

    # 2026-11-01 は America/Los_Angeles の DST 終了日 (UTC-7 -> UTC-8)。
    before = datetime(2026, 11, 1, 7, 30, tzinfo=UTC)  # PDT 00:30 (11/1)
    after = datetime(2026, 11, 2, 7, 30, tzinfo=UTC)  # PST 23:30 (11/1)

    assert local_effective_date(before, _LA) == date(2026, 11, 1)
    assert local_effective_date(after, _LA) == date(2026, 11, 1)


# -- window shape --------------------------------------------------------------
def test_change_day_belongs_to_neither_window() -> None:
    windows = _windows(today=date(2026, 7, 1), coverage=date(2026, 7, 1))

    assert windows.effective_date == date(2026, 6, 1)
    assert windows.pre.end == date(2026, 5, 31)
    assert windows.post.start == date(2026, 6, 2)
    assert not windows.pre.contains(windows.effective_date)
    assert not windows.post.contains(windows.effective_date)


def test_windows_have_the_same_length() -> None:
    windows = _windows(today=date(2026, 7, 1), coverage=date(2026, 7, 1), days=28)

    assert windows.pre.days == windows.post.days == 28


# -- maturity ------------------------------------------------------------------
def test_insufficient_when_post_window_has_not_elapsed() -> None:
    w = _windows(today=date(2026, 6, 3), coverage=date(2026, 6, 3))
    verdict = assess_maturity(
        pre=w.pre,
        post=w.post,
        today=date(2026, 6, 3),
        pre_impressions=500,
        post_impressions=500,
        pre_rows=7,
    )

    assert verdict.status == EFFECT_INSUFFICIENT_DATA
    assert POST_WINDOW_NOT_ELAPSED in verdict.reasons


def test_insufficient_when_imports_have_not_covered_the_post_window() -> None:
    """窓は経過しているが coverage が届いていない (活動ではなく coverage で見る)。"""

    w = _windows(today=date(2026, 7, 1), coverage=date(2026, 6, 5))
    verdict = assess_maturity(
        pre=w.pre,
        post=w.post,
        today=date(2026, 7, 1),
        pre_impressions=500,
        post_impressions=500,
        pre_rows=7,
    )

    assert verdict.status == EFFECT_INSUFFICIENT_DATA
    assert POST_WINDOW_NOT_COVERED in verdict.reasons


def test_insufficient_without_pre_change_measurement() -> None:
    w = _windows(today=date(2026, 7, 1), coverage=date(2026, 7, 1))
    verdict = assess_maturity(
        pre=w.pre,
        post=w.post,
        today=date(2026, 7, 1),
        pre_impressions=0,
        post_impressions=900,
        pre_rows=0,
    )

    assert verdict.status == EFFECT_INSUFFICIENT_DATA
    assert NO_PRE_DATA in verdict.reasons


def test_insufficient_below_minimum_volume() -> None:
    w = _windows(today=date(2026, 7, 1), coverage=date(2026, 7, 1))
    verdict = assess_maturity(
        pre=w.pre,
        post=w.post,
        today=date(2026, 7, 1),
        pre_impressions=3,
        post_impressions=5,
        pre_rows=4,
        minimum_impressions=30,
    )

    assert verdict.status == EFFECT_INSUFFICIENT_DATA
    assert BELOW_MINIMUM_VOLUME in verdict.reasons


def test_observed_only_when_everything_is_satisfied() -> None:
    w = _windows(today=date(2026, 7, 1), coverage=date(2026, 7, 1))
    verdict = assess_maturity(
        pre=w.pre,
        post=w.post,
        today=date(2026, 7, 1),
        pre_impressions=400,
        post_impressions=460,
        pre_rows=7,
    )

    assert verdict.status == EFFECT_OBSERVED
    assert verdict.sufficient
    assert verdict.reasons == []


def test_delta_requires_both_sides() -> None:
    assert delta(10, 12) == 2.0
    assert delta(None, 12) is None
    assert delta(10, None) is None
