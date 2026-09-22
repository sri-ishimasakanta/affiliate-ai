"""OperationsMonitoringService -- 監視の評価とアラート記録 (C8)。

:mod:`app.operations.monitoring` の純粋なルールに、DB から集めた事実を渡すだけ。
判定ロジックはここに書かない。

もっとも大事な振る舞い: **GA4 がまだ一度も日次行を返していない現在の状態を
「壊れている」と通知しない**。一度でも届いた実績がある場合にのみ、停止を
アラートにする。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    OPS_SUCCEEDED,
    AffiliateLinkTarget,
    ArticleLinkSubstitutionMapping,
    OperationsRun,
    RevenueOptimizationCandidate,
    RevenueOptimizationRun,
    SeoImprovementCandidate,
    SeoImprovementRun,
)
from app.operations.monitoring import (
    compare_candidates,
    evaluate_article_health,
    evaluate_automation_health,
    evaluate_candidate_changes,
    evaluate_import_failures,
    evaluate_monetization_regression,
)
from app.operations.notifications import build_notifiers
from app.operations.policy import OperationsPolicy, get_policy
from app.operations.source_health import (
    evaluate_recent_activity,
    evaluate_source_refresh,
)
from app.services.operations_alert_service import OperationsAlertService


class OperationsMonitoringService:
    def __init__(
        self,
        session: Session,
        *,
        settings,
        policy: OperationsPolicy | None = None,
        notifiers=None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._policy = policy or get_policy()
        self._notifiers = notifiers

    def evaluate(self, *, outcome, now: datetime | None = None, today: date | None = None) -> dict:
        now = now or datetime.now(UTC)
        today = today or now.date()
        drafts = []

        step_results = [s.as_dict() for s in outcome.steps]
        drafts += evaluate_import_failures(step_results=step_results, policy=self._policy)

        # C8.5: ソースの鮮度は「取り込みが動いているか」で判定する。
        # 「最新の行が古いか」は活動の話であり、低トラフィックでは正常。
        freshness = self._source_freshness()
        for value in freshness.values():
            draft = evaluate_source_refresh(
                freshness=value, today=today, now=now, policy=self._policy
            )
            if draft is not None:
                drafts.append(draft)
            draft = evaluate_recent_activity(freshness=value, today=today, policy=self._policy)
            if draft is not None:
                drafts.append(draft)

        indexability = outcome.step("check_indexability")
        if indexability is not None and indexability.status == OPS_SUCCEEDED:
            drafts += evaluate_article_health(
                rows=indexability.result.get("articles") or [], policy=self._policy
            )

        drafts += evaluate_monetization_regression(
            previous=self._previous_monetization_state(),
            current=self._current_monetization_state(),
            policy=self._policy,
        )

        changes = []
        for engine, previous, current in (
            ("seo", *self._candidate_snapshots("seo")),
            ("revenue", *self._candidate_snapshots("revenue")),
        ):
            changes += compare_candidates(engine=engine, previous=previous, current=current)
        drafts += evaluate_candidate_changes(changes=changes, policy=self._policy)

        drafts += evaluate_automation_health(
            run_status=outcome.status,
            consecutive_failures=self._consecutive_failures(exclude_run_id=outcome.run_id),
            lock_conflict=outcome.lock_conflict,
            blocking_owner_run_id=outcome.blocking_owner_run_id,
            policy=self._policy,
        )

        notifiers = (
            self._notifiers if self._notifiers is not None else build_notifiers(self._settings)
        )
        alert_outcome = OperationsAlertService(
            self._session, policy=self._policy, notifiers=notifiers
        ).record_and_notify(drafts, operations_run_id=outcome.run_id, now=now)

        return {
            "alerts_evaluated": len(drafts),
            "alerts_recorded": alert_outcome.recorded,
            "new_alerts": alert_outcome.new_alerts,
            "repeated_alerts": alert_outcome.repeated_alerts,
            "notified": alert_outcome.notified,
            "suppressed_by_severity": alert_outcome.suppressed_by_severity,
            "suppressed_by_cooldown": alert_outcome.suppressed_by_cooldown,
            "notification_failures": alert_outcome.notification_failures,
            "candidate_changes": len(changes),
            "source_freshness": {k: v.as_dict() for k, v in freshness.items()},
            "notifier_names": [getattr(n, "name", "?") for n in notifiers],
        }

    # -- facts ----------------------------------------------------------------
    def _source_freshness(self):
        from app.services.operations_source_health_service import (
            collect_source_freshness,
        )

        return collect_source_freshness(self._session)

    def _current_monetization_state(self) -> dict[int, dict]:
        state: dict[int, dict] = {}
        for target in self._session.scalars(select(AffiliateLinkTarget)).all():
            bucket = state.setdefault(
                target.article_id, {"active_target_count": 0, "active_mapping_count": 0}
            )
            if target.status == "active":
                bucket["active_target_count"] += 1
        for mapping in self._session.scalars(select(ArticleLinkSubstitutionMapping)).all():
            bucket = state.setdefault(
                mapping.article_id, {"active_target_count": 0, "active_mapping_count": 0}
            )
            if mapping.status == "active":
                bucket["active_mapping_count"] += 1
        return state

    def _previous_monetization_state(self) -> dict[int, dict]:
        """直近の C7 run が記録した記事ごとの収益化構造 (無ければ空)。

        候補は「欠けている」ことしか記録しないので、正常な状態は C7 run の
        ``summary_json["monetization_state"]`` から読む。初回 run には無いので、
        その場合は比較せず (= 退行アラートも出さない)。
        """

        run = self._session.scalars(
            select(RevenueOptimizationRun).order_by(RevenueOptimizationRun.id.desc()).limit(1)
        ).first()
        if run is None:
            return {}
        summary = run.summary_json or {}
        state: dict[int, dict] = {}
        for article_id, bucket in (summary.get("monetization_state") or {}).items():
            try:
                state[int(article_id)] = bucket
            except (TypeError, ValueError):
                continue
        return state

    def _candidate_snapshots(self, engine: str) -> tuple[list[dict], list[dict]]:
        """直近 2 つの評価 run の候補を (previous, current) で返す。"""

        if engine == "seo":
            run_model, candidate_model, fk = (
                SeoImprovementRun,
                SeoImprovementCandidate,
                SeoImprovementCandidate.seo_improvement_run_id,
            )
        else:
            run_model, candidate_model, fk = (
                RevenueOptimizationRun,
                RevenueOptimizationCandidate,
                RevenueOptimizationCandidate.revenue_optimization_run_id,
            )
        runs = list(
            self._session.scalars(select(run_model).order_by(run_model.id.desc()).limit(2)).all()
        )
        if not runs:
            return [], []
        current = self._candidates_of(candidate_model, fk, runs[0].id)
        previous = self._candidates_of(candidate_model, fk, runs[1].id) if len(runs) > 1 else []
        return previous, current

    def _candidates_of(self, model, fk, run_id: int) -> list[dict]:
        return [
            {
                "dedupe_key": row.dedupe_key,
                "candidate_type": row.candidate_type,
                "priority": row.priority,
                "article_id": row.article_id,
            }
            for row in self._session.scalars(select(model).where(fk == run_id)).all()
        ]

    def _consecutive_failures(self, *, exclude_run_id: int | None) -> int:
        runs = self._session.scalars(
            select(OperationsRun).order_by(OperationsRun.id.desc()).limit(10)
        ).all()
        count = 0
        for run in runs:
            if run.id == exclude_run_id:
                continue
            if run.status == OPS_SUCCEEDED:
                break
            count += 1
        return count
