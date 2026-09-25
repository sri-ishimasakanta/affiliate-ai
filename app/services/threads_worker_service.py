"""ThreadsWorkerService -- 常駐 worker の仕事を DB に結びつける (T4.1、PLAN 専用)。

各仕事は **自分の次回時刻を自分で決める**:

- ``health``: 設定の状態 (disabled / misconfigured / ready) を見る。heartbeat 間隔。
- ``queue_observation``: 提案と公開の状態の指紋を取る。変わっていたら公開の評価を
  前倒しする (承認が増えたのに次の定期評価まで気付かない、を避ける)。
- ``publication_evaluation``: queue を評価し、**次に評価すべき時刻ちょうど** を
  返す (間隔が明ける時刻・公開窓が開く時刻)。5 分の枠には丸めない。
- ``insights_refresh``: 投稿の若さに応じて 30〜60 分おき、成熟後はまれに。
  ``collect_insights=True`` のときだけ、期限が来た投稿を **読むだけ** で取得する
  (T4.2)。取得は C8 と同じ :class:`ThreadsInsightsService` を通るので、欠測の扱いも
  失敗の記録も同じ。直前にどちらかが観測していれば取りに行かない。
- ``approval_notification_flush``: 承認依頼のまとめ送りが「いま送るべきか」を評価する
  (T4.2)。送るのは ``send_approval_digests=True`` のときだけ。

既定 (どちらのフラグも False) の worker が書き込むのは **自分のロック行だけ**。
どのモードでも **公開はしない** (Threads への書き込みは 0 件)。WordPress にも
タスクスケジューラにも触れない。
"""

from __future__ import annotations

