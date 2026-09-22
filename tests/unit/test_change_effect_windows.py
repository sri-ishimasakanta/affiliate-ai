"""効果追跡の窓と成熟度 (C9.3、pure)。

pin する契約:

- 変更当日はどちらの窓にも入らない。
- 窓が経過していない/取り込みが届いていなければ ``insufficient_data``。
- 変更前に観測が無ければ ``insufficient_data``。
- 露出が少なすぎれば ``insufficient_data``。
- 片側が欠損している指標の差は ``None`` (0 を作らない)。
"""

from __future__ import annotations

from datetime import date

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
)

_CHANGE = date(2026, 6, 1)


def _windows(*, today: date, coverage: date | None, days: int = 7):
    return build_windows(
        change_date=_CHANGE, today=today, window_days=days, coverage_through=coverage
    )


def test_change_day_belongs_to_neither_window() -> None:
    pre, post = _windows(today=date(2026, 7, 1), coverage=date(2026, 7, 1))

    assert pre.end == date(2026, 5, 31)
    assert post.start == date(2026, 6, 2)
    assert not pre.contains(_CHANGE)
    assert not post.contains(_CHANGE)


def test_windows_have_the_same_length() -> None:
    pre, post = _windows(today=date(2026, 7, 1), coverage=date(2026, 7, 1), days=28)

    assert pre.days == post.days == 28


def test_insufficient_when_post_window_has_not_elapsed() -> None:
    pre, post = _windows(today=date(2026, 6, 3), coverage=date(2026, 6, 3))
    verdict = assess_maturity(
        pre=pre,
        post=post,
        today=date(2026, 6, 3),
        pre_impressions=500,
        post_impressions=500,
        pre_rows=7,
    )

    assert verdict.status == EFFECT_INSUFFICIENT_DATA
    assert POST_WINDOW_NOT_ELAPSED in verdict.reasons


def test_insufficient_when_imports_have_not_covered_the_post_window() -> None:
    """窓は経過しているが coverage が届いていない (活動ではなく coverage で見る)。"""

    pre, post = _windows(today=date(2026, 7, 1), coverage=date(2026, 6, 5))
    verdict = assess_maturity(
        pre=pre,
        post=post,
        today=date(2026, 7, 1),
        pre_impressions=500,
        post_impressions=500,
        pre_rows=7,
    )

    assert verdict.status == EFFECT_INSUFFICIENT_DATA
    assert POST_WINDOW_NOT_COVERED in verdict.reasons


def test_insufficient_without_pre_change_measurement() -> None:
    pre, post = _windows(today=date(2026, 7, 1), coverage=date(2026, 7, 1))
    verdict = assess_maturity(
        pre=pre,
        post=post,
        today=date(2026, 7, 1),
        pre_impressions=0,
        post_impressions=900,
        pre_rows=0,
    )

    assert verdict.status == EFFECT_INSUFFICIENT_DATA
    assert NO_PRE_DATA in verdict.reasons


def test_insufficient_below_minimum_volume() -> None:
    pre, post = _windows(today=date(2026, 7, 1), coverage=date(2026, 7, 1))
    verdict = assess_maturity(
        pre=pre,
        post=post,
        today=date(2026, 7, 1),
        pre_impressions=3,
        post_impressions=5,
        pre_rows=4,
        minimum_impressions=30,
    )

    assert verdict.status == EFFECT_INSUFFICIENT_DATA
    assert BELOW_MINIMUM_VOLUME in verdict.reasons


def test_observed_only_when_everything_is_satisfied() -> None:
    pre, post = _windows(today=date(2026, 7, 1), coverage=date(2026, 7, 1))
    verdict = assess_maturity(
        pre=pre,
        post=post,
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
