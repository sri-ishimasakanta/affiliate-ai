"""app/affiliate/click_import_rows.py — export レスポンス 1 ページの厳格検証 (pure)。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.affiliate.click_import_rows import ClickRow, validate_click_export_page
from app.exceptions import AffiliateClickImportError

_TOK_A = "tokAAAAAAAAAAAAAAAAA"  # 20 chars, [A-Za-z0-9_-]{16,64}
_TOK_B = "tok-B_0000000000000000"
_LIMIT = 1000


def _row(rid: int, token: str = _TOK_A, clicked_at: str = "2026-09-01 12:30:00") -> dict:
    return {"id": rid, "token": token, "clicked_at": clicked_at}


def _page(rows, *, since_id=0, limit=_LIMIT, count=None, next_since_id=None) -> dict:
    if count is None:
        count = len(rows) if isinstance(rows, list) else 0
    if next_since_id is None:
        next_since_id = rows[-1]["id"] if isinstance(rows, list) and rows else since_id
    return {
        "schema_version": 1,
        "count": count,
        "limit": limit,
        "next_since_id": next_since_id,
        "rows": rows,
    }


def _validate(payload, *, since_id=0, limit=_LIMIT):
    return validate_click_export_page(payload, since_id=since_id, limit=limit)


# ==================== valid pages =====================================
def test_valid_empty_page() -> None:
    rows, nxt = _validate(_page([], since_id=42), since_id=42)
    assert rows == []
    assert nxt == 42


def test_valid_single_row() -> None:
    rows, nxt = _validate(_page([_row(7)], since_id=0), since_id=0)
    assert rows == [
        ClickRow(
            source_click_id=7,
            token=_TOK_A,
            clicked_at_utc=datetime(2026, 9, 1, 12, 30, 0, tzinfo=UTC),
        )
    ]
    assert nxt == 7


def test_valid_multi_row_ascending() -> None:
    rows, nxt = _validate(
        _page([_row(3), _row(4, _TOK_B), _row(9)], since_id=2), since_id=2
    )
    assert [r.source_click_id for r in rows] == [3, 4, 9]
    assert nxt == 9


def test_clicked_at_is_parsed_as_utc_aware() -> None:
    rows, _ = _validate(
        _page([_row(1, clicked_at="2026-01-02 03:04:05")], since_id=0), since_id=0
    )
    assert rows[0].clicked_at_utc.tzinfo is UTC
    assert rows[0].clicked_at_utc == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


# ==================== structural violations ===========================
@pytest.mark.parametrize(
    ("payload", "needle"),
    [
        ([], "not a JSON object"),
        (
            {"schema_version": 2, "count": 0, "limit": _LIMIT, "next_since_id": 0, "rows": []},
            "schema_version",
        ),
        ({"count": 0, "limit": _LIMIT, "next_since_id": 0, "rows": []}, "schema_version"),
        (
            {"schema_version": 1, "count": 0, "limit": _LIMIT, "next_since_id": 0, "rows": "x"},
            "rows is not a list",
        ),
        (
            {"schema_version": 1, "count": 1, "limit": _LIMIT, "next_since_id": 1, "rows": [1]},
            "row is not an object",
        ),
    ],
)
def test_rejects_structural_violations(payload, needle) -> None:
    with pytest.raises(AffiliateClickImportError, match=needle) as exc:
        _validate(payload)
    assert exc.value.http_status == 200


def test_rejects_limit_echo_mismatch() -> None:
    with pytest.raises(AffiliateClickImportError, match="different limit"):
        _validate(_page([], limit=999), since_id=0, limit=_LIMIT)


def test_rejects_count_not_matching_rows() -> None:
    with pytest.raises(AffiliateClickImportError, match="count does not match"):
        _validate(_page([_row(1)], since_id=0, count=5))


def test_rejects_more_rows_than_limit() -> None:
    with pytest.raises(
        AffiliateClickImportError, match="more rows than the requested limit"
    ):
        _validate(_page([_row(1), _row(2)], since_id=0, limit=1), since_id=0, limit=1)


# ==================== id ordering / cursor ============================
def test_rejects_descending_ids() -> None:
    with pytest.raises(AffiliateClickImportError, match="ascending id order"):
        _validate(_page([_row(6), _row(5)], since_id=0, next_since_id=5))


def test_rejects_duplicate_ids() -> None:
    with pytest.raises(AffiliateClickImportError, match="ascending id order"):
        _validate(_page([_row(5), _row(5)], since_id=0, next_since_id=5))


def test_rejects_id_equal_to_since_id() -> None:
    with pytest.raises(
        AffiliateClickImportError, match="greater than the requested since_id"
    ):
        _validate(_page([_row(10)], since_id=10, next_since_id=10), since_id=10)


def test_rejects_id_below_since_id() -> None:
    with pytest.raises(
        AffiliateClickImportError, match="greater than the requested since_id"
    ):
        _validate(_page([_row(3)], since_id=5, next_since_id=3), since_id=5)


def test_rejects_next_since_id_mismatch_non_empty() -> None:
    with pytest.raises(
        AffiliateClickImportError, match="does not match the last row id"
    ):
        _validate(_page([_row(7)], since_id=0, next_since_id=99))


def test_rejects_next_since_id_mismatch_empty() -> None:
    with pytest.raises(
        AffiliateClickImportError, match="must equal since_id for an empty page"
    ):
        _validate(_page([], since_id=5, next_since_id=99), since_id=5)


# ==================== token / timestamp ==============================
@pytest.mark.parametrize(
    "token",
    ["short", "has space 0000000000", "bad!token@000000000", "", "x" * 65],
)
def test_rejects_bad_token_shape(token) -> None:
    with pytest.raises(AffiliateClickImportError, match="token has an invalid shape"):
        _validate(_page([_row(1, token=token)], since_id=0))


@pytest.mark.parametrize(
    "clicked_at",
    [
        "2026-09-01T12:00:00",
        "2026-09-01 12:00",
        "not-a-date",
        "2026/09/01 12:00:00",
        1725192600,
    ],
)
def test_rejects_bad_timestamp_shape(clicked_at) -> None:
    with pytest.raises(AffiliateClickImportError, match="clicked_at is not a"):
        _validate(_page([_row(1, clicked_at=clicked_at)], since_id=0))


def test_rejects_impossible_calendar_date() -> None:
    with pytest.raises(AffiliateClickImportError, match="valid calendar timestamp"):
        _validate(_page([_row(1, clicked_at="2026-02-30 00:00:00")], since_id=0))


# ==================== bool smuggling =================================
@pytest.mark.parametrize(
    ("field", "value"),
    [("count", True), ("limit", True), ("next_since_id", True)],
)
def test_rejects_bool_top_level_ints(field, value) -> None:
    payload = _page([], since_id=0)
    payload[field] = value
    with pytest.raises(AffiliateClickImportError, match="is not an integer"):
        _validate(payload)


def test_rejects_bool_row_id() -> None:
    payload = _page([], since_id=0)
    payload["rows"] = [
        {"id": True, "token": _TOK_A, "clicked_at": "2026-09-01 12:00:00"}
    ]
    payload["count"] = 1
    payload["next_since_id"] = 1
    with pytest.raises(AffiliateClickImportError, match="row id is not an integer"):
        _validate(payload)
