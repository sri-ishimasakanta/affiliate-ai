"""常駐 worker の骨組み (T4.1、pure)。

pin する契約:

- heartbeat のたびに全部の仕事を動かさない。期限が来た仕事だけ。
- 仕事ごとの next_run_at は独立している。
- 公開評価の時刻は 5 分の枠で決まらない (11:17 は 11:17)。
- 眠る長さは heartbeat (5 分) を超えない。
- 2 つ目の worker は何もせずに終わり、1 つ目を邪魔しない。
- 1 つの仕事の失敗で worker は止まらない。
- 自動公開の経路は T4.1 では差し込めない。1 サイクルで公開できるのは最大 1 件。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.social.threads.worker import (
    EXIT_ALREADY_RUNNING,
    EXIT_OK,
    SUBSYSTEM_HEALTH,
    SUBSYSTEM_INSIGHTS_REFRESH,
    SUBSYSTEM_PUBLICATION_EVALUATION,
    SUBSYSTEM_QUEUE_OBSERVATION,
    AutomaticPublicationUnavailable,
    SubsystemResult,
    ThreadsWorker,
    WorkerSchedule,
)

START = datetime(2026, 9, 24, 0, 0, tzinfo=UTC)  # 09:00 JST
HEARTBEAT = timedelta(minutes=5)


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class _Handlers:
    """仕事ごとの呼び出し時刻を記録する。間隔は仕事ごとに違う。"""

    def __init__(self, publication_next: datetime | None = None) -> None:
        self.calls: dict[str, list[datetime]] = {}
        self._publication_next = publication_next

    def _record(self, name: str, now: datetime) -> None:
        self.calls.setdefault(name, []).append(now)

    def health(self, now):
        self._record(SUBSYSTEM_HEALTH, now)
        return SubsystemResult(next_run_at=now + timedelta(minutes=5))

    def queue(self, now):
        self._record(SUBSYSTEM_QUEUE_OBSERVATION, now)
        return SubsystemResult(next_run_at=now + timedelta(minutes=10))

    def publication(self, now):
        self._record(SUBSYSTEM_PUBLICATION_EVALUATION, now)
        # 最後の公開が 09:17 → 次の評価は 11:17 (状態で決まる時刻)。
        target = self._publication_next or now + timedelta(minutes=30)
        if now >= target:
            target = now + timedelta(minutes=30)
        return SubsystemResult(next_run_at=target)

    def insights(self, now):
        self._record(SUBSYSTEM_INSIGHTS_REFRESH, now)
        return SubsystemResult(next_run_at=now + timedelta(minutes=30))

    def mapping(self) -> dict:
        return {
            SUBSYSTEM_HEALTH: self.health,
            SUBSYSTEM_QUEUE_OBSERVATION: self.queue,
            SUBSYSTEM_PUBLICATION_EVALUATION: self.publication,
            SUBSYSTEM_INSIGHTS_REFRESH: self.insights,
        }


def _schedule(now: datetime) -> WorkerSchedule:
    schedule = WorkerSchedule()
    for name in (
        SUBSYSTEM_HEALTH,
        SUBSYSTEM_QUEUE_OBSERVATION,
        SUBSYSTEM_PUBLICATION_EVALUATION,
        SUBSYSTEM_INSIGHTS_REFRESH,
    ):
        schedule.register(name, first_run_at=now)
    return schedule


def _worker(handlers: _Handlers, clock: _Clock, **kw) -> ThreadsWorker:
    return ThreadsWorker(
        handlers=handlers.mapping(),
        schedule=_schedule(clock.now),
        heartbeat=HEARTBEAT,
        clock=clock,
        sleep=clock.sleep,
        **kw,
    )


# == independent scheduling ===================================================
def test_the_heartbeat_does_not_run_every_subsystem() -> None:
    clock = _Clock(START)
    handlers = _Handlers()
    _worker(handlers, clock).run(max_cycles=13)  # 開始 + 5 分おき 12 回 = 1 時間

    # 1 時間で: health は毎回、queue は 10 分おき、insights/公開評価は 30 分おき。
    assert len(handlers.calls[SUBSYSTEM_HEALTH]) == 13
    assert len(handlers.calls[SUBSYSTEM_QUEUE_OBSERVATION]) == 7
    assert len(handlers.calls[SUBSYSTEM_INSIGHTS_REFRESH]) == 3
    assert len(handlers.calls[SUBSYSTEM_PUBLICATION_EVALUATION]) == 3


def test_next_run_times_are_independent_per_subsystem() -> None:
    clock = _Clock(START)
    worker = _worker(_Handlers(), clock)
    worker.run_cycle()
    times = {s.name: s.next_run_at for s in worker.schedule.states() if s.enabled}
    assert times[SUBSYSTEM_HEALTH] == START + timedelta(minutes=5)
    assert times[SUBSYSTEM_QUEUE_OBSERVATION] == START + timedelta(minutes=10)
    assert times[SUBSYSTEM_INSIGHTS_REFRESH] == START + timedelta(minutes=30)
    assert len(set(times.values())) == 3  # health / queue / (insights, 公開評価)


def test_publication_evaluation_runs_at_its_own_time_not_a_five_minute_slot() -> None:
    """09:17 公開 → 11:17。worker は 5 分おきに起きるが、評価はちょうど 11:17 に来る。"""

    target = datetime(2026, 9, 24, 2, 17, tzinfo=UTC)  # 11:17 JST
    clock = _Clock(START)
    handlers = _Handlers(publication_next=target)
    _worker(handlers, clock).run(max_cycles=40)

    evaluations = handlers.calls[SUBSYSTEM_PUBLICATION_EVALUATION]
    assert evaluations[0] == START
    assert evaluations[1] == target
    # その間 (09:00 と 11:17 の間) に公開評価は一度も走っていない。
    assert not any(START < t < target for t in evaluations)


def test_sleep_never_exceeds_the_heartbeat() -> None:
    schedule = WorkerSchedule()
    schedule.register("far", first_run_at=START + timedelta(hours=6))
    assert schedule.next_wake(START, HEARTBEAT) == START + HEARTBEAT


def test_sleep_is_shorter_when_work_is_due_sooner() -> None:
    schedule = WorkerSchedule()
    schedule.register("soon", first_run_at=START + timedelta(minutes=2, seconds=13))
    assert schedule.next_wake(START, HEARTBEAT) == START + timedelta(minutes=2, seconds=13)


def test_a_wake_request_pulls_another_subsystem_forward() -> None:
    schedule = _schedule(START)
    schedule.record(
        SUBSYSTEM_PUBLICATION_EVALUATION, START, SubsystemResult(START + timedelta(hours=2))
    )
    later = START + timedelta(minutes=10)
    schedule.record(
        SUBSYSTEM_QUEUE_OBSERVATION,
        later,
        SubsystemResult(
            next_run_at=later + timedelta(minutes=10),
            wake={SUBSYSTEM_PUBLICATION_EVALUATION: later},
        ),
    )
    assert schedule.state(SUBSYSTEM_PUBLICATION_EVALUATION).next_run_at == later


def test_a_disabled_subsystem_never_runs() -> None:
    clock = _Clock(START)
    handlers = _Handlers()
    schedule = _schedule(START)
    schedule.register("approval_notification_flush", first_run_at=None, enabled=False)
    ran: list = []
    mapping = {**handlers.mapping(), "approval_notification_flush": lambda now: ran.append(now)}
    ThreadsWorker(
        handlers=mapping, schedule=schedule, heartbeat=HEARTBEAT, clock=clock, sleep=clock.sleep
    ).run(max_cycles=20)
    assert ran == []


# == failure isolation ========================================================
def test_one_failing_subsystem_does_not_stop_the_others() -> None:
    clock = _Clock(START)
    handlers = _Handlers()
    mapping = handlers.mapping()

    def broken(now):
        raise RuntimeError("database is locked")

    mapping[SUBSYSTEM_INSIGHTS_REFRESH] = broken
    worker = ThreadsWorker(
        handlers=mapping,
        schedule=_schedule(START),
        heartbeat=HEARTBEAT,
        clock=clock,
        sleep=clock.sleep,
    )
    run = worker.run(max_cycles=3)

    assert run.exit_code == EXIT_OK
    assert len(handlers.calls[SUBSYSTEM_HEALTH]) == 3
    state = worker.schedule.state(SUBSYSTEM_INSIGHTS_REFRESH)
    assert state.failures == 3
    assert "database is locked" in state.last_error


# == locking ==================================================================
class _Lock:
    def __init__(self, *, acquired: bool) -> None:
        self._acquired = acquired
        self.events: list[str] = []

    def acquire(self, now):
        self.events.append("acquire")
        return {"acquired": self._acquired, "blocking_owner_label": "worker-1"}

    def heartbeat(self, now):
        self.events.append("heartbeat")

    def release(self, now):
        self.events.append("release")


def test_a_second_worker_exits_without_doing_any_work() -> None:
    clock = _Clock(START)
    handlers = _Handlers()
    lock = _Lock(acquired=False)
    run = _worker(handlers, clock, lock=lock).run(max_cycles=5)

    assert run.exit_code == EXIT_ALREADY_RUNNING
    assert handlers.calls == {}
    assert run.cycles == []
    # 他人のロックは解放しない。
    assert lock.events == ["acquire"]


def test_the_lock_is_heartbeated_every_cycle_and_released_at_the_end() -> None:
    clock = _Clock(START)
    lock = _Lock(acquired=True)
    _worker(_Handlers(), clock, lock=lock).run(max_cycles=3)
    assert lock.events == ["acquire", "heartbeat", "heartbeat", "heartbeat", "release"]


def test_the_lock_is_released_even_when_the_loop_crashes() -> None:
    clock = _Clock(START)
    lock = _Lock(acquired=True)

    def exploding_sleep(_seconds):
        raise KeyboardInterrupt

    worker = ThreadsWorker(
        handlers=_Handlers().mapping(),
        schedule=_schedule(START),
        heartbeat=HEARTBEAT,
        clock=clock,
        sleep=exploding_sleep,
        lock=lock,
    )
    with pytest.raises(KeyboardInterrupt):
        worker.run()
    assert lock.events[-1] == "release"


# == one post per cycle / no automatic publication =============================
def test_a_publisher_cannot_be_plugged_in_during_t4_1() -> None:
    with pytest.raises(AutomaticPublicationUnavailable):
        ThreadsWorker(
            handlers={},
            schedule=WorkerSchedule(),
            heartbeat=HEARTBEAT,
            publisher=object(),
        )


def test_a_cycle_that_tries_to_publish_twice_is_stopped() -> None:
    """構造上の保険: 2 件目の公開を報告した時点でサイクルを止める。"""

    schedule = WorkerSchedule()
    schedule.register("a", first_run_at=START)
    schedule.register("b", first_run_at=START)

    def publishes(now):
        return SubsystemResult(next_run_at=now + HEARTBEAT, publications=1)

    worker = ThreadsWorker(
        handlers={"a": publishes, "b": publishes},
        schedule=schedule,
        heartbeat=HEARTBEAT,
        clock=lambda: START,
    )
    with pytest.raises(RuntimeError, match="more than one publication"):
        worker.run_cycle()


def test_plan_worker_reports_zero_publications() -> None:
    clock = _Clock(START)
    run = _worker(_Handlers(), clock).run(max_cycles=10)
    assert run.publications == 0
