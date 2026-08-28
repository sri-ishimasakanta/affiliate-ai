"""AffiliateProgram schema の validation / normalization テスト。"""

import pytest
from pydantic import ValidationError

from app.affiliate.schemas import (
    AffiliateProgramCreate,
    AffiliateProgramRead,
    AffiliateProgramUpdate,
)
from app.models.enums import AffiliateProgramStatus


def test_valid_create_minimal() -> None:
    payload = AffiliateProgramCreate(name="Example AI Tool")
    assert payload.name == "Example AI Tool"
    assert payload.status is AffiliateProgramStatus.ACTIVE
    assert payload.currency is None
    assert payload.match_terms is None


def test_valid_create_full() -> None:
    payload = AffiliateProgramCreate(
        name="  Example AI Tool ",
        provider="example",
        category="ai",
        commission_type="fixed",
        commission_value=3000,
        currency="jpy",
        landing_page_url="https://example.com",
        tracking_url="https://example.com/track",
        notes="Example only",
        match_terms=["AI", "生成AI", "業務効率化"],
        status="paused",
    )
    assert payload.name == "Example AI Tool"
    assert payload.currency == "JPY"
    assert payload.commission_value == 3000.0
    assert payload.status is AffiliateProgramStatus.PAUSED


def test_name_blank_rejected() -> None:
    for bad in ("", "   ", "\t\n"):
        with pytest.raises(ValidationError):
            AffiliateProgramCreate(name=bad)


def test_currency_uppercased() -> None:
    assert AffiliateProgramCreate(name="x", currency="jpy").currency == "JPY"
    assert AffiliateProgramCreate(name="x", currency=" usd ").currency == "USD"


@pytest.mark.parametrize("bad", ["JP", "JPYX", "12A", "日本円", "J P"])
def test_currency_invalid_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        AffiliateProgramCreate(name="x", currency=bad)


def test_currency_none_allowed() -> None:
    assert AffiliateProgramCreate(name="x", currency=None).currency is None


def test_commission_value_non_negative() -> None:
    assert AffiliateProgramCreate(name="x", commission_value=0).commission_value == 0.0
    with pytest.raises(ValidationError):
        AffiliateProgramCreate(name="x", commission_value=-1)


def test_commission_value_none_allowed() -> None:
    assert AffiliateProgramCreate(name="x", commission_value=None).commission_value is None


def test_match_terms_trim() -> None:
    payload = AffiliateProgramCreate(name="x", match_terms=["  議事録 ", " AI 議事録"])
    assert payload.match_terms == ["議事録", "AI 議事録"]


def test_match_terms_duplicate_removal_preserves_order() -> None:
    payload = AffiliateProgramCreate(
        name="x", match_terms=["議事録", "文字起こし", "議事録", " 文字起こし "]
    )
    assert payload.match_terms == ["議事録", "文字起こし"]


def test_match_terms_empty_entries_removed() -> None:
    payload = AffiliateProgramCreate(name="x", match_terms=["", "  ", "議事録", "\t"])
    assert payload.match_terms == ["議事録"]


def test_match_terms_all_empty_becomes_empty_list() -> None:
    assert AffiliateProgramCreate(name="x", match_terms=["", "  "]).match_terms == []


def test_match_terms_none_stays_none() -> None:
    assert AffiliateProgramCreate(name="x", match_terms=None).match_terms is None


def test_match_terms_overlong_entry_rejected() -> None:
    with pytest.raises(ValidationError):
        AffiliateProgramCreate(name="x", match_terms=["a" * 256])


def test_status_validation() -> None:
    assert AffiliateProgramCreate(name="x", status="ended").status is (
        AffiliateProgramStatus.ENDED
    )
    with pytest.raises(ValidationError):
        AffiliateProgramCreate(name="x", status="bogus")


def test_update_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        AffiliateProgramUpdate(unknown="x")


def test_update_partial_normalizes() -> None:
    payload = AffiliateProgramUpdate(currency="usd", match_terms=[" a ", "a", "b"])
    dumped = payload.model_dump(exclude_unset=True)
    assert dumped == {"currency": "USD", "match_terms": ["a", "b"]}


def test_update_name_blank_rejected() -> None:
    with pytest.raises(ValidationError):
        AffiliateProgramUpdate(name="   ")


def test_update_explicit_null_clears_match_terms() -> None:
    payload = AffiliateProgramUpdate(match_terms=None)
    assert payload.model_dump(exclude_unset=True) == {"match_terms": None}


def test_read_match_terms_is_always_list() -> None:
    read = AffiliateProgramRead(
        id=1,
        name="x",
        provider=None,
        category=None,
        commission_type=None,
        commission_value=None,
        currency=None,
        landing_page_url=None,
        tracking_url=None,
        notes=None,
        match_terms=[],
        status=AffiliateProgramStatus.ACTIVE,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    assert read.match_terms == []
