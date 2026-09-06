"""Search Console import identity / dimensions_json / lifecycle helper の pure テスト。"""

from __future__ import annotations

import hashlib
import json
from datetime import date

import pytest

from app.article.draft_input_canonical import canonical_json
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
    AGGREGATION_TYPE,
    DATA_STATE,
    DIMENSION_SETS,
    IMPORT_VERSION,
    PAGE_DIMENSIONS,
    QUERY_DIMENSIONS,
    SEARCH_TYPE,
    compute_import_identity_hash,
    dimensions_json,
)

_PROP = "sc-domain:example.test"


def test_v2_contract_constants() -> None:
    assert SEARCH_TYPE == "web"
    assert AGGREGATION_TYPE == "auto"
    assert DATA_STATE == "final"
    assert IMPORT_VERSION == 2
    assert PAGE_DIMENSIONS == ("date", "page")
    assert QUERY_DIMENSIONS == ("date", "page", "query")
    assert DIMENSION_SETS == (PAGE_DIMENSIONS, QUERY_DIMENSIONS)


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


def test_v2_identity_differs_from_old_bypage_v1_semantics() -> None:
    """byPage / import_version=1 / dataState 無し の旧 identity とはハッシュが変わる。"""

    current = compute_import_identity_hash(
        property_uri=_PROP, start_date=date(2026, 9, 1), end_date=date(2026, 9, 6)
    )
    old_identity = {
        "property_uri": _PROP,
        "start_date": "2026-09-01",
        "end_date": "2026-09-06",
        "dimension_sets": [["date", "page"], ["date", "page", "query"]],
        "search_type": "web",
        "aggregation_type": "byPage",
        "import_version": 1,
    }
    old_hash = hashlib.sha256(
        canonical_json(old_identity).encode("utf-8")
    ).hexdigest()
    assert current != old_hash


def test_v2_identity_binds_aggregation_and_datastate() -> None:
    """identity 本体に aggregationType=auto / dataState=final / version=2 が入る。"""

    current = compute_import_identity_hash(
        property_uri=_PROP, start_date=date(2026, 9, 1), end_date=date(2026, 9, 6)
    )
    expected = hashlib.sha256(
        canonical_json(
            {
                "property_uri": _PROP,
                "start_date": "2026-09-01",
                "end_date": "2026-09-06",
                "page_dimensions": ["date", "page"],
                "query_dimensions": ["date", "page", "query"],
                "search_type": "web",
                "aggregation_type": "auto",
                "data_state": "final",
                "import_version": 2,
            }
        ).encode("utf-8")
    ).hexdigest()
    assert current == expected


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
