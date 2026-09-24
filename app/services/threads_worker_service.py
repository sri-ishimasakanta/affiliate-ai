"""ThreadsWorkerService -- 常駐 worker の仕事を DB に結びつける (T4.1、PLAN 専用)。

各仕事は **自分の次回時刻を自分で決める**:

- ``health``: 設定の状態 (disabled / misconfigured / ready) を見る。heartbeat 間隔。
- ``queue_observation``: 提案と公開の状態の指紋を取る。変わっていたら公開の評価を
  前倒しする (承認が増えたのに次の定期評価まで気付かない、を避ける)。
- ``publication_evaluation``: queue を評価し、**次に評価すべき時刻ちょうど** を
  返す (間隔が明ける時刻・公開窓が開く時刻)。5 分の枠には丸めない。
- ``insights_refresh``: 投稿の若さに応じて 30〜60 分おき、成熟後はまれに。
  **T4.1 では取得しない。** 期限が来た投稿を「取得するなら今」と報告するだけ
  (実際の取得は C8 の ``import_threads_insights`` ステップが行う)。
- ``approval_notification_flush``: T4.2 の仕事。登録はするが無効。

T4.1 の worker が書き込むのは **自分のロック行だけ** である。Threads へも、
メールへも、WordPress へも、タスクスケジューラへも一切触れない。
"""

from __future__ import annotations

import os
import socket
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.article.fact_freshness import ensure_aware
from app.models import (
    PUB_PUBLISHED,
    SNAPSHOT_FAILED,
    ThreadsInsightSnapshot,
    ThreadsPublication,
)
from app.operations.local_time import to_local
from app.operations.lock import OperationsLockService
from app.operations.policy import get_policy as get_operations_policy
from app.services.threads_queue_service import ThreadsQueueService
from app.social.threads.measurement import classify_maturity
from app.social.threads.policy import (
    ThreadsMeasurementPolicy,
    ThreadsOperationsPolicy,
    get_measurement_policy,
)
from app.social.threads.policy import (
    get_operations_policy as get_threads_operations_policy,
)
from app.social.threads.queue import AUTOMATIC_PUBLICATION_ENABLED, MAX_PUBLICATIONS_PER_CYCLE
from app.social.threads.schedule import daily_activity, publication_timing, window_is_open
from app.social.threads.worker import (
    MODE_PLAN,
    SUBSYSTEM_APPROVAL_NOTIFICATION_FLUSH,
    SUBSYSTEM_HEALTH,
    SUBSYSTEM_INSIGHTS_REFRESH,
    SUBSYSTEM_PUBLICATION_EVALUATION,
    SUBSYSTEM_QUEUE_OBSERVATION,
    SubsystemResult,
    ThreadsWorker,
    WorkerSchedule,
)

WORKER_LOCK_NAME = "threads_worker"
#: 仕事が「今すぐもう一度」を返しても空回りしないための最小の間隔。
_MIN_RESCHEDULE = timedelta(seconds=1)


class ThreadsWorkerLock:
    """C8 と同じ :class:`OperationsLockService` を、別のロック名で使う。

    2 つ目の worker は取得に失敗して **何もせずに** 終わる。1 つ目は邪魔されない。
    プロセスが死んでも ``stale_after_minutes`` を過ぎれば次の worker が回収できる
    (タスクスケジューラからの再起動に備える)。
    """

    def __init__(self, session_factory, *, stale_after_minutes: int, owner_label: str) -> None:
        self._factory = session_factory
        self._stale = stale_after_minutes
        self._label = owner_label[:128]
        self._held = False

    def acquire(self, now: datetime) -> dict:
        with self._factory() as session:
            try:
                outcome = OperationsLockService(session).acquire(
                    lock_name=WORKER_LOCK_NAME,
                    owner_label=self._label,
                    stale_after_minutes=self._stale,
                    now=now,
                )
            except IntegrityError:
                # 同時に起動した別の worker が先に行を作った。
                session.rollback()
                return {"acquired": False, "lock_name": WORKER_LOCK_NAME, "race": True}
        self._held = outcome.acquired
        return {
            "acquired": outcome.acquired,
            "lock_name": WORKER_LOCK_NAME,
            "owner_label": self._label if outcome.acquired else None,
            "blocking_owner_label": outcome.blocking_owner_label,
            "reclaimed_stale": outcome.reclaimed_stale,
        }

    def heartbeat(self, now: datetime) -> None:
        if not self._held:
            return
        with self._factory() as session:
            OperationsLockService(session).heartbeat(lock_name=WORKER_LOCK_NAME, now=now)

    def release(self, now: datetime) -> None:
        if not self._held:
            return
        with self._factory() as session:
            OperationsLockService(session).release(lock_name=WORKER_LOCK_NAME, now=now)
        self._held = False


