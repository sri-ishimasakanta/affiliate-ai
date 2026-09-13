"""ArticleLinkSubstitutionMapping — status 定数 / transition table (pure)。"""

from __future__ import annotations

import pytest

from app.models.article_link_substitution_mapping import (
    ALSM_ACTIVE,
    ALSM_REVOKED,
    ALSM_STATUSES,
    ALSM_SUPERSEDED,
    ALSM_TERMINAL_STATUSES,
    ALSM_TRANSITIONS,
    FROZEN_FIELDS,
    ArticleLinkSubstitutionMapping,
    alsm_transition_allowed,
)


def test_status_constants() -> None:
    assert ALSM_STATUSES == {ALSM_ACTIVE, ALSM_SUPERSEDED, ALSM_REVOKED}
    assert ALSM_TERMINAL_STATUSES == {ALSM_SUPERSEDED, ALSM_REVOKED}


@pytest.mark.parametrize("target", [ALSM_SUPERSEDED, ALSM_REVOKED])
def test_active_may_transition_to_either_terminal(target) -> None:
    assert alsm_transition_allowed(ALSM_ACTIVE, target)


@pytest.mark.parametrize("source", [ALSM_SUPERSEDED, ALSM_REVOKED])
@pytest.mark.parametrize("target", [ALSM_ACTIVE, ALSM_SUPERSEDED, ALSM_REVOKED])
def test_terminal_states_never_transition_further(source, target) -> None:
    assert not alsm_transition_allowed(source, target)


def test_same_status_transition_not_allowed() -> None:
    assert not alsm_transition_allowed(ALSM_ACTIVE, ALSM_ACTIVE)


def test_no_reverse_or_lateral_terminal_transitions() -> None:
    assert not alsm_transition_allowed(ALSM_SUPERSEDED, ALSM_ACTIVE)
    assert not alsm_transition_allowed(ALSM_REVOKED, ALSM_ACTIVE)
    assert not alsm_transition_allowed(ALSM_SUPERSEDED, ALSM_REVOKED)
    assert not alsm_transition_allowed(ALSM_REVOKED, ALSM_SUPERSEDED)


def test_frozen_fields_shape() -> None:
    assert FROZEN_FIELDS == (
        "article_id",
        "occurrence_identity_hash",
        "original_href",
        "affiliate_link_target_id",
        "approved_at",
        "idempotency_key",
    )


def test_transitions_table_has_no_unexpected_edges() -> None:
    for status in ALSM_STATUSES:
        assert ALSM_TRANSITIONS.get(status, frozenset()) <= ALSM_TERMINAL_STATUSES | {
            ALSM_ACTIVE
        }


def test_model_tablename_and_no_updated_at() -> None:
    assert (
        ArticleLinkSubstitutionMapping.__tablename__
        == "article_link_substitution_mappings"
    )
    cols = set(ArticleLinkSubstitutionMapping.__table__.columns.keys())
    assert "updated_at" not in cols
    assert "created_at" in cols
