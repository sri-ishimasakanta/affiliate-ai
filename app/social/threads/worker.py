"""常駐 Threads worker の骨組み (T4.1)。

作らないもの::

    while True:
        run_everything()
        sleep(300)

作るもの: **仕事ごとに自分の次回時刻 (next_run_at) を持つ** 小さな scheduler。

- worker は次に期限が来る仕事の時刻まで眠り、起きたら **期限が来た仕事だけ** を
  動かす。
- 眠る長さの上限は heartbeat (約 5 分)。これは生存確認とロック更新のためで、
  仕事の時刻を 5 分刻みに揃えるためではない。
- 公開の評価は自分で次回時刻を決める。最後の公開が 09:17 なら、次の評価は
  11:17 ちょうどに来る (5 分の枠に丸めない)。その前に worker が別の用事で
  起きても、公開の評価は走らない。
- 仕事の失敗は worker を止めない。その仕事だけを heartbeat 後に再試行する。

境界:

- 公開の経路は ``publication_evaluation`` の handler の中にある、ゲート付きの自動公開
  (T4.3) だけ。外から ``publisher`` を差し込むと起動を拒否する (ゲートを迂回させない)。
- 1 回のサイクルで公開してよいのは最大 1 件、という不変条件をここに固定する。
- worker の骨組み自体は、ネットワークにもメールにも WordPress にもタスク
  スケジューラにも触れない (外への作用はすべて handler 側の明示的なフラグ次第)。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.social.threads.queue import MAX_PUBLICATIONS_PER_CYCLE

# -- subsystem names -----------------------------------------------------------
SUBSYSTEM_HEALTH = "health"
SUBSYSTEM_QUEUE_OBSERVATION = "queue_observation"
SUBSYSTEM_PUBLICATION_EVALUATION = "publication_evaluation"
SUBSYSTEM_INSIGHTS_REFRESH = "insights_refresh"
SUBSYSTEM_APPROVAL_NOTIFICATION_FLUSH = "approval_notification_flush"
#: 携帯での決定を中継から取り込む (T4.3)。
SUBSYSTEM_APPROVAL_SYNC = "approval_sync"
#: 投稿案の在庫の保守 (T6)。低頻度。提案を用意するだけで、承認も公開もしない。
SUBSYSTEM_PROPOSAL_STOCK_MAINTENANCE = "proposal_stock_maintenance"
#: 同じ時刻に期限が来たときの実行順。決定を取り込み、queue を観測してから公開を評価する。
SUBSYSTEM_ORDER = (
    SUBSYSTEM_HEALTH,
    SUBSYSTEM_APPROVAL_SYNC,
    SUBSYSTEM_QUEUE_OBSERVATION,
    SUBSYSTEM_PUBLICATION_EVALUATION,
    SUBSYSTEM_INSIGHTS_REFRESH,
    SUBSYSTEM_APPROVAL_NOTIFICATION_FLUSH,
    SUBSYSTEM_PROPOSAL_STOCK_MAINTENANCE,
)

MODE_PLAN = "plan"
WORKER_MODES = (MODE_PLAN,)

EXIT_OK = 0
EXIT_ALREADY_RUNNING = 4
#: 実行中にロックの所有権を失った (古いと判定されて別の worker が回収した)。
EXIT_LOCK_LOST = 5


class AutomaticPublicationUnavailable(RuntimeError):
    """ゲートを迂回する公開の経路は作らせない。"""


@dataclass
class SubsystemResult:
    """仕事 1 回分の結果。**次にいつ動くかは仕事自身が決める。**"""

    next_run_at: datetime | None
    summary: dict = field(default_factory=dict)
    #: 他の仕事の次回時刻を前倒ししたいとき (例: 承認が増えた → 公開を再評価)。
    wake: dict[str, datetime] = field(default_factory=dict)
    #: この仕事が行った公開の件数。T4.1 では常に 0。
    publications: int = 0


@dataclass
class SubsystemState:
    name: str
    enabled: bool
    next_run_at: datetime | None
    last_run_at: datetime | None = None
    runs: int = 0
    failures: int = 0
    last_summary: dict = field(default_factory=dict)
    last_error: str | None = None
    disabled_reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "disabled_reason": self.disabled_reason,
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "runs": self.runs,
            "failures": self.failures,
            "last_error": self.last_error,
            "last_summary": self.last_summary,
        }


class WorkerSchedule:
    """仕事ごとの next_run_at を持つだけ。**どの仕事も他の仕事の時刻に従わない。**"""

    def __init__(self) -> None:
        self._states: dict[str, SubsystemState] = {}

    def register(
        self,
        name: str,
        *,
        first_run_at: datetime | None,
        enabled: bool = True,
        disabled_reason: str | None = None,
    ) -> None:
        self._states[name] = SubsystemState(
            name=name,
            enabled=enabled,
            next_run_at=first_run_at if enabled else None,
            disabled_reason=disabled_reason,
        )

    def state(self, name: str) -> SubsystemState:
        return self._states[name]

    def states(self) -> list[SubsystemState]:
        order = {name: i for i, name in enumerate(SUBSYSTEM_ORDER)}
        return sorted(self._states.values(), key=lambda s: order.get(s.name, len(order)))

    def due(self, now: datetime) -> list[str]:
        return [
            s.name
            for s in self.states()
            if s.enabled and s.next_run_at is not None and s.next_run_at <= now
        ]

    def record(self, name: str, now: datetime, result: SubsystemResult) -> None:
        state = self._states[name]
        state.last_run_at = now
        state.runs += 1
        state.last_summary = result.summary
        state.last_error = None
        state.next_run_at = result.next_run_at
        for other, moment in result.wake.items():
            target = self._states.get(other)
            if target is None or not target.enabled:
                continue
            if target.next_run_at is None or moment < target.next_run_at:
                target.next_run_at = moment

    def record_failure(self, name: str, now: datetime, error: str, retry_at: datetime) -> None:
        state = self._states[name]
        state.last_run_at = now
        state.failures += 1
        state.last_error = error
        state.next_run_at = retry_at

    def next_wake(self, now: datetime, heartbeat: timedelta) -> datetime:
        """次に起きる時刻 = 最も早い仕事の期限。ただし heartbeat を超えて眠らない。"""

        limit = now + heartbeat
        upcoming = [
            s.next_run_at for s in self._states.values() if s.enabled and s.next_run_at is not None
        ]
        if not upcoming:
            return limit
        return max(now, min(min(upcoming), limit))


@dataclass
class CycleReport:
    started_at: datetime
    ran: list[str]
    skipped_not_due: list[str]
    publications: int
    next_wake_at: datetime
    lock_lost: bool = False

    def as_dict(self) -> dict:
        return {
            "started_at": self.started_at.isoformat(),
            "ran": list(self.ran),
            "skipped_not_due": list(self.skipped_not_due),
            "publications": self.publications,
            "next_wake_at": self.next_wake_at.isoformat(),
            "lock_lost": self.lock_lost,
        }


@dataclass
class WorkerRun:
    exit_code: int
    mode: str
    cycles: list[CycleReport] = field(default_factory=list)
    lock: dict | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def publications(self) -> int:
        return sum(c.publications for c in self.cycles)


Handler = Callable[[datetime], SubsystemResult]


class ThreadsWorker:
    """仕事の時刻管理と 1 サイクルの実行だけを受け持つ。業務判断は handler 側。"""

    def __init__(
        self,
        *,
        handlers: dict[str, Handler],
        schedule: WorkerSchedule,
        heartbeat: timedelta,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        lock=None,
        publisher=None,
        mode: str = MODE_PLAN,
        on_event: Callable[[dict], None] | None = None,
    ) -> None:
        if publisher is not None:
            # 公開はゲート付きの handler の中だけで起きる。外から経路を足させない。
            raise AutomaticPublicationUnavailable(
                "an external publisher cannot be plugged into the worker; publication only "
                "happens through the gated publication_evaluation handler"
            )
        if mode not in WORKER_MODES:
            raise ValueError(f"unsupported worker mode: {mode}")
        self._handlers = handlers
        self._schedule = schedule
        self._heartbeat = heartbeat
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._lock = lock
        self._mode = mode
        self._stop = False
        #: 起動・仕事の実行・警告・停止だけを知らせる口 (眠るたびには呼ばない)。
        #: 知らせる側の失敗で worker を止めない。
        self._on_event = on_event

    def _emit(self, event: str, **fields) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event({"event": event, "at": self._clock(), **fields})
        except Exception:  # noqa: BLE001 - ログの失敗で仕事を止めない
            pass

    @property
    def schedule(self) -> WorkerSchedule:
        return self._schedule

    def stop(self) -> None:
        self._stop = True

    # -- one cycle -------------------------------------------------------------
    def run_cycle(self) -> CycleReport:
        """期限が来た仕事だけを 1 回ずつ動かす。**公開は最大 1 件。**"""

        now = self._clock()
        due = self._schedule.due(now)
        not_due = [s.name for s in self._schedule.states() if s.enabled and s.name not in due]
        ran: list[str] = []
        publications = 0
        for name in due:
            handler = self._handlers.get(name)
            if handler is None:
                continue
            try:
                result = handler(now)
            except Exception as exc:  # noqa: BLE001 - 1 つの仕事の失敗で worker を止めない
                error = f"{type(exc).__name__}: {exc}"[:300]
                self._schedule.record_failure(name, now, error, now + self._heartbeat)
                self._emit(
                    "subsystem_failed",
                    name=name,
                    error=error,
                    next_run_at=self._schedule.state(name).next_run_at,
                )
                continue
            publications += result.publications
            if publications > MAX_PUBLICATIONS_PER_CYCLE:
                # 構造上起きないはずだが、起きたら続行しない。
                raise RuntimeError("a worker cycle attempted more than one publication")
            self._schedule.record(name, now, result)
            ran.append(name)
            self._emit(
                "subsystem",
                name=name,
                summary=result.summary,
                publications=result.publications,
                next_run_at=self._schedule.state(name).next_run_at,
            )
        lock_lost = False
        if self._lock is not None and self._lock.heartbeat(now) is False:
            # 所有権を失った。これ以上この worker が仕事をしてはいけない。
            lock_lost = True
            self._stop = True
            self._emit("lock_lost")
        return CycleReport(
            lock_lost=lock_lost,
            started_at=now,
            ran=ran,
            skipped_not_due=not_due,
            publications=publications,
            next_wake_at=self._schedule.next_wake(now, self._heartbeat),
        )

    # -- loop ------------------------------------------------------------------
    def run(self, *, max_cycles: int | None = None) -> WorkerRun:
        run = WorkerRun(exit_code=EXIT_OK, mode=self._mode)
        if self._lock is not None:
            acquired = self._lock.acquire(self._clock())
            run.lock = acquired
            if not acquired.get("acquired"):
                # 先に動いている worker を邪魔しない。ネットワークも書き込みもしない。
                run.exit_code = EXIT_ALREADY_RUNNING
                run.notes.append("another Threads worker holds the lock; exiting without work")
                self._emit("already_running", blocking=acquired.get("blocking_owner_label"))
                return run
            self._emit("lock_acquired", reclaimed_stale=bool(acquired.get("reclaimed_stale")))
        reason = "completed"
        try:
            cycles = 0
            while not self._stop:
                report = self.run_cycle()
                run.cycles.append(report)
                cycles += 1
                if report.lock_lost:
                    run.exit_code = EXIT_LOCK_LOST
                    run.notes.append("lost the worker lock; stopping without further work")
                    break
                if max_cycles is not None and cycles >= max_cycles:
                    break
                if self._sleep is None:
                    break
                delay = (report.next_wake_at - self._clock()).total_seconds()
                self._sleep(max(0.0, delay))
            if run.exit_code == EXIT_LOCK_LOST:
                reason = "lost_lock"
        except KeyboardInterrupt:
            reason = "interrupted"
            raise
        except BaseException:
            reason = "crashed"
            raise
        finally:
            if self._lock is not None:
                self._lock.release(self._clock())
            self._emit(
                "stopped",
                reason=reason,
                exit_code=run.exit_code,
                cycles=len(run.cycles),
                publications=run.publications,
            )
        return run


__all__ = [
    "EXIT_ALREADY_RUNNING",
    "EXIT_LOCK_LOST",
    "EXIT_OK",
    "MODE_PLAN",
    "SUBSYSTEM_APPROVAL_NOTIFICATION_FLUSH",
    "SUBSYSTEM_APPROVAL_SYNC",
    "SUBSYSTEM_HEALTH",
    "SUBSYSTEM_INSIGHTS_REFRESH",
    "SUBSYSTEM_ORDER",
    "SUBSYSTEM_PROPOSAL_STOCK_MAINTENANCE",
    "SUBSYSTEM_PUBLICATION_EVALUATION",
    "SUBSYSTEM_QUEUE_OBSERVATION",
    "AutomaticPublicationUnavailable",
    "CycleReport",
    "SubsystemResult",
    "SubsystemState",
    "ThreadsWorker",
    "WorkerRun",
    "WorkerSchedule",
]
