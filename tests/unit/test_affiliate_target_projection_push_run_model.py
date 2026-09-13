"""AffiliateTargetProjectionPushRun / repository — status lifecycle + narrow mutation。"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.exceptions import AffiliateProjectionPushError
from app.models.affiliate_target_projection_push_run import (
    ATPP_FAILED,
    ATPP_OUTCOME_UNKNOWN,
    ATPP_RUNNING,
    ATPP_STATUSES,
    ATPP_SUCCEEDED,
    ATPP_TERMINAL_STATUSES,
    AffiliateTargetProjectionPushRun,
    atpp_transition_allowed,
)
from app.repositories.affiliate_target_projection_push_run_repository import (
    AffiliateTargetProjectionPushRunRepository,
)

_ORIGIN = "https://runtime.example.test"


def _repo(session: Session) -> AffiliateTargetProjectionPushRunRepository:
    return AffiliateTargetProjectionPushRunRepository(session)


def _add_running(session: Session, *, manifest="[]", origin=_ORIGIN, snapshot_hash="h" * 64):
    return _repo(session).add_running(
        snapshot_scope="full",
        runtime_origin=origin,
        requested_snapshot_hash=snapshot_hash,
        requested_target_count=0,
        request_manifest_json=manifest,
        started_at=datetime(2026, 9, 14),
    )


# ==================== status constants / transition table ================
def test_status_constants() -> None:
    assert ATPP_STATUSES == {
        ATPP_RUNNING, ATPP_SUCCEEDED, ATPP_FAILED, ATPP_OUTCOME_UNKNOWN,
    }
    assert ATPP_TERMINAL_STATUSES == {ATPP_SUCCEEDED, ATPP_FAILED, ATPP_OUTCOME_UNKNOWN}


@pytest.mark.parametrize("target", [ATPP_SUCCEEDED, ATPP_FAILED, ATPP_OUTCOME_UNKNOWN])
def test_running_may_transition_to_any_terminal(target) -> None:
    assert atpp_transition_allowed(ATPP_RUNNING, target)


_TERMINAL = [ATPP_SUCCEEDED, ATPP_FAILED, ATPP_OUTCOME_UNKNOWN]
_ANY = [*_TERMINAL, ATPP_RUNNING]


@pytest.mark.parametrize("source", _TERMINAL)
@pytest.mark.parametrize("target", _ANY)
def test_terminal_states_never_transition_further(source, target) -> None:
    assert not atpp_transition_allowed(source, target)


def test_same_status_transition_is_not_allowed() -> None:
    assert not atpp_transition_allowed(ATPP_RUNNING, ATPP_RUNNING)


# ==================== repository: allowed/invalid transitions ============
def test_add_running_then_mark_succeeded(session: Session) -> None:
    run = _add_running(session)
    session.commit()
    assert run.status == ATPP_RUNNING

    _repo(session).mark_succeeded(
        run, http_status=200, response_projection_snapshot_hash="h" * 64,
        received_count=0, inserted_count=0, updated_count=0, unchanged_count=0,
        finished_at=datetime(2026, 9, 14, 0, 1),
    )
    assert run.status == ATPP_SUCCEEDED


def test_mark_failed_and_mark_outcome_unknown_are_valid_from_running(session: Session) -> None:
    r1 = _add_running(session)
    _repo(session).mark_failed(
        r1, error_message="x", finished_at=datetime(2026, 9, 14), server_code="bad_entry",
    )
    assert r1.status == ATPP_FAILED

    r2 = _add_running(session)
    _repo(session).mark_outcome_unknown(
        r2, error_message="y", finished_at=datetime(2026, 9, 14),
    )
    assert r2.status == ATPP_OUTCOME_UNKNOWN


@pytest.mark.parametrize("terminal", [ATPP_SUCCEEDED, ATPP_FAILED, ATPP_OUTCOME_UNKNOWN])
def test_invalid_transition_from_terminal_is_rejected(session: Session, terminal) -> None:
    run = _add_running(session)
    if terminal == ATPP_SUCCEEDED:
        _repo(session).mark_succeeded(
            run, http_status=200, response_projection_snapshot_hash="h" * 64,
            received_count=0, inserted_count=0, updated_count=0, unchanged_count=0,
            finished_at=datetime(2026, 9, 14),
        )
    elif terminal == ATPP_FAILED:
        _repo(session).mark_failed(run, error_message="x", finished_at=datetime(2026, 9, 14))
    else:
        _repo(session).mark_outcome_unknown(
            run, error_message="x", finished_at=datetime(2026, 9, 14)
        )

    with pytest.raises(AffiliateProjectionPushError):
        _repo(session).mark_failed(run, error_message="again", finished_at=datetime(2026, 9, 14))


def test_reload_and_retransition_is_rejected(session: Session) -> None:
    """terminal -> terminal はロード経由でも拒否される (append-only の保証)。"""

    run = _add_running(session)
    session.commit()
    _repo(session).mark_succeeded(
        run, http_status=200, response_projection_snapshot_hash="h" * 64,
        received_count=0, inserted_count=0, updated_count=0, unchanged_count=0,
        finished_at=datetime(2026, 9, 14),
    )
    session.commit()

    reloaded = _repo(session).get_by_id(run.id)
    with pytest.raises(AffiliateProjectionPushError):
        _repo(session).mark_outcome_unknown(
            reloaded, error_message="x", finished_at=datetime(2026, 9, 14)
        )


# ==================== origin-scoped queries / ordering ====================
def test_latest_for_origin_orders_newest_first(session: Session) -> None:
    r1 = _add_running(session, snapshot_hash="1" * 64)
    session.commit()
    _repo(session).mark_succeeded(
        r1, http_status=200, response_projection_snapshot_hash="1" * 64,
        received_count=0, inserted_count=0, updated_count=0, unchanged_count=0,
        finished_at=datetime(2026, 9, 14),
    )
    session.commit()
    r2 = _add_running(session, snapshot_hash="2" * 64)
    session.commit()

    runs = _repo(session).latest_for_origin(_ORIGIN)
    assert [r.id for r in runs] == [r2.id, r1.id]


def test_latest_for_origin_is_isolated_by_origin(session: Session) -> None:
    _add_running(session, origin="https://a.example.test")
    r_b = _add_running(session, origin="https://b.example.test")
    session.commit()

    runs = _repo(session).latest_for_origin("https://b.example.test")
    assert [r.id for r in runs] == [r_b.id]


def test_latest_succeeded_for_origin_ignores_non_succeeded(session: Session) -> None:
    r1 = _add_running(session)
    session.commit()
    _repo(session).mark_failed(r1, error_message="x", finished_at=datetime(2026, 9, 14))
    r2 = _add_running(session)
    session.commit()

    assert _repo(session).latest_succeeded_for_origin(_ORIGIN) is None

    _repo(session).mark_succeeded(
        r2, http_status=200, response_projection_snapshot_hash="h" * 64,
        received_count=0, inserted_count=0, updated_count=0, unchanged_count=0,
        finished_at=datetime(2026, 9, 14),
    )
    session.commit()
    assert _repo(session).latest_succeeded_for_origin(_ORIGIN).id == r2.id


# ==================== metadata / no PII-ish leakage ========================
def test_run_model_has_no_secret_or_body_columns() -> None:
    cols = set(AffiliateTargetProjectionPushRun.__table__.columns.keys())
    forbidden = {
        "shared_secret", "secret", "signature", "response_body", "raw_body",
        "headers", "request_headers", "authorization", "token", "full_token",
        "destination_url", "updated_at",
    }
    assert cols.isdisjoint(forbidden)
    assert "created_at" in cols
    assert "updated_at" not in cols


def test_run_can_be_listed_via_orm(session: Session) -> None:
    _add_running(session)
    session.commit()
    rows = session.scalars(select(AffiliateTargetProjectionPushRun)).all()
    assert len(rows) == 1
