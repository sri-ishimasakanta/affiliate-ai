"""ThreadsAutoPublisher -- 承認済み queue から、1 回に 1 本だけ公開する (T4.3)。

**既定では何もしない。** 公開まで進むのは、次の条件が **すべて** そろったときだけ:

1. ポリシー ``automatic_publication.enabled`` が True (コミット済みの値は False)
2. worker が ``--auto-publish`` で起動されている
3. worker のロックを持っている (2 つのプロセスが同時に出さない)
4. Threads の設定が ``ready``
5. queue の評価に **ブロッカーが 1 つも無い** (公開窓・間隔・不確定な公開・候補なし…)
6. 選ばれた候補が T3 の ``plan()`` を通る (承認・stale・中身・二重投稿・間隔)
7. 読むだけの事前確認 (``GET /me``) が通る — API が止められていれば、コンテナを
   作る前に止まる (2026-09-25 の "API access blocked." のような状態)

manual-post coexistence (2026-10-02): 外への書き込みに進む **直前に**、自アカウントの投稿の一覧を
必ず読み直す (``account_posts.pre_publication_refresh``)。読み直した状態で queue と T3 の判定を
やり直し、人が直前に出した投稿があれば待つ (間隔・重複)。一覧を読めなければ公開しない
(提案は承認済みのまま、次の評価へ回す)。

公開は T3 の ``publish(trigger="automatic")`` を通る。自動の公開は **間隔を上書き
できない** (T3 がそれを拒否する)。応答を取りこぼせば T3 が ``uncertain`` にし、
照合が済むまで queue 全体が止まる。

1 回の呼び出しで公開を試みるのは最大 1 件。資格のある候補が 5 件あっても 1 件。
出した後は状態が変わる (間隔が始まる) ので、次は改めて評価する。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware
from app.models import PUB_TRIGGER_AUTOMATIC
from app.services.threads_publication_service import ThreadsPublicationService
from app.services.threads_queue_service import ThreadsQueueService
from app.social.threads.policy import ThreadsMeasurementPolicy, ThreadsOperationsPolicy

#: T6.3.3a: 承認からこの長さの中の Growth の公開は ``post_approval_heartbeat`` と記録する。
_POST_APPROVAL_WINDOW = timedelta(minutes=15)

GATE_POLICY = "policy_enabled"
GATE_FLAG = "worker_flag"
GATE_LOCK = "worker_lock"
GATE_CONFIG = "threads_ready"
GATES = (GATE_POLICY, GATE_FLAG, GATE_LOCK, GATE_CONFIG)


@dataclass
class AutoPublishOutcome:
    now: datetime
    attempted: bool = False
    published: bool = False
    outcome: str = "not_attempted"
    gates: dict[str, bool] = field(default_factory=dict)
    blocked_reasons: list[str] = field(default_factory=list)
    proposal_id: int | None = None
    publication_id: int | None = None
    media_id: str | None = None
    preflight: dict | None = None
    next_evaluation_at: datetime | None = None
    #: Threads への **書き込み呼び出し** の数 (コンテナ作成・公開)。結果ではなく呼んだ回数。
    threads_writes: int = 0
    #: この試みで行った HTTP 呼び出しの総数 (事前確認・作成・公開・読み戻し)。
    network_calls: int = 0
    #: T3 が返した失敗 (分類と redact 済みの理由)。token は入らない。
    error: dict | None = None
    #: 公開を試みた **後** の状態で評価し直したときのブロッカー (次の評価の話)。
    next_blockers: list[str] | None = None
    notes: list[str] = field(default_factory=list)
    #: T6.3.3a: どの枠か (``article`` / ``account_growth``) と、Growth の枠の事実。
    lane: str = "article"
    #: 公開の直前に読み直した自アカウントの投稿の一覧の要約 (manual-post coexistence)。
    account_post_refresh: dict | None = None
    growth_trigger: str | None = None
    growth_date_jst: str | None = None
    paired_article_publication_id: int | None = None

    def as_dict(self) -> dict:
        return {
            "now": self.now.isoformat(),
            "attempted": self.attempted,
            "published": self.published,
            "outcome": self.outcome,
            "gates": dict(self.gates),
            "blocked_reasons": list(self.blocked_reasons),
            "proposal_id": self.proposal_id,
            "publication_id": self.publication_id,
            "media_id": self.media_id,
            "preflight": self.preflight,
            "next_evaluation_at": (
                self.next_evaluation_at.isoformat() if self.next_evaluation_at else None
            ),
            "threads_writes": self.threads_writes,
            "network_calls": self.network_calls,
            "error": self.error,
            "next_blockers": list(self.next_blockers) if self.next_blockers is not None else None,
            "notes": list(self.notes),
            "lane": self.lane,
            "account_post_refresh": self.account_post_refresh,
            "growth_trigger": self.growth_trigger,
            "growth_date_jst": self.growth_date_jst,
            "paired_article_publication_id": self.paired_article_publication_id,
        }


class ThreadsAutoPublisher:
    def __init__(
        self,
        session: Session,
        *,
        settings,
        threads_service,
        policy: ThreadsOperationsPolicy,
        measurement_policy: ThreadsMeasurementPolicy,
        timezone: ZoneInfo,
        flag_enabled: bool,
        lock_held: bool,
        sleep=time.sleep,
    ) -> None:
        self._session = session
        self._settings = settings
        self._threads = threads_service
        self._policy = policy
        self._flag = flag_enabled
        self._lock_held = lock_held
        self._queue = ThreadsQueueService(
            session,
            settings=settings,
            threads_service=threads_service,
            policy=policy,
            measurement_policy=measurement_policy,
            timezone=timezone,
        )
        self._publications = ThreadsPublicationService(
            session,
            settings=settings,
            threads_service=threads_service,
            sleep=sleep,
            gap_minutes=policy.soft_min_gap_minutes,
        )

    # -- gates -------------------------------------------------------------------
    def gates(self) -> dict[str, bool]:
        return {
            GATE_POLICY: self._policy.automatic_publication_enabled,
            GATE_FLAG: bool(self._flag),
            GATE_LOCK: bool(self._lock_held),
            GATE_CONFIG: self._queue.config_status().state == "ready",
        }

    @staticmethod
    def _gate_reasons(gates: dict[str, bool]) -> list[str]:
        messages = {
            GATE_POLICY: "automatic_publication.enabled is false in the policy",
            GATE_FLAG: "the worker was not started with --auto-publish",
            GATE_LOCK: "the worker does not hold the worker lock",
            GATE_CONFIG: "Threads is not in the ready state",
        }
        return [messages[name] for name in GATES if not gates.get(name)]

    # -- dry run -----------------------------------------------------------------
    def dry_run(self, *, now: datetime | None = None) -> AutoPublishOutcome:
        """もし全部のゲートが開いていたら何を出すか。**外にも DB にも一切触れない。**

        事前確認 (ネットワーク) はしない。ゲートとブロッカーと T3 の判定だけを示す。
        """

        now = ensure_aware(now or datetime.now(UTC))
        outcome = AutoPublishOutcome(now=now, outcome="dry_run")
        outcome.gates = self.gates()
        outcome.blocked_reasons.extend(self._gate_reasons(outcome.gates))
        # ゲートを無視して「もし有効なら」を評価する (公開の可否の説明のため)。
        evaluation = self._queue.evaluate(now=now, publication_enabled=True)
        outcome.next_evaluation_at = evaluation.next_evaluation_at
        outcome.blocked_reasons.extend(f"queue: {b}" for b in evaluation.blockers)
        candidate = evaluation.next_candidate
        if candidate is not None:
            outcome.proposal_id = candidate.proposal_id
            plan = self._publications.plan(
                proposal_id=candidate.proposal_id, now=now, trigger=PUB_TRIGGER_AUTOMATIC
            )
            outcome.blocked_reasons.extend(f"t3: {r}" for r in plan.blocked_reasons)
            outcome.notes.append(
                "would publish proposal "
                f"{candidate.proposal_id} ({candidate.angle}) through the T3 path"
                if not outcome.blocked_reasons
                else f"next candidate is proposal {candidate.proposal_id}"
            )
        outcome.notes.append("dry run: no network call, no database write")
        return outcome

    # -- T6.3.3a: Growth Post の足し分の枠 -----------------------------------------------
    def publish_growth_one(
        self, *, now: datetime | None = None, companion_publication_id: int | None = None
    ) -> AutoPublishOutcome:
        """承認済みの今日の Growth Post を 1 本だけ出す。**記事の間隔・1 回 1 本の枠を使わない。**

        記事の投稿を出した直後 (``article_companion``)、承認の取り込みの直後
        (``post_approval_heartbeat``)、ふだんの評価 (``normal_heartbeat``) のどれでも同じ条件。
        出した後も記事の間隔の起点にならない。失敗しても記事の queue は止めない。
        """

        now = ensure_aware(now or datetime.now(UTC))
        outcome = AutoPublishOutcome(now=now, lane="account_growth")
        outcome.paired_article_publication_id = companion_publication_id
        outcome.gates = self.gates()
        reasons = self._gate_reasons(outcome.gates)
        if reasons:
            outcome.outcome = "gated"
            outcome.blocked_reasons.extend(reasons)
            return outcome
        evaluation = self._queue.evaluate_growth(now=now, publication_enabled=True)
        outcome.growth_date_jst = evaluation.date_jst
        outcome.next_evaluation_at = evaluation.next_evaluation_at
        candidate = evaluation.candidate
        if candidate is not None:
            outcome.proposal_id = candidate.proposal_id
        if not evaluation.would_publish_now:
            outcome.outcome = "blocked"
            outcome.blocked_reasons.extend(evaluation.blockers)
            return outcome
        approved_at = candidate.approved_at
        outcome.growth_trigger = (
            "article_companion"
            if companion_publication_id is not None
            else "post_approval_heartbeat"
            if approved_at is not None and now - approved_at <= _POST_APPROVAL_WINDOW
            else "normal_heartbeat"
        )
        plan = self._publications.plan(
            proposal_id=candidate.proposal_id, now=now, trigger=PUB_TRIGGER_AUTOMATIC
        )
        if not plan.ok:
            outcome.outcome = "blocked"
            outcome.blocked_reasons.extend(plan.blocked_reasons)
            return outcome
        if not self._refresh_account_posts(now, outcome):
            return outcome
        # 読み直した状態で評価し直す (直前の manual 投稿・重なりで止まることがある)。
        evaluation = self._queue.evaluate_growth(now=now, publication_enabled=True)
        outcome.next_evaluation_at = evaluation.next_evaluation_at
        if (not evaluation.would_publish_now or evaluation.candidate is None
                or evaluation.candidate.proposal_id != candidate.proposal_id):
            outcome.outcome = "blocked"
            outcome.blocked_reasons.extend(evaluation.blockers or (
                "the candidate changed after the pre-publication refresh",))
            return outcome
        plan = self._publications.plan(
            proposal_id=candidate.proposal_id, now=now, trigger=PUB_TRIGGER_AUTOMATIC
        )
        if not plan.ok:
            outcome.outcome = "blocked"
            outcome.blocked_reasons.extend(plan.blocked_reasons)
            return outcome
        audit = {
            "lane": "account_growth",
            "growth_trigger": outcome.growth_trigger,
            "growth_date_jst": evaluation.date_jst,
            "paired_article_publication_id": companion_publication_id,
            "article_gap_applies": False,
        }
        self._execute(candidate.proposal_id, now, outcome, audit=audit)
        after = self._queue.evaluate_growth(now=now, publication_enabled=True)
        outcome.next_blockers = list(after.blockers)
        outcome.next_evaluation_at = after.next_evaluation_at
        return outcome

    # -- manual-post coexistence: 公開の直前の読み直し -------------------------------------
    def _refresh_account_posts(self, now: datetime, outcome: AutoPublishOutcome) -> bool:
        """自アカウントの投稿の一覧を読み直す。読めなければ公開しない (False)。"""

        if not self._policy.account_post_pre_publication_refresh:
            return True
        from app.services.threads_account_post_service import (
            AccountPostDiscoveryError,
            ThreadsAccountPostService,
        )

        service = ThreadsAccountPostService(
            self._session, threads_service=self._threads,
            listing_limit=self._policy.account_post_listing_limit)  # fmt: skip
        outcome.network_calls += 1  # 一覧の読み (読むだけ)
        try:
            summary = service.refresh(now=now, source="pre_publication")
        except AccountPostDiscoveryError as exc:
            self._session.rollback()
            outcome.outcome = "account_post_refresh_failed"
            outcome.account_post_refresh = {"ok": False, "category": exc.category,
                                            "reason": exc.reason}
            outcome.blocked_reasons.append(
                f"pre-publication account post refresh failed ({exc.category}); the "
                "publication is deferred to the next evaluation (fail closed)")
            outcome.next_evaluation_at = now + timedelta(
                minutes=self._policy.idle_publication_reevaluation_minutes)
            return False
        outcome.account_post_refresh = {
            "ok": True, "listed": summary["listed"],
            "discovered": summary["discovered"], "origin_changed": summary["origin_changed"],
            "superseded_proposals": summary["superseded_proposals"]}  # fmt: skip
        return True

    def _execute(self, proposal_id: int, now: datetime, outcome: AutoPublishOutcome, *,
                 audit=None) -> None:  # fmt: skip
        """事前確認 (読むだけ) → T3 の公開。数えるのは「呼んだ回数」。"""

        if self._policy.automatic_publication_preflight:
            outcome.network_calls += 1  # GET /me (読むだけ)
            status = self._threads.check_connection()
            outcome.preflight = {"reachable": status.reachable, "error": status.error}
            if not status.reachable:
                outcome.outcome = "preflight_failed"
                category = (status.error or {}).get("category", "unknown")
                outcome.blocked_reasons.append(f"read-only preflight failed ({category})")
                return
        outcome.attempted = True
        result = self._publications.publish(
            proposal_id=proposal_id, execute=True, now=now, trigger=PUB_TRIGGER_AUTOMATIC,
            audit=audit,
        )  # fmt: skip
        outcome.outcome = result.outcome
        outcome.publication_id = result.publication_id
        outcome.media_id = result.media_id
        outcome.blocked_reasons.extend(result.blocked_reasons)
        create_called = bool(result.executed)
        publish_called = bool(result.creation_id)
        readback_called = bool(result.media_id)
        outcome.threads_writes = int(create_called) + int(publish_called)
        outcome.network_calls += int(create_called) + int(publish_called) + int(readback_called)
        outcome.published = result.outcome == "published"
        outcome.error = result.error
        if result.outcome == "uncertain":
            outcome.notes.append(
                "the growth publish response was lost; growth posts stay blocked until "
                "publish_threads_post.py --reconcile confirms what happened (articles continue)"
            )

    # -- publish ---------------------------------------------------------------
    def publish_one(self, *, now: datetime | None = None) -> AutoPublishOutcome:
        """条件がすべてそろっていれば、**1 件だけ** 公開する。"""

        now = ensure_aware(now or datetime.now(UTC))
        outcome = AutoPublishOutcome(now=now)
        outcome.gates = self.gates()
        reasons = self._gate_reasons(outcome.gates)
        if reasons:
            outcome.outcome = "gated"
            outcome.blocked_reasons.extend(reasons)
            return outcome

        # ロックの中で、いまの状態から評価し直す (古い評価で出さない)。
        evaluation = self._queue.evaluate(now=now, publication_enabled=True)
        outcome.next_evaluation_at = evaluation.next_evaluation_at
        if not evaluation.would_publish_now:
            outcome.outcome = "blocked"
            outcome.blocked_reasons.extend(evaluation.blockers)
            return outcome
        candidate = evaluation.next_candidate
        outcome.proposal_id = candidate.proposal_id

        plan = self._publications.plan(
            proposal_id=candidate.proposal_id, now=now, trigger=PUB_TRIGGER_AUTOMATIC
        )
        if not plan.ok:
            outcome.outcome = "blocked"
            outcome.blocked_reasons.extend(plan.blocked_reasons)
            return outcome

        if not self._refresh_account_posts(now, outcome):
            return outcome
        # 読み直した状態で評価し直す (直前の manual 投稿・重なりで止まることがある)。
        evaluation = self._queue.evaluate(now=now, publication_enabled=True)
        outcome.next_evaluation_at = evaluation.next_evaluation_at
        if (not evaluation.would_publish_now or evaluation.next_candidate is None
                or evaluation.next_candidate.proposal_id != candidate.proposal_id):
            outcome.outcome = "blocked"
            outcome.blocked_reasons.extend(evaluation.blockers or (
                "the candidate changed after the pre-publication refresh",))
            return outcome
        plan = self._publications.plan(
            proposal_id=candidate.proposal_id, now=now, trigger=PUB_TRIGGER_AUTOMATIC
        )
        if not plan.ok:
            outcome.outcome = "blocked"
            outcome.blocked_reasons.extend(plan.blocked_reasons)
            return outcome

        if self._policy.automatic_publication_preflight:
            outcome.network_calls += 1  # GET /me (読むだけ)
            status = self._threads.check_connection()
            outcome.preflight = {
                "reachable": status.reachable,
                "error": status.error,
            }
            if not status.reachable:
                # API が止められている・token が切れている等。コンテナを作る前に止まる。
                outcome.outcome = "preflight_failed"
                category = (status.error or {}).get("category", "unknown")
                outcome.blocked_reasons.append(f"read-only preflight failed ({category})")
                return outcome

        outcome.attempted = True
        result = self._publications.publish(
            proposal_id=candidate.proposal_id,
            execute=True,
            now=now,
            trigger=PUB_TRIGGER_AUTOMATIC,
        )
        outcome.outcome = result.outcome
        outcome.publication_id = result.publication_id
        outcome.media_id = result.media_id
        outcome.blocked_reasons.extend(result.blocked_reasons)
        # 数えるのは「呼んだ回数」。結果の有無で数えると、応答を取りこぼした公開
        # (呼んだが media id が返らなかった) を書き込み 0 回と誤って報告してしまう。
        #   作成を呼んだ   = T3 が行を確保して実行に進んだ (executed)
        #   公開を呼んだ   = コンテナができた (creation_id)
        #   読み戻しを呼んだ = media id が返った (T3 はその後に必ず読み戻す)
        create_called = bool(result.executed)
        publish_called = bool(result.creation_id)
        readback_called = bool(result.media_id)
        outcome.threads_writes = int(create_called) + int(publish_called)
        outcome.network_calls += int(create_called) + int(publish_called) + int(readback_called)
        outcome.published = result.outcome == "published"
        outcome.error = result.error
        if result.outcome == "uncertain":
            outcome.notes.append(
                "the publish response was lost; the queue stays blocked until "
                "publish_threads_post.py --reconcile confirms what happened"
            )
        # 出した (または試みた) 後の状態で、次の評価時刻を計算し直す。
        after = self._queue.evaluate(now=now, publication_enabled=True)
        outcome.next_evaluation_at = after.next_evaluation_at
        outcome.next_blockers = list(after.blockers)
        return outcome


__all__ = ["GATES", "AutoPublishOutcome", "ThreadsAutoPublisher"]
