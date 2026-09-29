"""SearchDemandNormalizer V1 の unit テスト (DB / SDK / FastAPI 非依存)。"""

from itertools import pairwise

import pytest

from app.keyword.normalizers.search_demand import (
    NORMALIZER_NAME,
    NORMALIZER_VERSION,
    normalize_search_demand,
)


@pytest.mark.parametrize(
    ("avg_monthly_searches", "expected"),
    [
        (0, 0.0),
        (1, 6.02),
        (10, 20.83),
        (100, 40.09),
        (1000, 60.01),
        (10000, 80.0),
        (100000, 100.0),
        (1000000, 100.0),
    ],
)
def test_known_values(avg_monthly_searches: int, expected: float) -> None:
    assert normalize_search_demand(avg_monthly_searches) == expected


def test_negative_raises_value_error() -> None:
    with pytest.raises(ValueError, match=">= 0"):
        normalize_search_demand(-1)


def test_monotonic_increasing() -> None:
    values = [normalize_search_demand(n) for n in (0, 1, 5, 50, 500, 5000, 50000, 500000)]
    assert values == sorted(values)
    assert all(a <= b for a, b in pairwise(values))


def test_never_exceeds_100() -> None:
    for n in (100000, 250000, 1_000_000, 50_000_000):
        assert normalize_search_demand(n) == 100.0


def test_result_is_rounded_to_two_decimals() -> None:
    for n in (7, 33, 123, 4567, 89012):
        value = normalize_search_demand(n)
        assert round(value, 2) == value


def test_deterministic() -> None:
    assert normalize_search_demand(4321) == normalize_search_demand(4321)


def test_version_metadata_constants() -> None:
    assert NORMALIZER_NAME == "search_demand"
    assert NORMALIZER_VERSION == "v2"  # C10-2: 検索量の有無の規則


# -- V2 (C10-2): 本当の 0 と欠測を分ける --------------------------------------------------
def test_v2_zero_average_without_history_is_missing_not_zero() -> None:
    from app.keyword.normalizers.search_demand import (
        MISSING,
        normalize_search_demand_v2,
        search_volume_evidence,
    )

    # 本番の実データの形: 平均 0 / 月ごとの履歴が空 (Google Ads の既定値)。
    assert search_volume_evidence(0, []) == MISSING
    assert normalize_search_demand_v2(0, []) is None
    assert search_volume_evidence(None, [10, 20]) == MISSING


def test_v2_a_real_zero_needs_enough_zero_months() -> None:
    from app.keyword.normalizers.search_demand import (
        INSUFFICIENT,
        MIN_ZERO_MONTHS,
        OBSERVED,
        OBSERVED_ZERO,
        normalize_search_demand_v2,
        search_volume_evidence,
    )

    assert search_volume_evidence(0, [0] * MIN_ZERO_MONTHS) == OBSERVED_ZERO
    assert normalize_search_demand_v2(0, [0] * 12) == 0.0
    assert search_volume_evidence(0, [0, 0]) == INSUFFICIENT
    assert normalize_search_demand_v2(0, [0, 0]) is None
    assert search_volume_evidence(0, [0, 10, 0]) == OBSERVED  # 丸めで平均 0
    assert search_volume_evidence(100, []) == OBSERVED
    assert normalize_search_demand_v2(100, []) == normalize_search_demand(100)


def test_v2_stored_v1_zero_rows_are_read_as_missing() -> None:
    from app.keyword.normalizers.search_demand import is_missing_search_demand

    assert is_missing_search_demand({"avg_monthly_searches": 0, "monthly_search_volumes": []})
    assert not is_missing_search_demand({"avg_monthly_searches": 0, "monthly_search_volumes": [
        {"year": 2026, "month": m, "monthly_searches": 0} for m in range(1, 13)]})
    assert not is_missing_search_demand({"avg_monthly_searches": 320,
                                         "monthly_search_volumes": []})
    assert not is_missing_search_demand(None) and not is_missing_search_demand({})
