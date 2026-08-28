"""CompetitionEaseNormalizer V1 の unit テスト (DB / FastAPI 非依存、外部通信なし)。"""

import math

import pytest

from app.keyword.normalizers.competition_ease import (
    DIFFICULTY_SCALE,
    NORMALIZER_NAME,
    NORMALIZER_VERSION,
    calculate_competition_ease,
)


@pytest.mark.parametrize(
    ("difficulty", "expected_ease"),
    [
        (0, 100.0),
        (1, 99.0),
        (10, 90.0),
        (30, 70.0),
        (50, 50.0),
        (80, 20.0),
        (99, 1.0),
        (100, 0.0),
    ],
)
def test_formula_known_values(difficulty: float, expected_ease: float) -> None:
    result = calculate_competition_ease(difficulty)
    assert result.normalized_value == expected_ease
    assert result.keyword_difficulty == float(difficulty)


def test_formula_is_100_minus_difficulty() -> None:
    for d in (0, 12.5, 37, 63.2, 100):
        assert calculate_competition_ease(d).normalized_value == round(100 - d, 2)


def test_decimal_difficulty() -> None:
    assert calculate_competition_ease(32.45).normalized_value == 67.55


def test_evidence_and_metadata() -> None:
    result = calculate_competition_ease(40)
    assert result.evidence_available is True
    assert result.evidence_coverage == 1.0
    assert result.difficulty_scale == DIFFICULTY_SCALE == "0_easy_100_hard"
    assert result.normalizer_name == NORMALIZER_NAME == "competition_ease"
    assert result.normalizer_version == NORMALIZER_VERSION == "v1"


def test_deterministic() -> None:
    assert calculate_competition_ease(55) == calculate_competition_ease(55)


# -- validation ------------------------------------------------------
@pytest.mark.parametrize("bad", [-0.01, -1, -100])
def test_rejects_negative(bad: float) -> None:
    with pytest.raises(ValueError, match=r"\[0, 100\]"):
        calculate_competition_ease(bad)


@pytest.mark.parametrize("bad", [100.01, 101, 1000])
def test_rejects_over_100(bad: float) -> None:
    with pytest.raises(ValueError, match=r"\[0, 100\]"):
        calculate_competition_ease(bad)


def test_rejects_nan() -> None:
    with pytest.raises(ValueError, match="finite"):
        calculate_competition_ease(math.nan)


def test_rejects_infinity() -> None:
    with pytest.raises(ValueError, match="finite"):
        calculate_competition_ease(math.inf)
    with pytest.raises(ValueError, match="finite"):
        calculate_competition_ease(-math.inf)


def test_rejects_bool() -> None:
    # bool は int のサブクラスだが numeric として受け付けない
    with pytest.raises(ValueError, match="boolean"):
        calculate_competition_ease(True)
    with pytest.raises(ValueError, match="boolean"):
        calculate_competition_ease(False)


def test_rejects_non_numeric() -> None:
    with pytest.raises(ValueError, match="number"):
        calculate_competition_ease("50")
    with pytest.raises(ValueError, match="number"):
        calculate_competition_ease(None)
