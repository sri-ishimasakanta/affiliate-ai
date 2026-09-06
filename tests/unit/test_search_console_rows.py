"""app/search_console/rows.py の厳格な数値変換境界 (coerce_count / coerce_ratio)。"""

from __future__ import annotations

import math

import pytest

from app.exceptions import ExternalProviderDataError
from app.search_console.rows import coerce_count, coerce_ratio


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0), (1, 1), (1.0, 1), (123.0, 123), (25000, 25000), (25000.0, 25000)],
)
def test_coerce_count_accepts_integral_numeric(value: object, expected: int) -> None:
    out = coerce_count(value, field="clicks")
    assert out == expected
    assert isinstance(out, int)


@pytest.mark.parametrize(
    "value",
    [
        -1,
        -1.0,
        1.5,
        0.1,
        math.nan,
        math.inf,
        -math.inf,
        "1",
        None,
        True,
        False,
    ],
)
def test_coerce_count_rejects_bad_values(value: object) -> None:
    with pytest.raises(ExternalProviderDataError):
        coerce_count(value, field="impressions")


@pytest.mark.parametrize("value", [0, 0.0, 0.25, 1, 1.0])
def test_coerce_ratio_ctr_accepts_in_range(value: object) -> None:
    out = coerce_ratio(value, field="ctr", low=0.0, high=1.0)
    assert isinstance(out, float)
    assert 0.0 <= out <= 1.0


@pytest.mark.parametrize("value", [-0.01, 1.01, 2, math.nan, math.inf, "0.5", True, None])
def test_coerce_ratio_ctr_rejects_out_of_range_or_bad(value: object) -> None:
    with pytest.raises(ExternalProviderDataError):
        coerce_ratio(value, field="ctr", low=0.0, high=1.0)


def test_coerce_ratio_position_preserves_float_and_allows_large() -> None:
    assert coerce_ratio(3.4, field="position", low=0.0) == 3.4
    assert coerce_ratio(97.0, field="position", low=0.0) == 97.0
    assert coerce_ratio(0, field="position", low=0.0) == 0.0


@pytest.mark.parametrize("value", [-0.1, -1, math.nan, -math.inf, True, "3.4", None])
def test_coerce_ratio_position_rejects_bad(value: object) -> None:
    with pytest.raises(ExternalProviderDataError):
        coerce_ratio(value, field="position", low=0.0)
