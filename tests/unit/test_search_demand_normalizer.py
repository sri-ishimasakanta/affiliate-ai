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
    assert NORMALIZER_VERSION == "v1"
