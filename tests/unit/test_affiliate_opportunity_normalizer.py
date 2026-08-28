"""AffiliateOpportunityNormalizer V1 の unit テスト (DB / SDK / FastAPI 非依存)。"""

from itertools import pairwise

import pytest

from app.keyword.affiliate_matching import MatchedProgram
from app.keyword.normalizers.affiliate_opportunity import (
    COMMISSION_WEIGHT,
    NORMALIZER_NAME,
    NORMALIZER_VERSION,
    PROGRAM_MATCH_CALIBRATION,
    PROGRAM_MATCH_WEIGHT,
    PROVIDER_SPREAD_WEIGHT,
    calculate_affiliate_opportunity,
    commission_score,
    program_match_score,
    provider_spread_score,
)


def _mp(
    program_id: int,
    *,
    provider: str | None = "direct",
    commission_type: str | None = None,
    commission_value: float | None = None,
    currency: str | None = None,
) -> MatchedProgram:
    return MatchedProgram(
        program_id=program_id,
        name=f"P{program_id}",
        provider=provider,
        category="ai",
        matched_terms=("議事録",),
        commission_type=commission_type,
        commission_value=commission_value,
        currency=currency,
    )


# -- program_match_score -------------------------------------------
@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, 0.0),
        (1, 22.12),
        (2, 39.35),
        (3, 52.76),
        (4, 63.21),
        (7, 82.62),
        (10, 91.79),
    ],
)
def test_program_match_score_known(n: int, expected: float) -> None:
    assert program_match_score(n) == expected


def test_program_match_score_saturation_and_bounds() -> None:
    values = [program_match_score(n) for n in (0, 1, 2, 3, 5, 10, 30, 100)]
    assert values == sorted(values)
    assert all(a <= b for a, b in pairwise(values))
    assert all(0.0 <= v <= 100.0 for v in values)
    assert program_match_score(100_000) == 100.0
    # 限界効用逓減: 1->2 の増分 > 9->10 の増分
    assert (program_match_score(2) - program_match_score(1)) > (
        program_match_score(10) - program_match_score(9)
    )


def test_program_match_calibration_constant() -> None:
    assert PROGRAM_MATCH_CALIBRATION == 4.0


# -- commission_score --------------------------------------------
@pytest.mark.parametrize(
    ("pct", "expected"),
    [
        (0, 0.0),
        (10, 25.0),
        (20, 50.0),
        (25, 62.5),
        (30, 75.0),
        (35, 87.5),
        (40, 100.0),
        (50, 100.0),
    ],
)
def test_commission_score_percentage_known(pct: float, expected: float) -> None:
    matched = [_mp(1, commission_type="percentage", commission_value=pct)]
    assert commission_score(matched) == expected


def test_commission_score_takes_max_percentage() -> None:
    matched = [
        _mp(1, commission_type="percentage", commission_value=10),
        _mp(2, commission_type="percentage", commission_value=30),
        _mp(3, commission_type="percentage", commission_value=20),
    ]
    assert commission_score(matched) == 75.0  # 30% * 2.5


def test_commission_score_fixed_only_is_none() -> None:
    matched = [_mp(1, commission_type="fixed", commission_value=200, currency="USD")]
    assert commission_score(matched) is None


def test_commission_score_mixed_uses_percentage_only() -> None:
    matched = [
        _mp(1, commission_type="fixed", commission_value=999, currency="USD"),
        _mp(2, commission_type="percentage", commission_value=20),
    ]
    assert commission_score(matched) == 50.0


def test_commission_score_ignores_negative_and_none_values() -> None:
    matched = [
        _mp(1, commission_type="percentage", commission_value=-5),
        _mp(2, commission_type="percentage", commission_value=None),
    ]
    assert commission_score(matched) is None


def test_commission_type_casefold_and_strip() -> None:
    matched = [_mp(1, commission_type=" Percentage ", commission_value=20)]
    assert commission_score(matched) == 50.0


