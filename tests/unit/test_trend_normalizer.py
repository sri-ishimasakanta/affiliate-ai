"""TrendNormalizer V1 の unit テスト (DB / SDK / FastAPI 非依存)。"""

import pytest

from app.keyword.normalizers.trend import (
    MIN_MONTHS,
    NORMALIZER_NAME,
    NORMALIZER_VERSION,
    TrendResult,
    calculate_trend,
    prepare_monthly_series,
    trend_from_monthly_searches,
)
from app.keyword.providers.google_ads import MonthlySearchVolume


def _months(
    values: list[int | None], *, start_year: int = 2025, start_month: int = 1
) -> list[MonthlySearchVolume]:
    """連番の年月を振って monthly_search_volumes を作る (None もそのまま入れられる)。"""

    out: list[MonthlySearchVolume] = []
    year, month = start_year, start_month
    for value in values:
        out.append(
            MonthlySearchVolume(year=year, month=month, monthly_searches=value)
        )
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return out


# -- 既知パターン (spec の例) -----------------------------------------
@pytest.mark.parametrize(
    ("series", "expected"),
    [
        ([100, 100, 100, 100, 100, 100], 50.0),   # 横ばい
        ([100, 100, 100, 150, 150, 150], 70.0),   # 上昇
        ([150, 150, 150, 100, 100, 100], 30.0),   # 下降
        ([0, 0, 0, 100, 100, 100], 100.0),        # 強い上昇 (clamp +1)
        ([100, 100, 100, 0, 0, 0], 0.0),          # 強い下降 (clamp -1)
        ([0, 0, 0, 0, 0, 0], 50.0),               # すべて 0 -> 分母下限で 50
    ],
)
def test_known_patterns_via_calculate_trend(
    series: list[int], expected: float
) -> None:
    assert calculate_trend(_months(series)).normalized_value == expected


@pytest.mark.parametrize(
    ("series", "expected"),
    [
        ([100, 100, 100, 100, 100, 100], 50.0),
        ([100, 100, 100, 150, 150, 150], 70.0),
        ([150, 150, 150, 100, 100, 100], 30.0),
        ([0, 0, 0, 100, 100, 100], 100.0),
        ([100, 100, 100, 0, 0, 0], 0.0),
        ([0, 0, 0, 0, 0, 0], 50.0),
    ],
)
def test_known_patterns_via_series(series: list[int], expected: float) -> None:
    assert trend_from_monthly_searches(series).normalized_value == expected


def test_result_fields() -> None:
    result = calculate_trend(_months([100, 100, 100, 150, 150, 150]))
    assert isinstance(result, TrendResult)
    assert result.previous_3_average == 100.0
    assert result.recent_3_average == 150.0
    assert result.change_ratio == 0.4  # 50 / 125
    assert result.months_used == 6
    assert result.available_months == 6
    assert result.normalizer_name == NORMALIZER_NAME == "trend"
    assert result.normalizer_version == NORMALIZER_VERSION == "v1"
    assert [(m.year, m.month, m.monthly_searches) for m in result.window] == [
        (2025, 1, 100),
        (2025, 2, 100),
        (2025, 3, 100),
        (2025, 4, 150),
        (2025, 5, 150),
        (2025, 6, 150),
    ]


def test_symmetric_growth_and_decline() -> None:
    up = calculate_trend(_months([100, 100, 100, 150, 150, 150])).normalized_value
    down = calculate_trend(_months([150, 150, 150, 100, 100, 100])).normalized_value
    assert up == 70.0
    assert down == 30.0
    assert up - 50.0 == pytest.approx(50.0 - down)  # 対称


# -- 6 か月超 / 並び順 ------------------------------------------------
def test_uses_only_latest_6_months() -> None:
    # 9 か月。最新 6 = [100,100,100,150,150,150] -> 70
    result = calculate_trend(
        _months([10, 20, 30, 100, 100, 100, 150, 150, 150])
    )
    assert result.normalized_value == 70.0
    assert result.available_months == 9
    assert result.months_used == 6
    assert len(result.window) == 6
    assert result.window[0].monthly_searches == 100  # 4 番目の月から


