"""KeywordSignalComponent が Opportunity Score V1 の component 名と一致することを検証する。"""

from app.keyword.scoring import COMPONENT_NAMES, OPPORTUNITY_SCORE_WEIGHTS
from app.models.enums import KeywordSignalComponent


def test_enum_values_exactly_match_component_names() -> None:
    assert tuple(member.value for member in KeywordSignalComponent) == COMPONENT_NAMES


def test_every_weight_key_has_an_enum_member() -> None:
    assert {member.value for member in KeywordSignalComponent} == set(
        OPPORTUNITY_SCORE_WEIGHTS
    )


def test_enum_members_are_plain_strings() -> None:
    assert KeywordSignalComponent.TREND == "trend"
    assert str(KeywordSignalComponent.SEARCH_DEMAND) == "search_demand"