# -- provider_spread_score --------------------------------------
def test_provider_spread_score_known() -> None:
    def _n(k: int) -> list[MatchedProgram]:
        return [_mp(i, provider=f"p{i}") for i in range(k)]

    assert provider_spread_score(_n(1)) == 40.0
    assert provider_spread_score(_n(2)) == 80.0
    assert provider_spread_score(_n(3)) == 100.0
    assert provider_spread_score(_n(5)) == 100.0
    assert provider_spread_score([]) == 0.0


def test_provider_spread_direct_shared_counts_one() -> None:
    matched = [_mp(i, provider="direct") for i in range(4)]
    assert provider_spread_score(matched) == 40.0


def test_provider_spread_ignores_empty_provider() -> None:
    matched = [_mp(1, provider=""), _mp(2, provider="  "), _mp(3, provider=None)]
    assert provider_spread_score(matched) == 0.0


# -- calculate_affiliate_opportunity ---------------------------
def test_zero_match_final_is_zero() -> None:
    result = calculate_affiliate_opportunity([])
    assert result.normalized_value == 0.0
    assert result.market_evidence_available is False
    assert result.matched_program_count == 0
    assert result.commission_score is None
    assert result.available_weight == 0.55
    assert result.evidence_coverage == 0.55


def test_matched_sets_market_evidence_true() -> None:
    result = calculate_affiliate_opportunity([_mp(1)])
    assert result.market_evidence_available is True


def test_all_components_available_coverage_1() -> None:
    matched = [
        _mp(1, provider="a", commission_type="percentage", commission_value=30),
        _mp(2, provider="b", commission_type="percentage", commission_value=10),
        _mp(3, provider="c"),
    ]
    result = calculate_affiliate_opportunity(matched)
    assert result.available_weight == 1.0
    assert result.evidence_coverage == 1.0
    assert result.commission_score == 75.0
    assert result.provider_spread_score == 100.0
    assert result.program_match_score == 52.76
    # (52.76*0.55 + 75.0*0.35 + 100.0*0.10) / 1.0
    assert result.normalized_value == pytest.approx(65.27, abs=0.01)


def test_missing_commission_redistributes_weight() -> None:
    # 7 program / 3 provider / commission なし
    matched = [_mp(i, provider=["a", "b", "c"][i % 3]) for i in range(7)]
    result = calculate_affiliate_opportunity(matched)
    assert result.commission_score is None
    assert result.available_weight == 0.65  # 0.55 + 0.10
    assert result.evidence_coverage == 0.65
    # (82.62*0.55 + 100.0*0.10) / 0.65
    assert result.normalized_value == pytest.approx(85.29, abs=0.01)


def test_missing_commission_not_treated_as_zero() -> None:
    with_missing = calculate_affiliate_opportunity(
        [_mp(1, provider="a"), _mp(2, provider="b")]
    ).normalized_value
    with_zero = calculate_affiliate_opportunity(
        [
            _mp(1, provider="a", commission_type="percentage", commission_value=0),
            _mp(2, provider="b", commission_type="percentage", commission_value=0),
        ]
    ).normalized_value
    assert with_missing > with_zero  # 欠測は 0 点扱いしない


def test_metadata_and_weights() -> None:
    result = calculate_affiliate_opportunity([_mp(1)])
    assert result.normalizer_name == NORMALIZER_NAME == "affiliate_opportunity"
    assert result.normalizer_version == NORMALIZER_VERSION == "v1"
    assert (PROGRAM_MATCH_WEIGHT, COMMISSION_WEIGHT, PROVIDER_SPREAD_WEIGHT) == (
        0.55,
        0.35,
        0.10,
    )


def test_deterministic() -> None:
    matched = [_mp(1, provider="a", commission_type="percentage", commission_value=25)]
    assert calculate_affiliate_opportunity(matched) == calculate_affiliate_opportunity(matched)


def test_result_rounded_to_two_decimals() -> None:
    matched = [
        _mp(i, provider=f"p{i}", commission_type="percentage", commission_value=17 + i)
        for i in range(5)
    ]
    value = calculate_affiliate_opportunity(matched).normalized_value
    assert round(value, 2) == value
