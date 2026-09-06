"""Search Console import identity / dimensions_json / lifecycle helper の pure テスト。"""

from __future__ import annotations

import json
from datetime import date

import pytest

from app.models.search_console_import_run import (
    SC_IMPORT_CANCELLED,
    SC_IMPORT_FAILED,
    SC_IMPORT_PREPARED,
    SC_IMPORT_RUNNING,
    SC_IMPORT_SUCCEEDED,
    SC_IMPORT_TERMINAL_STATUSES,
    sc_import_transition_allowed,
)
from app.search_console.import_identity import (
    V1_DIMENSION_SETS,
    compute_import_identity_hash,
    dimensions_json,
)

_PROP = "sc-domain:example.test"


def test_dimensions_json_is_canonical_and_stable() -> None:
    js = dimensions_json()
    assert json.loads(js) == [["date", "page"], ["date", "page", "query"]]
    assert js == dimensions_json()
    assert " " not in js and "\n" not in js


def test_import_identity_hash_is_deterministic() -> None:
    a = compute_import_identity_hash(
        property_uri=_PROP, start_date=date(2026, 9, 1), end_date=date(2026, 9, 6)
    )
    b = compute_import_identity_hash(
        property_uri=_PROP, start_date=date(2026, 9, 1), end_date=date(2026, 9, 6)
    )
    assert a == b and len(a) == 64
    assert all(c in "0123456789abcdef" for c in a)


def test_import_identity_hash_changes_per_component() -> None:
    base = compute_import_identity_hash(
        property_uri=_PROP, start_date=date(2026, 9, 1), end_date=date(2026, 9, 6)
    )
    assert (
        compute_import_identity_hash(
            property_uri="sc-domain:other.test",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 6),
        )
        != base
    )
    assert (
        compute_import_identity_hash(
            property_uri=_PROP, start_date=date(2026, 8, 31), end_date=date(2026, 9, 6)
        )
        != base
    )
    assert (
        compute_import_identity_hash(
            property_uri=_PROP, start_date=date(2026, 9, 1), end_date=date(2026, 9, 7)
        )
        != base
    )


def test_v1_dimension_sets_shape() -> None:
    assert V1_DIMENSION_SETS == (("date", "page"), ("date", "page", "query"))


@pytest.mark.parametrize(
    ("cur", "tgt"),
    [
        (SC_IMPORT_PREPARED, SC_IMPORT_RUNNING),
        (SC_IMPORT_RUNNING, SC_IMPORT_SUCCEEDED),
        (SC_IMPORT_RUNNING, SC_IMPORT_FAILED),
        (SC_IMPORT_PREPARED, SC_IMPORT_CANCELLED),
    ],
)
def test_allowed_transitions(cur: str, tgt: str) -> None:
    assert sc_import_transition_allowed(cur, tgt) is True


@pytest.mark.parametrize(
    ("cur", "tgt"),
    [
        (SC_IMPORT_FAILED, SC_IMPORT_RUNNING),
        (SC_IMPORT_SUCCEEDED, SC_IMPORT_RUNNING),
        (SC_IMPORT_CANCELLED, SC_IMPORT_RUNNING),
        (SC_IMPORT_PREPARED, SC_IMPORT_SUCCEEDED),
        (SC_IMPORT_RUNNING, SC_IMPORT_PREPARED),
        (SC_IMPORT_PREPARED, SC_IMPORT_PREPARED),
    ],
)
def test_rejected_transitions(cur: str, tgt: str) -> None:
    assert sc_import_transition_allowed(cur, tgt) is False


def test_terminal_states_have_no_outgoing() -> None:
    for st in SC_IMPORT_TERMINAL_STATUSES:
        for tgt in (
            SC_IMPORT_PREPARED,
            SC_IMPORT_RUNNING,
            SC_IMPORT_SUCCEEDED,
            SC_IMPORT_FAILED,
            SC_IMPORT_CANCELLED,
        ):
            assert sc_import_transition_allowed(st, tgt) is False