import os
import socket
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

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
from app.social.threads.queue import MAX_PUBLICATIONS_PER_CYCLE
from app.social.threads.schedule import daily_activity, publication_timing, window_is_open
from app.social.threads.worker import (
    MODE_PLAN,
    SUBSYSTEM_APPROVAL_NOTIFICATION_FLUSH,
    SUBSYSTEM_APPROVAL_SYNC,
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
        #: 取得時に DB が発行した所有者証明。**ログにも出力にも出さない。**
        self._token: str | None = None

    def acquire(self, now: datetime) -> dict:
        with self._factory() as session:
            outcome = OperationsLockService(session).acquire(
                lock_name=WORKER_LOCK_NAME,
                owner_label=self._label,
                stale_after_minutes=self._stale,
                now=now,
            )
        self._token = outcome.owner_token if outcome.acquired else None
        return {
            "acquired": outcome.acquired,
            "lock_name": WORKER_LOCK_NAME,
            "owner_label": self._label if outcome.acquired else None,
            "blocking_owner_label": outcome.blocking_owner_label,
            "reclaimed_stale": outcome.reclaimed_stale,
            "lost_race": outcome.lost_race,
        }

    @property
    def held(self) -> bool:
        return self._token is not None

    def heartbeat(self, now: datetime) -> bool:
        """所有権がまだあれば True。**False なら worker は仕事を止める。**"""

        if self._token is None:
            return False
        with self._factory() as session:
            alive = OperationsLockService(session).heartbeat(
                lock_name=WORKER_LOCK_NAME, owner_token=self._token, now=now
            )
        if not alive:
            self._token = None
        return alive

    def release(self, now: datetime) -> bool:
        """自分のロックだけを解放する。他人が回収したロックには触れない。"""

        if self._token is None:
            return False
        with self._factory() as session:
            released = OperationsLockService(session).release(
                lock_name=WORKER_LOCK_NAME, owner_token=self._token, now=now
            )
        self._token = None
        return released


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
        collect_insights: bool = False,
        send_approval_digests: bool = False,
        notifier=None,
        relay_client=None,
        auto_publish: bool = False,
        sync_approvals: bool = False,
        publish_sleep=None,
        alert_notifiers=None,
    ) -> None:
        self._factory = session_factory
        self._settings = settings
        self._threads = threads_service
        self._policy = policy or get_threads_operations_policy()
        self._measurement = measurement_policy or get_measurement_policy()
        self._tz = timezone or get_operations_policy().timezone
        self._last_fingerprint: tuple | None = None
        #: 読むだけの Meta 呼び出しを許すか (T4.2)。既定は許さない。
        self._collect_insights = collect_insights
        #: 承認依頼メールを実際に送るか (T4.2)。既定は送らない。**人が明示したときだけ。**
        self._send_digests = send_approval_digests
        self._notifier = notifier
        self._relay = relay_client
        #: この worker が実際に行った外部への作用 (固定の 0 ではなく数えた値)。
        self._counters = {"network_calls": 0, "approval_emails": 0, "threads_writes": 0}
        #: T4.3: 自動公開を worker として許すか。**ポリシーも有効でなければ公開しない。**
        self._auto_publish = auto_publish
        #: T4.3: 携帯での決定を中継から取り込むか。
        self._sync_approvals = sync_approvals
        #: T3 の「コンテナ作成後に待つ」ための sleep (試験で差し替える)。
        self._publish_sleep = publish_sleep
        #: build_worker で渡されたロック (自動公開のゲートに使う)。
        self._lock = None
        #: 自動公開の異常を知らせる notifier (None なら設定から作る。試験では差し替える)。
        self._alert_notifiers = alert_notifiers

    @property
    def capabilities(self) -> dict:
        return {
            "collect_insights": self._collect_insights,
            "send_approval_digests": self._send_digests,
            "sync_approvals": self._sync_approvals,
            "auto_publish_flag": self._auto_publish,
            "auto_publish_policy": self._policy.automatic_publication_enabled,
            # 公開しうるのは、フラグとポリシーの両方がそろったときだけ。
            "publish": self._auto_publish and self._policy.automatic_publication_enabled,
        }

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
        schedule.register(
            SUBSYSTEM_APPROVAL_SYNC,
            first_run_at=now if self._sync_approvals else None,
            enabled=self._sync_approvals,
            disabled_reason=None if self._sync_approvals else "start with --sync-approvals",
        )
        flush = self._policy.subsystem(SUBSYSTEM_APPROVAL_NOTIFICATION_FLUSH)
        enabled = bool(flush.get("enabled", False))
        schedule.register(
            SUBSYSTEM_APPROVAL_NOTIFICATION_FLUSH,
            first_run_at=now if enabled else None,
            enabled=enabled,
            disabled_reason=None if enabled else "disabled by policy",
        )
        return schedule

    def handlers(self) -> dict:
        return {
            SUBSYSTEM_HEALTH: self._health,
            SUBSYSTEM_QUEUE_OBSERVATION: self._queue_observation,
            SUBSYSTEM_PUBLICATION_EVALUATION: self._publication_evaluation,
            SUBSYSTEM_INSIGHTS_REFRESH: self._insights_refresh,
            SUBSYSTEM_APPROVAL_NOTIFICATION_FLUSH: self._approval_notification_flush,
            SUBSYSTEM_APPROVAL_SYNC: self._approval_sync,
        }

    def build_worker(self, *, now: datetime, clock=None, sleep=None, lock=None) -> ThreadsWorker:
        self._lock = lock
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
            next_run_at=now + self._interval(SUBSYSTEM_QUEUE_OBSERVATION, 5),
            summary={**counts, "changed_since_last_observation": changed},
            # 承認が増えた・提案が増えた・状態が変わったら、公開の評価と
            # まとめ送りの評価を、それぞれの定期時刻まで待たせない。
            wake=(
                {SUBSYSTEM_PUBLICATION_EVALUATION: now, SUBSYSTEM_APPROVAL_NOTIFICATION_FLUSH: now}
                if changed
                else {}
            ),
        )

    def _auto_publisher(self, session):
        from app.services.threads_auto_publisher import ThreadsAutoPublisher

        threads = self._threads
        if threads is None:
            from app.social.threads.service import ThreadsService

            threads = ThreadsService(self._settings)
        extra = {"sleep": self._publish_sleep} if self._publish_sleep is not None else {}
        return ThreadsAutoPublisher(
            session,
            settings=self._settings,
            threads_service=threads,
            policy=self._policy,
            measurement_policy=self._measurement,
            timezone=self._tz,
            flag_enabled=self._auto_publish,
            lock_held=bool(self._lock is not None and getattr(self._lock, "held", False)),
            **extra,
        )

    def _publication_evaluation(self, now: datetime) -> SubsystemResult:
        """公開を評価する。自動公開はフラグ・ポリシー・ロックがそろったときだけ。

        1 回の評価で公開を試みるのは最大 1 件。試みたら (成否によらず) 状態が変わる
        ので、次の評価時刻は公開後の状態から計算し直す。
        """

        with self._factory() as session:
            evaluation = self._queue(session).evaluate(now=now)
            summary = {
                "blockers": list(evaluation.blockers),
                "problems": list(evaluation.problems),
                "eligible_count": evaluation.eligible_count,
                "next_candidate_id": (
                    evaluation.next_candidate.proposal_id if evaluation.next_candidate else None
                ),
                "would_publish_now": evaluation.would_publish_now,
                "evidence_state": evaluation.evidence_state,
                "auto_publish": None,
            }
            next_at = evaluation.next_evaluation_at
            publications = 0
            if self._auto_publish:
                publisher = self._auto_publisher(session)
                result = publisher.publish_one(now=now)
                summary["auto_publish"] = result.as_dict()
                self._counters["threads_writes"] += result.threads_writes
                if result.preflight is not None:
                    self._counters["network_calls"] += 1
                self._counters["network_calls"] += result.threads_writes
                if result.attempted:
                    publications = 1
                if result.next_evaluation_at is not None:
                    next_at = result.next_evaluation_at
                summary["alerts_recorded"] = self._alert_on_autopublish(session, result, now)
        return SubsystemResult(
            next_run_at=max(next_at, now + _MIN_RESCHEDULE),
            summary=summary,
            publications=publications,
        )

    def _alert_on_autopublish(self, session, result, now: datetime) -> int:
        """自動公開がうまくいかなかったら、その場でアラートを記録・通知する (T4.3)。

        不確定・照合待ちは queue 全体を止める状態なので、翌朝の日次監視まで黙って
        待たない。同じ問題は fingerprint で 1 行にまとまり、cooldown 中は再通知しない
        (C8 と同じ OperationsAlertService)。成績ではアラートを出さない。
        """

        from app.operations.threads_health import (
            build_autopublish_preflight_draft,
            build_publication_alert_drafts,
        )
        from app.services.threads_insights_service import ThreadsInsightsService

        drafts = []
        if result.outcome == "preflight_failed":
            error = (result.preflight or {}).get("error") or {}
            drafts.append(
                build_autopublish_preflight_draft(error.get("category"), error.get("reason"))
            )
        elif result.outcome == "failed":
            error = result.error or {}
            drafts.append(
                build_autopublish_preflight_draft(error.get("category"), error.get("reason"))
            )
        if result.attempted:
            health = ThreadsInsightsService(
                session,
                settings=self._settings,
                threads_service=self._threads,
                policy=self._measurement,
                timezone=self._tz,
            )
            drafts += build_publication_alert_drafts(health.publication_health_inputs(now=now))
        if not drafts:
            return 0

        from app.operations.notifications import build_notifiers
        from app.operations.policy import get_policy
        from app.services.operations_alert_service import OperationsAlertService

        notifiers = (
            self._alert_notifiers
            if self._alert_notifiers is not None
            else build_notifiers(self._settings)
        )
        outcome = OperationsAlertService(
            session, policy=get_policy(), notifiers=notifiers
        ).record_and_notify(drafts, now=now)
        return outcome.recorded

    def _approval_sync(self, now: datetime) -> SubsystemResult:
        """携帯で下された決定を取り込む (T4.3)。既存の sync をそのまま使う。

        承認は queue に入るだけで、ここからは公開しない。取り込めたら公開の評価と
        queue の観測を前倒しする (承認を 5 分以内に反映する)。
        """

        from app.services.mobile_approval_service import MobileApprovalService

        with self._factory() as session:
            outcome = MobileApprovalService(
                session, settings=self._settings, relay_client=self._relay
            ).sync(execute=True, now=now)
        self._counters["network_calls"] += 1
        wake = (
            {SUBSYSTEM_PUBLICATION_EVALUATION: now, SUBSYSTEM_QUEUE_OBSERVATION: now}
            if outcome.applied
            else {}
        )
        return SubsystemResult(
            next_run_at=now + self._interval(SUBSYSTEM_APPROVAL_SYNC, 5),
            summary={
                "fetched": outcome.fetched,
                "applied": outcome.applied,
                "skipped": outcome.skipped,
                "failed": outcome.failed,
            },
            wake=wake,
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
        # 取得してもしなくても、次の確認は最も短い間隔ぶん先にする
        # (期限が来たものが「期限のまま」空回りしないように)。
        next_at = now + (min(intervals) if intervals else idle)
        if not self._collect_insights or not due:
            return SubsystemResult(
                next_run_at=next_at,
                summary={
                    "tracked": tracked,
                    "would_refresh": due,
                    "refreshed": [],
                    "network_calls": 0,
                    "note": (
                        "PLAN only; start the worker with --collect-insights to read, "
                        "or rely on the C8 import_threads_insights step"
                    )
                    if not self._collect_insights
                    else None,
                },
            )

        from app.services.threads_insights_service import ThreadsInsightsService

        refreshed: list[dict] = []
        calls = 0
        with self._factory() as session:
            collector = ThreadsInsightsService(
                session,
                settings=self._settings,
                threads_service=self._threads,
                policy=self._measurement,
                timezone=self._tz,
            )
            for publication_id in due:
                # 読むだけ。欠測は NULL のまま、失敗は失敗の観測として残る (C8 と同じ)。
                result = collector.collect(publication_id=publication_id, execute=True, now=now)
                calls += result.imported + result.failed
                refreshed.extend(result.details)
        self._counters["network_calls"] += calls
        return SubsystemResult(
            next_run_at=next_at,
            summary={
                "tracked": tracked,
                "would_refresh": due,
                # 失敗も隠さない。何を試して、どう失敗したか (redact 済みの理由) を残す。
                "refreshed": [
                    {
                        "publication_id": d.get("publication_id"),
                        "result": d.get("result"),
                        "reason": d.get("reason"),
                    }
                    for d in refreshed
                ],
                "network_calls": calls,
                "threads_writes": 0,
            },
        )

    def _approval_notification_flush(self, now: datetime) -> SubsystemResult:
        """まとめ送りが「いま送るべきか」を評価する。送るのはフラグが立っているときだけ。

        時刻は digest の計画が決める (cooldown / gather / 通知窓)。heartbeat のたびには
        走らず、固定の毎時 :00 にも合わせない。
        """

        from app.services.threads_approval_digest_service import ThreadsApprovalDigestService

        with self._factory() as session:
            digests = ThreadsApprovalDigestService(
                session,
                settings=self._settings,
                notifier=self._notifier,
                relay_client=self._relay,
                policy=self._policy,
                timezone=self._tz,
            )
            plan = digests.plan(now=now)
            summary = {
                "would_send": plan.would_send,
                "waiting_for": list(plan.waiting_for),
                "selected": [i.candidate.proposal_id for i in plan.selected],
                "deferred": [i.candidate.proposal_id for i in plan.deferred],
                "suppressed": [
                    {"proposal_id": i.candidate.proposal_id, "reason": i.reason}
                    for i in plan.suppressed
                ],
                "emails_sent": 0,
            }
            if plan.would_send and self._send_digests:
                outcome = digests.send(now=now, execute=True)
                summary["emails_sent"] = 1 if outcome.sent else 0
                self._counters["approval_emails"] += summary["emails_sent"]
                summary["digest"] = outcome.as_dict()
                # 送った (または送れなかった) 後の状態で、次の機会を計算し直す。
                plan = digests.plan(now=now)
        idle = timedelta(
            minutes=int(
                self._policy.subsystem(SUBSYSTEM_APPROVAL_NOTIFICATION_FLUSH).get(
                    "idle_interval_minutes", 30
                )
            )
        )
        next_at = plan.next_run_at
        if next_at <= now:
            # 送れる状態なのに送らない (PLAN) とき、heartbeat ごとに空回りしない。
            next_at = now + idle
        return SubsystemResult(next_run_at=max(next_at, now + _MIN_RESCHEDULE), summary=summary)

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
            # 最後に **試みた** 観測 (失敗も含む)。成功した観測だけを見せると、
            # 取得が失敗し続けていても「最後の観測」が古いまま黙って残って見える。
            latest_attempt = None
            if latest is not None:
                latest_attempt = session.scalars(
                    select(ThreadsInsightSnapshot)
                    .where(ThreadsInsightSnapshot.threads_publication_id == latest.id)
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
                    "latest_attempt_at": (
                        ensure_aware(latest_attempt.observed_at).isoformat()
                        if latest_attempt
                        else None
                    ),
                    "latest_attempt_outcome": latest_attempt.outcome if latest_attempt else None,
                    "latest_attempt_error_category": (
                        latest_attempt.error_category if latest_attempt else None
                    ),
                    "latest_attempt_error": (
                        latest_attempt.error_message if latest_attempt else None
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
            "automatic_publication_enabled": self.capabilities["publish"],
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
                "delivery_enabled": self._send_digests,
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
            "capabilities": self.capabilities,
            "publication_dry_run": self._dry_run(now),
            "side_effects": {
                "threads_writes": self._counters["threads_writes"],
                "network_calls": self._counters["network_calls"],
                "approval_emails": self._counters["approval_emails"],
                "wordpress_writes": 0,
                "go_probes": 0,
                "scheduler_changes": 0,
            },
        }

    def _dry_run(self, now: datetime) -> dict:
        """自動公開が有効なら何が起きるか。**外にも DB にも触れない。**"""

        with self._factory() as session:
            return self._auto_publisher(session).dry_run(now=now).as_dict()

    def _subsystem_view(self, state) -> dict:
        view = state.as_dict()
        view["next_run_local"] = (
            to_local(state.next_run_at, self._tz).isoformat() if state.next_run_at else None
        )
        return view


__all__ = ["WORKER_LOCK_NAME", "ThreadsWorkerLock", "ThreadsWorkerService", "default_owner_label"]
