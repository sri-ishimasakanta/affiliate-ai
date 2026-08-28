"""CompetitionEaseManualCreate schema の validation テスト。"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.keyword.schemas import CompetitionEaseManualCreate


def test_valid_minimal() -> None:
    payload = CompetitionEaseManualCreate(
        keyword_difficulty=32, source_name="example_free_seo_tool"
    )
    assert payload.keyword_difficulty == 32.0
    assert payload.source_name == "example_free_seo_tool"
    assert payload.source_reference is None
    assert payload.observed_at is None


def test_source_name_stripped() -> None:
    payload = CompetitionEaseManualCreate(
        keyword_difficulty=10, source_name="  manual_research  "
    )
    assert payload.source_name == "manual_research"


def test_source_reference_blank_becomes_none() -> None:
    payload = CompetitionEaseManualCreate(
        keyword_difficulty=10, source_name="x", source_reference="   "
    )
    assert payload.source_reference is None


def test_observed_at_accepted() -> None:
    ts = datetime(2026, 8, 28, tzinfo=UTC)
    payload = CompetitionEaseManualCreate(
        keyword_difficulty=10, source_name="x", observed_at=ts
    )
    assert payload.observed_at == ts


@pytest.mark.parametrize("bad", [-1, 100.5, 101, 200])
def test_difficulty_out_of_range_rejected(bad: float) -> None:
    with pytest.raises(ValidationError):
        CompetitionEaseManualCreate(keyword_difficulty=bad, source_name="x")


def test_difficulty_bool_rejected() -> None:
    with pytest.raises(ValidationError):
        CompetitionEaseManualCreate(keyword_difficulty=True, source_name="x")
    with pytest.raises(ValidationError):
        CompetitionEaseManualCreate(keyword_difficulty=False, source_name="x")


def test_source_name_required_and_non_blank() -> None:
    with pytest.raises(ValidationError):
        CompetitionEaseManualCreate(keyword_difficulty=10)
    with pytest.raises(ValidationError):
        CompetitionEaseManualCreate(keyword_difficulty=10, source_name="")
    with pytest.raises(ValidationError):
        CompetitionEaseManualCreate(keyword_difficulty=10, source_name="   ")


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError):
        CompetitionEaseManualCreate(
            keyword_difficulty=10, source_name="x", competition_index=5
        )
