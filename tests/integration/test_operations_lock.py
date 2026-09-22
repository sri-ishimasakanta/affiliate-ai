"""OperationsLockService の統合テスト (C8)。

スケジューラが前回の実行中に次を開始しないことと、プロセスが死んでも恒久
デッドロックにならないことを pin する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OperationsLock
from app.operations.lock import OperationsLockService

_NOW = datetime(2026, 9, 23, 6, 30, tzinfo=UTC)


def _service(session: Session) -> OperationsLockService:
    return OperationsLockService(session)


def test_first_acquire_succeeds(session: Session) -> None:
    outcome = _service(session).acquire(owner_run_id=1, now=_NOW)
    assert outcome.acquired is True
    assert outcome.reclaimed_stale is False


def test_second_acquire_is_blocked_while_held(session: Session) -> None:
    _service(session).acquire(owner_run_id=1, owner_label="daily", now=_NOW)

    outcome = _service(session).acquire(owner_run_id=2, now=_NOW + timedelta(minutes=5))

    assert outcome.acquired is False
    assert outcome.blocking_owner_run_id == 1
    assert outcome.blocking_owner_label == "daily"


def test_acquire_succeeds_after_release(session: Session) -> None:
    _service(session).acquire(owner_run_id=1, now=_NOW)
    assert _service(session).release(owner_run_id=1) is True

    assert _service(session).acquire(owner_run_id=2, now=_NOW).acquired is True


def test_stale_lock_is_reclaimed_after_the_gate(session: Session) -> None:
    """プロセスが死んで解放されなかったロックを、時間経過で回収できる。"""

    _service(session).acquire(owner_run_id=1, stale_after_minutes=120, now=_NOW)

    outcome = _service(session).acquire(
        owner_run_id=2, stale_after_minutes=120, now=_NOW + timedelta(minutes=121)
    )

    assert outcome.acquired is True
    # 黙って奪ったことにしない。
    assert outcome.reclaimed_stale is True


def test_fresh_lock_is_not_reclaimed(session: Session) -> None:
    _service(session).acquire(owner_run_id=1, stale_after_minutes=120, now=_NOW)
    outcome = _service(session).acquire(
        owner_run_id=2, stale_after_minutes=120, now=_NOW + timedelta(minutes=119)
    )
    assert outcome.acquired is False


def test_heartbeat_extends_the_lock(session: Session) -> None:
    _service(session).acquire(owner_run_id=1, stale_after_minutes=60, now=_NOW)
    _service(session).heartbeat(now=_NOW + timedelta(minutes=50))

    outcome = _service(session).acquire(
        owner_run_id=2, stale_after_minutes=60, now=_NOW + timedelta(minutes=100)
    )
    assert outcome.acquired is False


def test_release_does_not_free_another_owners_lock(session: Session) -> None:
    _service(session).acquire(owner_run_id=1, now=_NOW)
    assert _service(session).release(owner_run_id=99) is False
    assert _service(session).acquire(owner_run_id=2, now=_NOW).acquired is False


def test_only_one_lock_row_exists_per_name(session: Session) -> None:
    for run_id in (1, 2, 3):
        _service(session).acquire(owner_run_id=run_id, now=_NOW)
        _service(session).release(owner_run_id=run_id)
    assert len(session.scalars(select(OperationsLock)).all()) == 1