def test_unsorted_input_is_sorted_by_year_month() -> None:
    ordered = _months([100, 100, 100, 150, 150, 150])
    shuffled = [ordered[3], ordered[0], ordered[5], ordered[1], ordered[4], ordered[2]]
    assert calculate_trend(shuffled).normalized_value == 70.0


def test_sort_crosses_year_boundary() -> None:
    # 2024-11, 2024-12, 2025-01 ... と年をまたいでも年月順で並ぶ
    volumes = _months([100, 100, 100, 150, 150, 150], start_year=2024, start_month=11)
    assert calculate_trend(volumes).normalized_value == 70.0
    assert calculate_trend(list(reversed(volumes))).normalized_value == 70.0


# -- データ数・欠測・負値 -------------------------------------------
def test_exactly_6_months_ok() -> None:
    assert calculate_trend(_months([1, 2, 3, 4, 5, 6])).months_used == 6


def test_five_months_raises_insufficient() -> None:
    with pytest.raises(ValueError, match="at least 6"):
        calculate_trend(_months([1, 2, 3, 4, 5]))


def test_none_month_excluded_then_ok_with_6_remaining() -> None:
    # 7 要素、1 つ None -> 有効 6 -> success ([100,100,100,150,150,150])
    volumes = _months([None, 100, 100, 100, 150, 150, 150])
    result = calculate_trend(volumes)
    assert result.normalized_value == 70.0
    assert result.available_months == 6


def test_none_month_leaving_only_5_raises() -> None:
    volumes = _months([None, 100, 100, 100, 150, 150])
    with pytest.raises(ValueError, match="at least 6"):
        calculate_trend(volumes)


def test_negative_month_raises() -> None:
    volumes = _months([100, 100, 100, 150, 150, -5])
    with pytest.raises(ValueError, match=">= 0"):
        calculate_trend(volumes)


def test_series_negative_raises() -> None:
    with pytest.raises(ValueError, match=">= 0"):
        trend_from_monthly_searches([1, 2, 3, 4, 5, -1])


def test_series_too_short_raises() -> None:
    with pytest.raises(ValueError, match="at least 6"):
        trend_from_monthly_searches([1, 2, 3, 4, 5])


# -- prepare_monthly_series --------------------------------------
def test_prepare_monthly_series_filters_and_sorts() -> None:
    volumes = [
        MonthlySearchVolume(year=2025, month=3, monthly_searches=30),
        MonthlySearchVolume(year=2025, month=1, monthly_searches=10),
        MonthlySearchVolume(year=2025, month=2, monthly_searches=None),  # 除外
        MonthlySearchVolume(year=0, month=5, monthly_searches=99),       # 不正年 -> 除外
        MonthlySearchVolume(year=2025, month=2, monthly_searches=20),
    ]
    result = prepare_monthly_series(volumes)
    assert [(m.month, m.monthly_searches) for m in result] == [(1, 10), (2, 20), (3, 30)]


# -- 丸め・決定論 --------------------------------------------------
def test_result_rounded_to_two_decimals() -> None:
    result = calculate_trend(_months([100, 100, 100, 133, 133, 134]))
    for value in (
        result.normalized_value,
        result.previous_3_average,
        result.recent_3_average,
        result.change_ratio,
    ):
        assert round(value, 2) == value
    assert result.normalized_value == pytest.approx(64.29, abs=0.01)


def test_deterministic() -> None:
    volumes = _months([12, 34, 56, 78, 90, 42, 88])
    assert calculate_trend(volumes) == calculate_trend(volumes)


def test_growth_monotonic_in_score() -> None:
    flat = calculate_trend(_months([100, 100, 100, 100, 100, 100])).normalized_value
    mild = calculate_trend(_months([100, 100, 100, 120, 120, 120])).normalized_value
    strong = calculate_trend(_months([100, 100, 100, 200, 200, 200])).normalized_value
    assert flat == 50.0
    assert 50.0 < mild < strong


def test_min_months_constant() -> None:
    assert MIN_MONTHS == 6
