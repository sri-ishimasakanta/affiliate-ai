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
    # T4.2: 延長には所有者の証明が要る。
    assert _service(session).heartbeat(owner_run_id=1, now=_NOW + timedelta(minutes=50)) is True

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


# == T4.2: atomic reclaim and ownership =======================================
def _stale_lock(session: Session) -> None:
    _service(session).acquire(owner_label="dead", stale_after_minutes=15, now=_NOW)


def test_two_claimants_that_both_saw_a_stale_lock_produce_exactly_one_owner(
    session: Session, monkeypatch
) -> None:
    """旧実装の競合を再現する: 2 人とも「古い」と読んだあとで、2 人とも書きに行く。

    読み取りの段階を「古いロックの行」に固定しても、書き込みは条件付き UPDATE なので
    2 人目は 0 行になり、取得できない。
    """

    _stale_lock(session)
    later = _NOW + timedelta(minutes=16)
    service_a, service_b = _service(session), _service(session)

    stale_row = service_a._observe("operations_pipeline")
    stale_snapshot = {
        "released_at": stale_row.released_at,
        "heartbeat_at": stale_row.heartbeat_at,
        "owner_run_id": stale_row.owner_run_id,
        "owner_label": stale_row.owner_label,
        "acquired_at": stale_row.acquired_at,
    }

    class _Stale:
        """2 人目にも、1 人目が書く前の古い行を見せる。"""

        def __init__(self, token):
            self.owner_token = token
            for key, value in stale_snapshot.items():
                setattr(self, key, value)

    first = service_a.acquire(owner_label="a", stale_after_minutes=15, now=later)
    assert first.acquired is True and first.reclaimed_stale is True

    real_observe = service_b._observe
    calls = {"n": 0}

    def observe_stale_then_real(name):
        calls["n"] += 1
        return _Stale(token=None) if calls["n"] == 1 else real_observe(name)

    monkeypatch.setattr(service_b, "_observe", observe_stale_then_real)
    second = service_b.acquire(owner_label="b", stale_after_minutes=15, now=later)

    assert second.acquired is False
    assert second.lost_race is True
    row = session.scalars(select(OperationsLock)).one()
    assert row.owner_label == "a"
    assert row.owner_token == first.owner_token


def test_the_loser_cannot_release_the_winners_lock(session: Session) -> None:
    _stale_lock(session)
    later = _NOW + timedelta(minutes=16)
    winner = _service(session).acquire(owner_label="a", stale_after_minutes=15, now=later)
    loser = _service(session).acquire(owner_label="b", stale_after_minutes=15, now=later)

    assert winner.acquired and not loser.acquired
    assert loser.owner_token is None
    assert _service(session).release(owner_token="not-the-owner") is False
    assert session.scalars(select(OperationsLock)).one().released_at is None


def test_a_reclaimed_workers_heartbeat_reports_lost_ownership(session: Session) -> None:
    """古くなった worker は、回収された後に heartbeat しても延長できない。"""

    old = _service(session).acquire(owner_label="old", stale_after_minutes=15, now=_NOW)
    new = _service(session).acquire(
        owner_label="new", stale_after_minutes=15, now=_NOW + timedelta(minutes=16)
    )
    assert new.acquired

    assert (
        _service(session).heartbeat(owner_token=old.owner_token, now=_NOW + timedelta(minutes=17))
        is False
    )
    assert (
        _service(session).heartbeat(owner_token=new.owner_token, now=_NOW + timedelta(minutes=17))
        is True
    )
    assert _service(session).release(owner_token=old.owner_token) is False
    assert _service(session).release(owner_token=new.owner_token) is True


def test_touching_a_lock_without_proving_ownership_is_refused(session: Session) -> None:
    import pytest

    _service(session).acquire(owner_run_id=1, now=_NOW)
    with pytest.raises(ValueError):
        _service(session).release()
    with pytest.raises(ValueError):
        _service(session).heartbeat()


def test_each_acquisition_gets_a_fresh_owner_token(session: Session) -> None:
    first = _service(session).acquire(owner_run_id=1, now=_NOW)
    _service(session).release(owner_token=first.owner_token)
    second = _service(session).acquire(owner_run_id=2, now=_NOW)
    assert first.owner_token and second.owner_token
    assert first.owner_token != second.owner_token


def _claim(factory, label, barrier, at, results, errors) -> None:
    try:
        with factory() as own:
            barrier.wait()
            results.append(
                OperationsLockService(own).acquire(
                    owner_label=label, stale_after_minutes=15, now=at
                )
            )
    except Exception as exc:  # noqa: BLE001 - スレッド内の例外を検証側へ運ぶ
        errors.append(exc)


def test_concurrent_stale_reclaim_has_exactly_one_winner(tmp_path) -> None:
    """別々の接続・別々のスレッドから同時に回収しに行く。勝者は毎回ちょうど 1 人。"""

    import threading

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models.base import Base

    engine = create_engine(
        f"sqlite:///{(tmp_path / 'locks.db').as_posix()}",
        connect_args={"timeout": 30, "check_same_thread": False},
    )
    Base.metadata.create_all(engine, tables=[OperationsLock.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    for round_number in range(15):
        base = _NOW + timedelta(hours=round_number)
        with factory() as setup:
            setup.query(OperationsLock).delete()
            setup.commit()
            OperationsLockService(setup).acquire(
                owner_label="dead", stale_after_minutes=15, now=base
            )

        barrier = threading.Barrier(4)
        results: list = []
        errors: list = []
        at = base + timedelta(minutes=16)
        threads = [
            threading.Thread(target=_claim, args=(factory, f"w{i}", barrier, at, results, errors))
            for i in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        winners = [r for r in results if r.acquired]
        assert len(winners) == 1, f"round {round_number}: {len(winners)} winners"
        with factory() as check:
            row = check.scalars(select(OperationsLock)).one()
            assert row.owner_token == winners[0].owner_token
    engine.dispose()