def default_owner_label() -> str:
    return f"threads-worker pid={os.getpid()} host={socket.gethostname()} mode={MODE_PLAN}"


class ThreadsWorkerService:
    def __init__(
        self,
        session_factory,
        *,
        settings,
        threads_service=None,
        policy: ThreadsOperationsPolicy | None = None,
        measurement_policy: ThreadsMeasurementPolicy | None = None,
        timezone: ZoneInfo | None = None,
    ) -> None:
        self._factory = session_factory
        self._settings = settings
        self._threads = threads_service
        self._policy = policy or get_threads_operations_policy()
        self._measurement = measurement_policy or get_measurement_policy()
        self._tz = timezone or get_operations_policy().timezone
        self._last_fingerprint: tuple | None = None

    # -- construction ------------------------------------------------------------
    def _queue(self, session) -> ThreadsQueueService:
        return ThreadsQueueService(
            session,
            settings=self._settings,
            threads_service=self._threads,
            policy=self._policy,
            measurement_policy=self._measurement,
            timezone=self._tz,
        )

    def build_schedule(self, now: datetime) -> WorkerSchedule:
        schedule = WorkerSchedule()
        for name in (
            SUBSYSTEM_HEALTH,
            SUBSYSTEM_QUEUE_OBSERVATION,
            SUBSYSTEM_PUBLICATION_EVALUATION,
            SUBSYSTEM_INSIGHTS_REFRESH,
        ):
            schedule.register(name, first_run_at=now)
        flush = self._policy.subsystem(SUBSYSTEM_APPROVAL_NOTIFICATION_FLUSH)
        schedule.register(
            SUBSYSTEM_APPROVAL_NOTIFICATION_FLUSH,
            first_run_at=None,
            enabled=False,
            disabled_reason=(
                f"approval notification batching is deferred to {flush.get('deferred_to', 'T4.2')}"
            ),
        )
        return schedule

    def handlers(self) -> dict:
        return {
            SUBSYSTEM_HEALTH: self._health,
            SUBSYSTEM_QUEUE_OBSERVATION: self._queue_observation,
            SUBSYSTEM_PUBLICATION_EVALUATION: self._publication_evaluation,
            SUBSYSTEM_INSIGHTS_REFRESH: self._insights_refresh,
        }

    def build_worker(self, *, now: datetime, clock=None, sleep=None, lock=None) -> ThreadsWorker:
        return ThreadsWorker(
            handlers=self.handlers(),
            schedule=self.build_schedule(now),
            heartbeat=timedelta(seconds=self._policy.heartbeat_max_seconds),
            clock=clock,
            sleep=sleep,
            lock=lock,
        )

    def build_lock(self) -> ThreadsWorkerLock:
        return ThreadsWorkerLock(
            self._factory,
            stale_after_minutes=self._policy.stale_lock_after_minutes,
            owner_label=default_owner_label(),
        )

    # -- handlers ----------------------------------------------------------------
    def _interval(self, name: str, default_minutes: int) -> timedelta:
        return timedelta(
            minutes=int(self._policy.subsystem(name).get("interval_minutes", default_minutes))
        )

    def _health(self, now: datetime) -> SubsystemResult:
        with self._factory() as session:
            status = self._queue(session).config_status()
        return SubsystemResult(
            next_run_at=now + self._interval(SUBSYSTEM_HEALTH, 5),
            summary={
                "threads_state": status.state,
                "config_issues": list(status.config_issues),
                "network_calls": 0,
            },
        )

    def _queue_observation(self, now: datetime) -> SubsystemResult:
        with self._factory() as session:
            queue = self._queue(session)
            fingerprint = queue.approval_fingerprint()
            counts = queue.counts()
        changed = self._last_fingerprint is not None and fingerprint != self._last_fingerprint
        self._last_fingerprint = fingerprint
        return SubsystemResult(
            next_run_at=now + self._interval(SUBSYSTEM_QUEUE_OBSERVATION, 10),
            summary={**counts, "changed_since_last_observation": changed},
            # 承認が増えた・状態が変わったら、公開の評価を定期時刻まで待たせない。
            wake={SUBSYSTEM_PUBLICATION_EVALUATION: now} if changed else {},
        )

    def _publication_evaluation(self, now: datetime) -> SubsystemResult:
        with self._factory() as session:
            evaluation = self._queue(session).evaluate(now=now)
        next_at = max(evaluation.next_evaluation_at, now + _MIN_RESCHEDULE)
        return SubsystemResult(
            next_run_at=next_at,
            summary={
                "blockers": list(evaluation.blockers),
                "problems": list(evaluation.problems),
                "eligible_count": evaluation.eligible_count,
                "next_candidate_id": (
                    evaluation.next_candidate.proposal_id if evaluation.next_candidate else None
                ),
                "would_publish_now": evaluation.would_publish_now,
                "evidence_state": evaluation.evidence_state,
            },
            publications=0,
        )

    def _insights_refresh(self, now: datetime) -> SubsystemResult:
        """期限が来た投稿を報告するだけ。**T4.1 では Meta に問い合わせない。**"""

        config = self._policy.subsystem(SUBSYSTEM_INSIGHTS_REFRESH)
        by_maturity = config.get("interval_minutes_by_maturity") or {}
        idle = timedelta(minutes=int(config.get("idle_interval_minutes", 60)))
        stop_after = float(config.get("stop_refresh_after_hours", 336))

        due: list[int] = []
        tracked: list[dict] = []
        intervals: list[timedelta] = []
        with self._factory() as session:
            rows = session.scalars(
                select(ThreadsPublication).where(
                    ThreadsPublication.status == PUB_PUBLISHED,
                    ThreadsPublication.published_at.is_not(None),
                )
            ).all()
            for row in rows:
                age = (now - ensure_aware(row.published_at)).total_seconds() / 3600.0
                if age > stop_after:
                    continue
                stage = classify_maturity(age, self._measurement).stage
                interval = timedelta(minutes=int(by_maturity.get(stage, 60)))
                last = session.scalars(
                    select(ThreadsInsightSnapshot.observed_at)
                    .where(
                        ThreadsInsightSnapshot.threads_publication_id == row.id,
                        ThreadsInsightSnapshot.outcome != SNAPSHOT_FAILED,
                    )
                    .order_by(ThreadsInsightSnapshot.observed_at.desc())
                    .limit(1)
                ).first()
                due_at = ensure_aware(last) + interval if last else now
                if due_at <= now:
                    due.append(row.id)
                intervals.append(interval)
                tracked.append(
                    {"publication_id": row.id, "maturity": stage, "due_at": due_at.isoformat()}
                )
        # PLAN なので取得はしない。期限が来たものが「期限のまま」空回りしないよう、
        # 次の確認は最も短い間隔ぶん先にする。
        next_at = now + (min(intervals) if intervals else idle)
        return SubsystemResult(
            next_run_at=next_at,
            summary={
                "tracked": tracked,
                "would_refresh": due,
                "network_calls": 0,
                "note": "PLAN only; the C8 import_threads_insights step performs real reads",
            },
        )

    # -- status ------------------------------------------------------------------
    def status(self, *, now: datetime, schedule: WorkerSchedule | None = None) -> dict:
        """CLI が出す現在の状態。**読むだけ。**"""

        now = ensure_aware(now)
        with self._factory() as session:
            queue = self._queue(session)
            config = queue.config_status()
            evaluation = queue.evaluate(now=now)
            latest = queue.latest_publication()
            counts = queue.counts()
            today = queue.published_today(now)
            latest_snapshot = None
            if latest is not None:
                latest_snapshot = session.scalars(
                    select(ThreadsInsightSnapshot)
                    .where(
                        ThreadsInsightSnapshot.threads_publication_id == latest.id,
                        ThreadsInsightSnapshot.outcome != SNAPSHOT_FAILED,
                    )
                    .order_by(ThreadsInsightSnapshot.observed_at.desc())
                    .limit(1)
                ).first()
            latest_view = None
            if latest is not None:
                published = ensure_aware(latest.published_at)
                age_hours = (now - published).total_seconds() / 3600.0
                maturity = classify_maturity(age_hours, self._measurement)
                latest_view = {
                    "publication_id": latest.id,
                    "proposal_id": latest.proposal_id,
                    "angle": latest.angle,
                    "published_at": published.isoformat(),
                    "published_local": to_local(published, self._tz).isoformat(),
                    "minutes_since": round((now - published).total_seconds() / 60.0, 1),
                    "maturity": maturity.stage,
                    "comparable": maturity.comparable,
                    "latest_snapshot_at": (
                        ensure_aware(latest_snapshot.observed_at).isoformat()
                        if latest_snapshot
                        else None
                    ),
                }

        timing = publication_timing(
            now=now,
            last_published_at=ensure_aware(latest.published_at) if latest else None,
            policy=self._policy,
            tz=self._tz,
        )
        return {
            "now": now.isoformat(),
            "now_local": to_local(now, self._tz).isoformat(),
            "timezone": self._tz.key,
            "policy_version": self._policy.policy_version,
            "worker_mode": MODE_PLAN,
            "automatic_publication_enabled": AUTOMATIC_PUBLICATION_ENABLED,
            "max_publications_per_cycle": MAX_PUBLICATIONS_PER_CYCLE,
            "threads": {
                "state": config.state,
                "enabled": config.enabled,
                "config_issues": list(config.config_issues),
                "healthy": config.state != "misconfigured",
            },
            "publication_window": {
                "start": self._policy.publication_window.start.isoformat("minutes"),
                "end": self._policy.publication_window.end.isoformat("minutes"),
                "open_now": window_is_open(self._policy.publication_window, now, self._tz),
            },
            "approval_notification_window": {
                "start": self._policy.approval_notification_window.start.isoformat("minutes"),
                "end": self._policy.approval_notification_window.end.isoformat("minutes"),
                "open_now": window_is_open(
                    self._policy.approval_notification_window, now, self._tz
                ),
                "delivery_enabled": False,
                "deferred_to": "T4.2",
            },
            "latest_publication": latest_view,
            "soft_gap": {
                "minutes": self._policy.soft_min_gap_minutes,
                **timing.as_dict(self._tz),
            },
            "daily_activity": daily_activity(today, self._policy),
            "proposals": counts,
            "queue": evaluation.as_dict(self._tz),
            "hard_blockers": list(evaluation.blockers),
            "problems": list(evaluation.problems),
            "subsystems": [self._subsystem_view(s) for s in schedule.states()] if schedule else [],
            "side_effects": {
                "threads_writes": 0,
                "network_calls": 0,
                "approval_emails": 0,
                "wordpress_writes": 0,
                "go_probes": 0,
                "scheduler_changes": 0,
            },
        }

    def _subsystem_view(self, state) -> dict:
        view = state.as_dict()
        view["next_run_local"] = (
            to_local(state.next_run_at, self._tz).isoformat() if state.next_run_at else None
        )
        return view


__all__ = ["WORKER_LOCK_NAME", "ThreadsWorkerLock", "ThreadsWorkerService", "default_owner_label"]
