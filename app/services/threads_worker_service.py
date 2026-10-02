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
    SUBSYSTEM_ACCOUNT_GROWTH_MAINTENANCE,
    SUBSYSTEM_ACCOUNT_POST_DISCOVERY,
    SUBSYSTEM_APPROVAL_NOTIFICATION_FLUSH,
    SUBSYSTEM_APPROVAL_SYNC,
    SUBSYSTEM_GROWTH_OPPORTUNITY,
    SUBSYSTEM_HEALTH,
    SUBSYSTEM_INSIGHTS_REFRESH,
    SUBSYSTEM_PERFORMANCE_FEEDBACK,
    SUBSYSTEM_PROPOSAL_STOCK_MAINTENANCE,
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
        maintain_proposal_stock: bool = False,
        stock_provider=None,
        maintain_growth_posts: bool = False,
        growth_client=None,
        growth_directory=None,
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
        self._counters = {
            "network_calls": 0,
            "approval_emails": 0,
            "threads_writes": 0,
            "publications": 0,
        }
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
        #: T6: 投稿案の在庫を保守するか。**提案を用意するだけ** (承認・公開はしない)。
        self._maintain_stock = maintain_proposal_stock
        self._stock_provider = stock_provider
        self._last_stock_run: datetime | None = None
        #: T6.3.3: 毎日 1 本の Growth Post を用意するか。**既定は用意しない** (人が有効にする)。
        self._maintain_growth = maintain_growth_posts
        self._growth_client = growth_client
        self._growth_directory = growth_directory
        #: T6.5: 最後に作った成績からの補助の参考 (メモリだけ。DB には書かない)。
        self._performance_feedback = None
        self._last_feedback_run: datetime | None = None
        #: C9: 最後に重い評価をした時刻・そのときの印・候補の指紋 (メモリだけ)。
        self._last_growth_evaluation: datetime | None = None
        self._growth_signature: str | None = None
        self._growth_fingerprint: str | None = None
        #: C9-C: 変換した行動の追跡の観測 (anchor ごと。メモリだけ。DB には書かない)。
        self._measurement_cache: dict = {}

    # -- growth opportunities (C9) -----------------------------------------------
    #: 既定は **無効** (本番ではまだ使わない)。方針の ``subsystems.growth_opportunity_evaluation``。
    GROWTH_OPPORTUNITY_DEFAULTS = {"enabled": False, "interval_minutes": 1440,
                                   "check_interval_minutes": 60, "min_interval_minutes": 360,
                                   "write_history": True}  # fmt: skip

    def _growth_opportunity_config(self) -> dict:
        return {**self.GROWTH_OPPORTUNITY_DEFAULTS,
                **self._policy.subsystem(SUBSYSTEM_GROWTH_OPPORTUNITY)}

    def _growth_signature_now(self, session) -> str:
        """軽い点検の印: 取り込みの成功の時刻と、新しい行の印 (重い評価はしない)。"""

        import hashlib

        from sqlalchemy import func, select

        from app.models import ChangeRequest, KeywordScore, ThreadsPostProposal
        from app.services.operations_source_health_service import collect_source_freshness

        freshness = {k: v.last_successful_import_at.isoformat()
                     if v.last_successful_import_at else None
                     for k, v in collect_source_freshness(session).items()}  # fmt: skip
        marks = [session.scalar(select(func.max(model.id))) for model in (
            ThreadsPublication, ThreadsPostProposal, ChangeRequest, KeywordScore)]
        blob = repr((sorted(freshness.items()), marks))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _growth_opportunity_evaluation(self, now: datetime) -> SubsystemResult:
        """成長の候補を評価して、C9 の履歴の表だけを更新する。**外に書かない・何も実行しない。**"""

        from app.services.growth_action_service import (
            GrowthActionHistory,
            build_inbox,
            history_tables_ready,
        )

        config = self._growth_opportunity_config()
        check = timedelta(minutes=int(config["check_interval_minutes"]))
        interval = timedelta(minutes=int(config["interval_minutes"]))
        minimum = timedelta(minutes=int(config["min_interval_minutes"]))
        with self._factory() as session:
            if not history_tables_ready(session):
                session.rollback()
                return SubsystemResult(next_run_at=now + interval, summary={
                    "evaluated": False, "reason": "history tables missing (migration "
                    "74bfaf6c9c9f not applied)", "db_writes": 0, "external_writes": 0})
            measured, measurement = self._measure_followups(session, now)
            signature = self._growth_signature_now(session)
            last = self._last_growth_evaluation
            due = last is None or now - last >= interval
            followup_changed = bool(measurement.get("changed"))
            changed = signature != self._growth_signature or followup_changed
            if not due and not (changed and now - last >= minimum):
                session.rollback()
                return SubsystemResult(next_run_at=now + check, summary={
                    "evaluated": False, "reason": "signature unchanged" if not changed
                    else "signature changed; waiting for the minimum interval",
                    "measurement": measurement,
                    "db_writes": 0, "external_writes": 0})  # fmt: skip
            followup = self._followup_provider(session, measured)
            box = build_inbox(session, settings=self._settings, now=now, followup=followup)
            written = None
            if config.get("write_history", True):
                written = GrowthActionHistory(session).apply_refresh(box["plan"], now=now)
            self._last_growth_evaluation = now
            self._growth_signature = signature
            self._growth_fingerprint = box["report"]["fingerprint"]
            counts = box["plan"].counts()
        return SubsystemResult(next_run_at=now + check, summary={
            "evaluated": True, "trigger": "interval" if due else (
                "followup_measured" if followup_changed else "signature_changed"),
            "measurement": measurement,
            "fingerprint": (self._growth_fingerprint or "")[:12], "counts": counts,
            "created": len((written or {}).get("created") or []),
            "db_writes": "growth_action_* only" if written else 0, "external_writes": 0})

    def _measure_followups(self, session, now: datetime) -> tuple[list, dict]:
        """C9-C: 期日が来た追跡の観測だけをし直す (読むだけ。外に問い合わせない)。"""

        from app.services.growth_measurement_service import (
            GrowthMeasurementService,
            measurement_ready,
        )

        if not measurement_ready(session):
            return [], {"anchors": 0, "measured": 0, "changed": []}
        try:
            measured, stats = GrowthMeasurementService(
                session, settings=self._settings, timezone=self.timezone).refresh(
                now=now, cache=self._measurement_cache)  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - 観測の失敗で成長の評価を止めない
            session.rollback()
            return [], {"anchors": 0, "measured": 0, "changed": [],
                        "error": f"{type(exc).__name__}"}
        session.rollback()
        return measured, {k: stats[k] for k in ("anchors", "effective", "measured", "reused",
                                                "changed", "next_measurement_at")}

    def _followup_provider(self, session, measured: list):
        from app.services.growth_measurement_service import GrowthMeasurementService

        service = GrowthMeasurementService(session, settings=self._settings,
                                           timezone=self.timezone)
        return lambda _now: service.followup_by_article(measured)

    # -- performance feedback (T6.5) ---------------------------------------------
    #: 方針に節が無いときの値 (``threads_operations_policy.json`` の
    #: ``subsystems.performance_feedback_evaluation`` で変えられる)。
    FEEDBACK_DEFAULTS = {"enabled": True, "interval_minutes": 360, "min_interval_minutes": 60,
                         "use_in_generation": False}  # fmt: skip

    def _feedback_config(self) -> dict:
        return {**self.FEEDBACK_DEFAULTS, **self._policy.subsystem(SUBSYSTEM_PERFORMANCE_FEEDBACK)}

    @property
    def performance_feedback(self):
        """最後に作った補助の参考 (まだ無ければ ``None``)。"""

        return self._performance_feedback

    @property
    def capabilities(self) -> dict:
        return {
            "collect_insights": self._collect_insights,
            "send_approval_digests": self._send_digests,
            "sync_approvals": self._sync_approvals,
            "maintain_proposal_stock": self._maintain_stock,
            "maintain_growth_posts": self._maintain_growth,
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
        # manual-post coexistence: 自アカウントの投稿の一覧 (読むだけ、15 分ごと)。Threads を
        # 読んでよい worker (--collect-insights か --auto-publish) だけ。
        reads = self._collect_insights or self._auto_publish
        schedule.register(
            SUBSYSTEM_ACCOUNT_POST_DISCOVERY,
            first_run_at=now if reads else None,
            enabled=reads,
            disabled_reason=None if reads else "start with --collect-insights or --auto-publish",
        )
        schedule.register(
            SUBSYSTEM_APPROVAL_SYNC,
            first_run_at=now if self._sync_approvals else None,
            enabled=self._sync_approvals,
            disabled_reason=None if self._sync_approvals else "start with --sync-approvals",
        )
        # 起動直後に 1 回だけ保守する (停止していた間の分をまとめて作らない: 1 回の
        # 上限と「答え待ちの依頼があれば出さない」がそのまま効く)。
        schedule.register(
            SUBSYSTEM_PROPOSAL_STOCK_MAINTENANCE,
            first_run_at=now if self._maintain_stock else None,
            enabled=self._maintain_stock,
            disabled_reason=(
                None if self._maintain_stock else "start with --maintain-proposal-stock"
            ),
        )
        schedule.register(
            SUBSYSTEM_ACCOUNT_GROWTH_MAINTENANCE,
            first_run_at=now if self._maintain_growth else None,
            enabled=self._maintain_growth,
            disabled_reason=(
                None if self._maintain_growth else "start with --maintain-growth-posts"
            ),
        )
        feedback = bool(self._feedback_config().get("enabled", True))
        schedule.register(
            SUBSYSTEM_PERFORMANCE_FEEDBACK,
            first_run_at=now if feedback else None,
            enabled=feedback,
            disabled_reason=None if feedback else "disabled by policy",
        )
        growth = bool(self._growth_opportunity_config().get("enabled", False))
        schedule.register(
            SUBSYSTEM_GROWTH_OPPORTUNITY,
            first_run_at=now if growth else None,
            enabled=growth,
            disabled_reason=None if growth else "disabled by policy (C9 not enabled yet)",
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
            SUBSYSTEM_ACCOUNT_POST_DISCOVERY: self._account_post_discovery,
            SUBSYSTEM_PUBLICATION_EVALUATION: self._publication_evaluation,
            SUBSYSTEM_INSIGHTS_REFRESH: self._insights_refresh,
            SUBSYSTEM_PERFORMANCE_FEEDBACK: self._performance_feedback_evaluation,
            SUBSYSTEM_GROWTH_OPPORTUNITY: self._growth_opportunity_evaluation,
            SUBSYSTEM_APPROVAL_NOTIFICATION_FLUSH: self._approval_notification_flush,
            SUBSYSTEM_APPROVAL_SYNC: self._approval_sync,
            SUBSYSTEM_PROPOSAL_STOCK_MAINTENANCE: self._proposal_stock_maintenance,
            SUBSYSTEM_ACCOUNT_GROWTH_MAINTENANCE: self._account_growth_maintenance,
        }

    @property
    def timezone(self) -> ZoneInfo:
        return self._tz

    @property
    def policy_version(self) -> str:
        return self._policy.policy_version

    def build_worker(
        self, *, now: datetime, clock=None, sleep=None, lock=None, on_event=None
    ) -> ThreadsWorker:
        self._lock = lock
        return ThreadsWorker(
            handlers=self.handlers(),
            schedule=self.build_schedule(now),
            heartbeat=timedelta(seconds=self._policy.heartbeat_max_seconds),
            clock=clock,
            sleep=sleep,
            lock=lock,
            on_event=on_event,
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
            wake=self._observation_wake(now) if changed else {},
        )

    def _observation_wake(self, now: datetime) -> dict:
        wake = {SUBSYSTEM_PUBLICATION_EVALUATION: now, SUBSYSTEM_APPROVAL_NOTIFICATION_FLUSH: now}
        if self._maintain_stock:
            # 在庫が動いたら保守も前倒しする。ただし最短間隔より早めない (debounce)。
            wake[SUBSYSTEM_PROPOSAL_STOCK_MAINTENANCE] = self._stock_wake_at(now)
        return wake

    def _stock_wake_at(self, now: datetime) -> datetime:
        minimum = timedelta(
            minutes=int(
                self._policy.subsystem(SUBSYSTEM_PROPOSAL_STOCK_MAINTENANCE).get(
                    "min_interval_minutes", 60
                )
            )
        )
        if self._last_stock_run is None:
            return now
        return max(now, self._last_stock_run + minimum)

    def _account_growth_maintenance(self, now: datetime) -> SubsystemResult:
        """今日の Growth Post を 1 本だけ用意する (T6.3.3)。**承認・却下・公開はしない。**

        期限が来ていなければ何も呼ばない (OpenAI にも Threads にも)。1 時間ごとに状態を見るだけ。
        フォロワー数を読む (読むだけ) のは ``--collect-insights`` のときだけで、生成の直前に 1 回。
        """

        from app.services.threads_growth_service import DEFAULT_DIRECTORY, ThreadsGrowthService
        from app.services.threads_openai_provider import build_responses_client
        from app.social.threads.growth import next_eligible_at

        client = (
            self._growth_client
            if self._growth_client is not None
            else build_responses_client(self._settings)
        )
        with self._factory() as session:
            outcome = ThreadsGrowthService(
                session,
                timezone=self._tz,
                client=client,
                threads_service=self._threads,
                directory=self._growth_directory or DEFAULT_DIRECTORY,
                collect_followers=self._collect_insights,
            ).maintain(now=now, execute=True)
        interval = self._interval(SUBSYSTEM_ACCOUNT_GROWTH_MAINTENANCE, 60)
        created = outcome.get("created")
        return SubsystemResult(
            next_run_at=max(min(now + interval, next_eligible_at(now, self._tz)),
                            now + _MIN_RESCHEDULE),  # fmt: skip
            summary={
                "date_jst": outcome.get("date_jst"),
                "due": outcome.get("due"),
                "active_proposal": outcome.get("active_proposal"),
                "published_today": len(outcome.get("published_today") or []),
                "created": created,
                "model_calls": outcome.get("model_calls"),
                "reason": outcome.get("reason"),
                "follower_target": outcome.get("follower_target"),
                "follower_target_reached": outcome.get("follower_target_reached"),
                "followers_observed": (outcome.get("follower_observation") or {}).get(
                    "followers_count"
                ),
                "follower_read": (outcome.get("follower_read") or {}).get("outcome"),
            },
            wake=(
                {SUBSYSTEM_APPROVAL_NOTIFICATION_FLUSH: now, SUBSYSTEM_QUEUE_OBSERVATION: now}
                if created
                else {}
            ),
        )

    def _performance_feedback_evaluation(self, now: datetime) -> SubsystemResult:
        """自分の投稿の成績を分析し、生成への補助の参考を作り直す (T6.5)。**読むだけ。**

        DB に書かない (分析は ``read_only_session``)。Threads に問い合わせない。結果はメモリに
        持つだけ。生成に使うのは方針の ``use_in_generation`` が真のときだけ (既定は使わない)。
        """

        from app.services.threads_performance_analysis_service import (
            ThreadsPerformanceAnalysisService,
        )
        from app.social.threads.performance_analysis import supported_values_for
        from app.social.threads.policy import get_policy as get_style_policy

        self._last_feedback_run = now
        with self._factory() as session:
            feedback = ThreadsPerformanceAnalysisService(
                session, settings=self._settings, timezone=self._tz
            ).feedback(as_of=now, supported_values=supported_values_for(
                get_style_policy(), self._measurement))  # fmt: skip
        previous = self._performance_feedback
        self._performance_feedback = feedback
        config = self._feedback_config()
        return SubsystemResult(
            next_run_at=now + timedelta(minutes=int(config["interval_minutes"])),
            summary={
                "mode": feedback.mode,
                "checkpoint": feedback.checkpoint,
                "cohort_n": feedback.cohort_n,
                "supported": len(feedback.supported),
                "weak": len(feedback.weak),
                "insufficient": len(feedback.insufficient),
                "fingerprint": feedback.fingerprint[:12],
                "changed": previous is None or previous.fingerprint != feedback.fingerprint,
                "used_in_generation": bool(config.get("use_in_generation")),
                "db_writes": 0,
                "network_calls": 0,
            },
        )

    def _feedback_wake_at(self, now: datetime) -> datetime:
        minimum = timedelta(minutes=int(self._feedback_config()["min_interval_minutes"]))
        if self._last_feedback_run is None:
            return now
        return max(now, self._last_feedback_run + minimum)

    def _stock_proposal_service(self, session):
        """在庫の保守に使う提案の service。補助の参考は方針で許されたときだけ渡す。"""

        if not self._feedback_config().get("use_in_generation"):
            return None
        from app.services.threads_proposal_service import ThreadsProposalService
        from app.social.threads.performance_analysis import neutral_feedback

        def provide(as_of):
            return self._performance_feedback or neutral_feedback(
                as_of.isoformat(), "no performance feedback evaluated yet; neutral")

        return ThreadsProposalService(session, performance_feedback_provider=provide)

    def _proposal_stock_maintenance(self, now: datetime) -> SubsystemResult:
        """投稿案の在庫を 1 回保守する (T6)。**承認・却下・公開はしない。**

        新しい提案は awaiting_approval で保存され、既存の digest が人へ依頼する。
        """

        from app.services.threads_proposal_stock_service import (
            ThreadsProposalStockService,
        )

        self._last_stock_run = now
        with self._factory() as session:
            outcome = ThreadsProposalStockService(
                session,
                settings=self._settings,
                threads_service=self._threads,
                policy=self._policy,
                provider=self._stock_provider,
                proposal_service=self._stock_proposal_service(session),
                timezone=self._tz,
                alert_notifiers=self._alert_notifiers,
            ).maintain(now=now, execute=True)
        plan = outcome.get("plan", {})
        created = outcome["created"]
        return SubsystemResult(
            next_run_at=now + self._interval(SUBSYSTEM_PROPOSAL_STOCK_MAINTENANCE, 360),
            summary={
                "needs_generation": plan.get("stock", {}).get("needs_generation"),
                "usable": plan.get("stock", {}).get("usable"),
                "created": len(created),
                "requests_created": len(outcome["requests_created"]),
                "pending_requests": len(plan.get("pending_requests", [])),
                "skipped": len(outcome["skipped"]),
                "failures": [f["reason"] for f in outcome["failures"]],
                "blocked_by": plan.get("blocked_by", []),
                "provider": plan.get("provider", {}).get("name"),
                "guidance": plan.get("learning", {}).get("mode"),
            },
            # 新しい提案は既存の digest の評価に任せる (送るかどうかは T4.2 が決める)。
            wake=(
                {SUBSYSTEM_APPROVAL_NOTIFICATION_FLUSH: now, SUBSYSTEM_QUEUE_OBSERVATION: now}
                if created
                else {}
            ),
        )

    def _account_post_service(self, session):
        from app.services.threads_account_post_service import ThreadsAccountPostService

        threads = self._threads
        if threads is None:
            from app.social.threads.service import ThreadsService

            threads = ThreadsService(self._settings)
        return ThreadsAccountPostService(
            session, threads_service=threads, listing_limit=self._policy.account_post_listing_limit,
            measurement_policy=self._measurement)  # fmt: skip

    def _account_post_discovery(self, now: datetime) -> SubsystemResult:
        """自アカウントの投稿の一覧を読み、system / manual / unknown を照合する (読むだけ)。

        manual 投稿は公開の本数・承認・Growth の枠に入らない。間隔・重複の判定に入る。読めなく
        ても worker は止めない (次の回に読み直す。公開の直前の読み直しは別に必ず行う)。
        """

        from app.services.threads_account_post_service import (
            AccountPostDiscoveryError,
            account_posts_ready,
        )

        interval = self._interval(SUBSYSTEM_ACCOUNT_POST_DISCOVERY, 15)
        with self._factory() as session:
            if not account_posts_ready(session):
                session.rollback()
                return SubsystemResult(next_run_at=now + interval, summary={
                    "discovered": False, "reason": "account post tables missing (migration "
                    "3d5382e2a6bd not applied)", "network_calls": 0})  # fmt: skip
            self._counters["network_calls"] += 1
            try:
                summary = self._account_post_service(session).refresh(now=now, source="periodic")
            except AccountPostDiscoveryError as exc:
                session.rollback()
                return SubsystemResult(next_run_at=now + interval, summary={
                    "discovered": False, "category": exc.category, "reason": exc.reason,
                    "network_calls": 1, "threads_writes": 0})  # fmt: skip
        changed = bool(summary["discovered"] or summary["origin_changed"]
                       or summary["superseded_proposals"] or summary["text_changed"])
        return SubsystemResult(
            next_run_at=now + interval,
            summary={**summary, "discovered_count": len(summary["discovered"]),
                     "network_calls": 1, "threads_writes": 0},
            # 新しい manual 投稿・提案の置き換えがあれば、公開の評価を待たせない (間隔を見直す)。
            wake={SUBSYSTEM_PUBLICATION_EVALUATION: now, SUBSYSTEM_QUEUE_OBSERVATION: now}
            if changed else {},
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
            # 実行の **前** の評価。この worker が公開しうるか (フラグとポリシー) で評価する。
            # 以前は publication_enabled を渡しておらず、公開できる worker でも
            # automatic_publication_disabled を出していた (実際の判断は別の評価だった)。
            evaluation = self._queue(session).evaluate(
                now=now, publication_enabled=self.capabilities["publish"]
            )
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
            growth_publications = 0
            if self._auto_publish:
                publisher = self._auto_publisher(session)
                result = publisher.publish_one(now=now)
                summary["auto_publish"] = result.as_dict()
                if result.next_blockers is not None:
                    # 公開 (の試み) の後、次の評価で効くブロッカー。実行前のものとは別に出す。
                    summary["next_blockers"] = list(result.next_blockers)
                self._counters["threads_writes"] += result.threads_writes
                self._counters["network_calls"] += result.network_calls
                if result.published:
                    self._counters["publications"] += 1
                if result.attempted:
                    publications = 1
                if result.next_evaluation_at is not None:
                    next_at = result.next_evaluation_at
                summary["alerts_recorded"] = self._alert_on_autopublish(session, result, now)
                # T6.3.3a: Growth Post の足し分の枠。記事の結果を問わず、記事の間隔を使わずに見る。
                growth = publisher.publish_growth_one(
                    now=now,
                    companion_publication_id=result.publication_id if result.published else None,
                )
                summary["growth"] = growth.as_dict()
                self._counters["threads_writes"] += growth.threads_writes
                self._counters["network_calls"] += growth.network_calls
                if growth.published:
                    self._counters["publications"] += 1
                if growth.attempted:
                    # 記事の 1 回 1 本の枠には数えない (足し分の枠で別に数える)。
                    growth_publications = 1
                    summary["alerts_recorded"] += self._alert_on_autopublish(
                        session, growth, now
                    )
                if growth.next_evaluation_at is not None:
                    next_at = min(next_at, growth.next_evaluation_at)
        return SubsystemResult(
            next_run_at=max(next_at, now + _MIN_RESCHEDULE),
            summary=summary,
            publications=publications,
            growth_publications=growth_publications,
        )

    def _alert_on_autopublish(self, session, result, now: datetime) -> int:
        """自動公開がうまくいかなかったら、その場でアラートを記録・通知する (T4.3)。

        不確定・照合待ちは queue 全体を止める状態なので、翌朝の日次監視まで黙って
        待たない。同じ問題は fingerprint で 1 行にまとまり、cooldown 中は再通知しない
        (C8 と同じ OperationsAlertService)。成績ではアラートを出さない。
        """

        from app.operations.threads_health import (
            build_autopublish_failure_draft,
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
            # T6.4: コンテナ作成・公開の失敗は「事前確認の失敗」ではない。人に分かる説明で出す。
            drafts.append(
                build_autopublish_failure_draft(
                    result.error, growth=getattr(result, "lane", "article") == "account_growth",
                    publication_id=result.publication_id,
                )
            )  # fmt: skip
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
        # manual-post coexistence: manual / unknown の自分の投稿も同じ間隔で観測する。
        account_due: list[int] = []
        with self._factory() as session:
            from app.services.threads_account_post_service import account_posts_ready

            if account_posts_ready(session):
                plan = self._account_post_service(session).due_for_insights(
                    now, by_maturity=by_maturity, stop_after_hours=stop_after)
                account_due = plan["due"]
                intervals.extend(plan["intervals"])
            session.rollback()
        account_refreshed: list[dict] = []
        if self._collect_insights and account_due:
            with self._factory() as session:
                result = self._account_post_service(session).collect_insights(
                    account_due, now=now)
            self._counters["network_calls"] += result["network_calls"]
            account_refreshed = result["details"]
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
                    "account_posts_due": account_due,
                    "account_posts_refreshed": account_refreshed,
                    "network_calls": len([d for d in account_refreshed
                                          if d.get("result") != "unchanged"]),
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
                        "category": d.get("category"),
                        "reason": d.get("reason"),
                    }
                    for d in refreshed
                ],
                "network_calls": calls,
                "account_posts_due": account_due,
                "account_posts_refreshed": account_refreshed,
                "threads_writes": 0,
            },
            # T6.5: 新しい観測を取り込んだら、成績の参考の作り直しを前倒しする
            # (最短間隔より早めない。5 分ごとの heartbeat では作り直さない)。
            wake=(
                {SUBSYSTEM_PERFORMANCE_FEEDBACK: self._feedback_wake_at(now)}
                if any(d.get("result") == "imported" for d in refreshed)
                else {}
            ),
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
    #: status を取る時点。実行の前か後かで、dry run の意味が変わる。
    PHASE_SNAPSHOT = "snapshot"
    PHASE_PRE_RUN = "pre_run"
    PHASE_POST_RUN = "post_run"

    def run_kind(self) -> str:
        """このコマンドの種類。**何をしうるか** で決まる (何をしたかは execution を見る)。

        - ``plan``: 外に一切作用しない
        - ``observe``: 読むだけの外部呼び出し (指標・決定の取り込み)
        - ``operate``: 承認依頼メールを送りうる
        - ``execute``: Threads に公開しうる (フラグとポリシーの両方がそろったとき)
        """

        caps = self.capabilities
        if caps["publish"]:
            return "execute"
        if caps["send_approval_digests"]:
            return "operate"
        if caps["collect_insights"] or caps["sync_approvals"]:
            return "observe"
        return "plan"

    def execution(self) -> dict:
        """この worker が **実際に行ったこと** (固定値ではなく数えた値)。"""

        return {
            "publications": self._counters["publications"],
            "threads_writes": self._counters["threads_writes"],
            "network_calls": self._counters["network_calls"],
            "approval_emails": self._counters["approval_emails"],
        }

    def status(
        self,
        *,
        now: datetime,
        schedule: WorkerSchedule | None = None,
        phase: str = PHASE_SNAPSHOT,
    ) -> dict:
        """CLI が出す現在の状態。**読むだけ。**

        ``phase`` が ``post_run`` のとき、ここに含まれる評価と dry run は **このコマンドの
        実行後の状態から見た「次のサイクル」** の話である (ロックは既に解放済み)。
        """

        now = ensure_aware(now)
        with self._factory() as session:
            queue = self._queue(session)
            config = queue.config_status()
            # ブロッカーは「このコマンドが公開しうるか」で評価する。ポリシーとフラグが
            # そろっているのに automatic_publication_disabled を出さない。
            evaluation = queue.evaluate(now=now, publication_enabled=self.capabilities["publish"])
            growth_lane = queue.evaluate_growth(
                now=now, publication_enabled=self.capabilities["publish"]
            )
            latest = queue.latest_publication()
            basis = queue.latest_gap_basis()
            counts = queue.counts()
            today = queue.published_today(now)
            growth_today = queue.growth_published_today(now)
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
                    # 間隔の起点になる実際の公開時刻 (Threads の投稿時刻が優先)。
                    "actual_published_at": basis.at.isoformat() if basis else None,
                    "actual_published_local": (
                        to_local(basis.at, self._tz).isoformat() if basis else None
                    ),
                    "gap_basis_source": basis.source if basis else None,
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
            last_published_at=basis.at if basis else None,
            policy=self._policy,
            tz=self._tz,
        )
        return {
            "now": now.isoformat(),
            "now_local": to_local(now, self._tz).isoformat(),
            "timezone": self._tz.key,
            "policy_version": self._policy.policy_version,
            "worker_mode": MODE_PLAN,
            "run_kind": self.run_kind(),
            "phase": phase,
            "execution": self.execution(),
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
            # 3〜5 本の目安は記事の投稿だけで数える。Growth Post は足し分 (T6.3.3)。
            "daily_activity": daily_activity(today, self._policy),
            "posts_today": {
                "article_posts_today": today,
                "growth_posts_today": growth_today,
                "total_posts_today": today + growth_today,
            },
            "proposals": counts,
            "queue": evaluation.as_dict(self._tz),
            # T6.3.3a: Growth Post の足し分の枠 (記事の間隔を使わない)。
            "growth_lane": growth_lane.as_dict(self._tz),
            "hard_blockers": list(evaluation.blockers),
            "problems": list(evaluation.problems),
            "subsystems": [self._subsystem_view(s) for s in schedule.states()] if schedule else [],
            "capabilities": self.capabilities,
            "publication_dry_run": self._dry_run(now, phase=phase),
            "side_effects": {
                "threads_writes": self._counters["threads_writes"],
                "network_calls": self._counters["network_calls"],
                "approval_emails": self._counters["approval_emails"],
                "wordpress_writes": 0,
                "go_probes": 0,
                "scheduler_changes": 0,
            },
        }

    def _dry_run(self, now: datetime, *, phase: str = PHASE_SNAPSHOT) -> dict:
        """自動公開が有効なら何が起きるか。**外にも DB にも触れない。**"""

        with self._factory() as session:
            view = self._auto_publisher(session).dry_run(now=now).as_dict()
        view["phase"] = phase
        if phase == self.PHASE_POST_RUN:
            view["label"] = (
                "next-cycle preview after this run (the worker lock was released when the "
                "run ended; the next cycle re-acquires it)"
            )
        elif phase == self.PHASE_PRE_RUN:
            view["label"] = "pre-execution plan (what this run is about to evaluate)"
        else:
            view["label"] = "preview (nothing is executed)"
        return view

    def preview(self, *, now: datetime) -> dict:
        """実行の **前** の計画 (ロックを取った後、サイクルを回す前に呼ぶ)。"""

        return self._dry_run(ensure_aware(now), phase=self.PHASE_PRE_RUN)

    def _subsystem_view(self, state) -> dict:
        view = state.as_dict()
        view["next_run_local"] = (
            to_local(state.next_run_at, self._tz).isoformat() if state.next_run_at else None
        )
        return view


__all__ = ["WORKER_LOCK_NAME", "ThreadsWorkerLock", "ThreadsWorkerService", "default_owner_label"]
