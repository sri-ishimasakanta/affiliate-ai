"""Opportunity Score V1 の純粋計算ロジックの unit テスト (DB 非依存)。"""

import pytest

from app.keyword.scoring import (
    COMPONENT_NAMES,
    OPPORTUNITY_SCORE_WEIGHTS,
    SCORE_VERSION,
    OpportunityScoreInput,
    calculate_opportunity_score,
)

_ALL_COMPONENTS = (
    "search_demand",
    "commercial_intent",
    "affiliate_opportunity",
    "competition_ease",
    "trend",
    "originality",
    "site_relevance",
)


def _input(**overrides: float) -> OpportunityScoreInput:
    values = dict.fromkeys(_ALL_COMPONENTS, 0.0)
    values.update(overrides)
    return OpportunityScoreInput(**values)


def test_weight_set_matches_component_names() -> None:
    assert set(OPPORTUNITY_SCORE_WEIGHTS) == set(_ALL_COMPONENTS)
    assert COMPONENT_NAMES == _ALL_COMPONENTS


def test_weights_sum_to_one() -> None:
    assert sum(OPPORTUNITY_SCORE_WEIGHTS.values()) == 1.0


def test_all_zero_gives_total_zero() -> None:
    result = calculate_opportunity_score(_input())
    assert result.total == 0.0
    assert result.version == "v1" == SCORE_VERSION


def test_all_hundred_gives_total_hundred() -> None:
    result = calculate_opportunity_score(_input(**dict.fromkeys(_ALL_COMPONENTS, 100.0)))
    assert result.total == 100.0


def test_known_input_matches_expected_total() -> None:
    result = calculate_opportunity_score(
        OpportunityScoreInput(
            search_demand=75,
            commercial_intent=95,
            affiliate_opportunity=90,
            competition_ease=55,
            trend=90,
            originality=80,
            site_relevance=100,
        )
    )
    assert result.total == 82.25


@pytest.mark.parametrize(("component", "weight"), list(OPPORTUNITY_SCORE_WEIGHTS.items()))
def test_each_weight_is_reflected(component: str, weight: float) -> None:
    # 対象コンポーネントだけ 100、他は 0 -> total は weight * 100
    result = calculate_opportunity_score(_input(**{component: 100.0}))
    assert result.total == round(weight * 100.0, 2)
    assert result.contributions[component] == round(weight * 100.0, 2)


def test_boundary_values_are_accepted() -> None:
    assert calculate_opportunity_score(_input(search_demand=0.0)).total == 0.0
    assert calculate_opportunity_score(_input(search_demand=100.0)).total == 20.0


@pytest.mark.parametrize("bad_value", [-0.01, -1, 100.01, 101, 250])
def test_out_of_range_component_raises_value_error(bad_value: float) -> None:
    with pytest.raises(ValueError, match="within"):
        calculate_opportunity_score(_input(trend=bad_value))


def test_higher_competition_ease_gives_higher_total() -> None:
    low = calculate_opportunity_score(_input(competition_ease=10.0)).total
    high = calculate_opportunity_score(_input(competition_ease=90.0)).total
    assert high > low


def test_same_input_is_deterministic() -> None:
    data = OpportunityScoreInput(
        search_demand=12.34,
        commercial_intent=56.78,
        affiliate_opportunity=90.0,
        competition_ease=33.33,
        trend=1.0,
        originality=99.99,
        site_relevance=50.0,
    )
    first = calculate_opportunity_score(data)
    second = calculate_opportunity_score(data)
    assert first == second
    assert first.total == second.total


def test_weights_mapping_is_read_only() -> None:
    with pytest.raises(TypeError):
        OPPORTUNITY_SCORE_WEIGHTS["search_demand"] = 0.99  # type: ignore[index]
