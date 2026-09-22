"""OperationsRunner -- 定期実行のオーケストレーション (C8)。

**業務ロジックはここに書かない**。既存の取り込み service / 候補エンジンを
依存順に呼び、結果を構造化して記録するだけ。

この runner が絶対にしないこと:

- WordPress への書き込み・記事本文の変更
- アフィリエイト URL の作成、``/go/<token>`` への HTTP リクエスト
- SEO / 収益化の「自動修正」

できるのは「測る・評価する・記録する・知らせる」までで、意思決定と変更は人に残す。

失敗の意味づけ (S):

- 取り込み 1 つの失敗で、成功した他の取り込みを巻き戻さない (外部データは有効)。
- 依存元が失敗したステップは ``skipped`` + 理由を記録する (失敗の握り潰しをしない)。
- 通知の失敗は運用 run を ``partial`` にするが、取り込んだデータは有効なまま。
- 自動リトライは既定で無効 (ポリシーで明示的に有効化する)。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.article.fact_freshness import to_storage_utc
from app.models import (
    OPS_FAILED,
    OPS_PARTIAL,
    OPS_RUNNING,
    OPS_SKIPPED,
    OPS_SUCCEEDED,
    Ga4ImportRun,
    Ga4PageDaily,
    OperationsRun,
    OperationsStepRun,
    SearchConsolePageDaily,
)
from app.operations.lock import DEFAULT_LOCK_NAME, OperationsLockService
from app.operations.policy import OperationsPolicy, get_policy

# -- step names ----------------------------------------------------------------
STEP_SEARCH_CONSOLE = "import_search_console"
STEP_GA4 = "import_ga4"
STEP_AFFILIATE_CLICKS = "import_affiliate_clicks"
STEP_MAKE_COMMISSIONS = "import_make_commissions"
STEP_INDEXABILITY = "check_indexability"
STEP_SEO_CANDIDATES = "evaluate_seo_candidates"
STEP_REVENUE_CANDIDATES = "evaluate_revenue_candidates"
STEP_MONITORING = "evaluate_monitoring"

DAILY_STEPS = (
    STEP_SEARCH_CONSOLE,
    STEP_GA4,
    STEP_AFFILIATE_CLICKS,
    STEP_MAKE_COMMISSIONS,
    STEP_INDEXABILITY,
    STEP_SEO_CANDIDATES,
    STEP_REVENUE_CANDIDATES,
    STEP_MONITORING,
)
WEEKLY_STEPS = DAILY_STEPS

#: どのステップがどのステップの成功に依存するか (失敗したら skip する)。
STEP_DEPENDENCIES = {
    STEP_SEO_CANDIDATES: (STEP_SEARCH_CONSOLE,),
    STEP_REVENUE_CANDIDATES: (STEP_AFFILIATE_CLICKS,),
}


@dataclass
class StepOutcome:
    step_name: str
    status: str
    source_run_id: int | None = None
    rows_received: int | None = None
    rows_changed: int | None = None
    error_category: str | None = None
    error_message: str | None = None
    skip_reason: str | None = None
    attempt_count: int = 1
    result: dict = field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def as_dict(self) -> dict:
        return {
            "step_name": self.step_name,
            "status": self.status,
            "source_run_id": self.source_run_id,
            "rows_received": self.rows_received,
            "rows_changed": self.rows_changed,
            "error_category": self.error_category,
            "error_message": self.error_message,
            "skip_reason": self.skip_reason,
            "attempt_count": self.attempt_count,
            "result": self.result,
        }


@dataclass
class OperationsOutcome:
    profile: str
    policy_version: str
    effective_date: date
    timezone_name: str
    status: str
    run_id: int | None = None
    steps: list[StepOutcome] = field(default_factory=list)
    lock_conflict: bool = False
    blocking_owner_run_id: int | None = None
    reclaimed_stale_lock: bool = False
    plan_only: bool = False
    notes: list[str] = field(default_factory=list)

    def step(self, name: str) -> StepOutcome | None:
        return next((s for s in self.steps if s.step_name == name), None)

    def as_dict(self) -> dict:
        return {
            "profile": self.profile,
            "policy_version": self.policy_version,
            "effective_date": self.effective_date.isoformat(),
            "timezone": self.timezone_name,
            "status": self.status,
            "run_id": self.run_id,
            "plan_only": self.plan_only,
            "lock_conflict": self.lock_conflict,
            "blocking_owner_run_id": self.blocking_owner_run_id,
            "reclaimed_stale_lock": self.reclaimed_stale_lock,
            "steps": [s.as_dict() for s in self.steps],
            "notes": self.notes,
        }


class OperationsRunner:
    """既存 service を依存順に呼ぶだけのオーケストレータ。

    ``step_overrides`` はテスト用の注入口 (実ネットワークに触れずに経路を検証する)。
    """

    def __init__(
        self,
        session_factory,
        *,
        settings,
        policy: OperationsPolicy | None = None,
        step_overrides: dict | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._policy = policy or get_policy()
        self._overrides = step_overrides or {}

    # -- public ---------------------------------------------------------------
    def plan(self, *, profile: str, now: datetime | None = None) -> OperationsOutcome:
        """何を実行するかだけを返す (通信も書き込みもしない)。"""

        now = now or datetime.now(UTC)
        outcome = OperationsOutcome(
            profile=profile,
            policy_version=self._policy.policy_version,
            effective_date=self._effective_date(now),
            timezone_name=self._policy.timezone_name,
            status="planned",
            plan_only=True,
        )
        for name in self._steps_for(profile):
            outcome.steps.append(
                StepOutcome(step_name=name, status="planned", result=self._step_plan(name, profile))
            )
        outcome.notes.append("plan only: no network call, no database write")
        outcome.notes.append("this pipeline never writes WordPress content and never calls /go/")
        return outcome

    def execute(
        self,
        *,
        profile: str,
        trigger: str = "manual",
        now: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> OperationsOutcome:
        now = now or datetime.now(UTC)
        effective_date = self._effective_date(now)
        outcome = OperationsOutcome(
            profile=profile,
            policy_version=self._policy.policy_version,
            effective_date=effective_date,
            timezone_name=self._policy.timezone_name,
            status=OPS_RUNNING,
        )

        with self._session_factory() as session:
            run = OperationsRun(
                profile=profile,
                policy_version=self._policy.policy_version,
                effective_date=effective_date,
                timezone_name=self._policy.timezone_name,
                trigger=trigger,
                status=OPS_RUNNING,
                git_revision=_git_revision(),
                schema_version=_schema_version(session),
                step_total=len(self._steps_for(profile)),
                idempotency_key=idempotency_key,
                started_at=to_storage_utc(now),
            )
            session.add(run)
            session.commit()
            session.refresh(run)
            outcome.run_id = run.id

            lock = OperationsLockService(session).acquire(
                lock_name=DEFAULT_LOCK_NAME,
                owner_run_id=run.id,
                owner_label=f"{profile}:{effective_date.isoformat()}",
                stale_after_minutes=self._policy.stale_lock_after_minutes,
                now=now,
            )
            if not lock.acquired:
                # 競合は成功ではない -- skip として残し、監視が拾えるようにする。
                outcome.lock_conflict = True
                outcome.blocking_owner_run_id = lock.blocking_owner_run_id
                outcome.status = OPS_SKIPPED
                run.status = OPS_SKIPPED
                run.failure_summary = (
                    f"another operations run ({lock.blocking_owner_run_id}) holds the lock"
                )
                run.finished_at = to_storage_utc(datetime.now(UTC))
                session.commit()
                return outcome
            outcome.reclaimed_stale_lock = lock.reclaimed_stale
            if lock.reclaimed_stale:
                outcome.notes.append("reclaimed a stale pipeline lock from a dead run")

        try:
            self._run_steps(profile, outcome, now=now, effective_date=effective_date)
        finally:
            with self._session_factory() as session:
                OperationsLockService(session).release(
                    lock_name=DEFAULT_LOCK_NAME, owner_run_id=outcome.run_id
                )

        self._finalize(outcome)
        return outcome

    # -- steps ----------------------------------------------------------------
    def _steps_for(self, profile: str) -> tuple[str, ...]:
        return WEEKLY_STEPS if profile == "weekly" else DAILY_STEPS

    def _step_plan(self, name: str, profile: str) -> dict:
        if name == STEP_SEARCH_CONSOLE:
            config = self._policy.import_config("search_console")
            return {"days": config.get("days"), "lag_days": config.get("lag_days")}
        if name == STEP_GA4:
            config = self._policy.import_config("ga4")
            return {"days": config.get("days"), "lag_days": config.get("lag_days")}
        if name == STEP_AFFILIATE_CLICKS:
            return {"page_limit": self._policy.import_config("affiliate_clicks").get("page_limit")}
        if name == STEP_MAKE_COMMISSIONS:
            return {"days": self._policy.import_config("make_commissions").get("days")}
        if name == STEP_INDEXABILITY:
            return {"inspect": self._inspect_enabled(profile)}
        if name in (STEP_SEO_CANDIDATES, STEP_REVENUE_CANDIDATES):
            return {"window_days": self._policy.gate("candidates", "window_days", 30)}
        return {}

    def _inspect_enabled(self, profile: str) -> bool:
        key = "inspect_on_weekly" if profile == "weekly" else "inspect_on_daily"
        return bool(self._policy.gate("indexability", key, profile == "weekly"))

    def _run_steps(
        self, profile: str, outcome: OperationsOutcome, *, now: datetime, effective_date: date
    ) -> None:
        succeeded: set[str] = set()
        for name in self._steps_for(profile):
            missing = [
                dependency
                for dependency in STEP_DEPENDENCIES.get(name, ())
                if dependency not in succeeded
            ]
            if missing:
                step = StepOutcome(
                    step_name=name,
                    status=OPS_SKIPPED,
                    skip_reason=f"required step(s) did not succeed: {', '.join(missing)}",
                )
                outcome.steps.append(step)
                self._persist_step(outcome.run_id, step)
                continue

            started = datetime.now(UTC)
            handler = self._overrides.get(name) or getattr(self, f"_step_{name}")
            try:
                step = handler(
                    profile=profile, now=now, effective_date=effective_date, outcome=outcome
                )
            except Exception as exc:  # noqa: BLE001 - どのステップの失敗も run を壊さない
                step = StepOutcome(
                    step_name=name,
                    status=OPS_FAILED,
                    error_category=type(exc).__name__,
                    # 例外文言は既存 service が secret を含めない契約なのでそのまま。
                    error_message=str(exc)[:500],
                )
            step.step_name = name
            step.started_at = started
            step.finished_at = datetime.now(UTC)
            outcome.steps.append(step)
            self._persist_step(outcome.run_id, step)
            if step.status == OPS_SUCCEEDED:
                succeeded.add(name)

    # -- individual steps (既存 service へ委譲するだけ) ------------------------
    def _step_import_search_console(self, *, now, effective_date, **_kwargs) -> StepOutcome:
        from app.search_console.google_provider import GoogleSearchConsoleProvider
        from app.services.search_console_import_service import SearchConsoleImportService

        config = self._policy.import_config("search_console")
        start, end = _window(effective_date, config)
        with self._session_factory() as session:
            run = SearchConsoleImportService(session).prepare(start_date=start, end_date=end)
            run_id = run.id
        with self._session_factory() as session:
            run = SearchConsoleImportService(session).execute(
                run_id,
                provider=GoogleSearchConsoleProvider(
                    credentials_file=self._settings.search_console_credentials_file
                ),
            )
            return StepOutcome(
                step_name=STEP_SEARCH_CONSOLE,
                status=OPS_SUCCEEDED,
                source_run_id=run.id,
                rows_received=(run.page_rows_received or 0) + (run.query_rows_received or 0),
                rows_changed=(run.page_rows_upserted or 0) + (run.query_rows_upserted or 0),
                result={"start_date": start.isoformat(), "end_date": end.isoformat()},
            )

    def _step_import_ga4(self, *, now, effective_date, **_kwargs) -> StepOutcome:
        from app.analytics.google_provider import GoogleGa4Provider
        from app.services.ga4_import_service import Ga4ImportService

        property_id = (getattr(self._settings, "ga4_property_id", None) or "").strip()
        if not property_id:
            return StepOutcome(
                step_name=STEP_GA4,
                status=OPS_SKIPPED,
                skip_reason="ga4 property is not configured",
            )
        config = self._policy.import_config("ga4")
        start, end = _window(effective_date, config)
        with self._session_factory() as session:
            run = Ga4ImportService(session).prepare(
                start_date=start, end_date=end, property_id=property_id
            )
            run_id = run.id
        with self._session_factory() as session:
            run = Ga4ImportService(session).execute(
                run_id, provider=GoogleGa4Provider(self._settings)
            )
            return StepOutcome(
                step_name=STEP_GA4,
                status=OPS_SUCCEEDED,
                source_run_id=run.id,
                rows_received=run.page_rows_received,
                rows_changed=run.page_rows_upserted,
                result={
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "property_timezone": run.property_timezone,
                    "data_through": (
                        run.data_through_date.isoformat() if run.data_through_date else None
                    ),
                },
            )

    def _step_import_affiliate_clicks(self, *, now, **_kwargs) -> StepOutcome:
        from app.services.affiliate_click_import_service import AffiliateClickImportService

        limit = int(self._policy.import_config("affiliate_clicks").get("page_limit", 1000))
        with self._session_factory() as session:
            result = AffiliateClickImportService(session).import_next_page(
                limit=limit, settings=self._settings
            )
            return StepOutcome(
                step_name=STEP_AFFILIATE_CLICKS,
                status=OPS_SUCCEEDED,
                source_run_id=getattr(result, "run_id", None),
                rows_received=getattr(result, "response_count", None),
                rows_changed=getattr(result, "inserted_count", None),
                result={
                    "has_more": getattr(result, "has_more", None),
                    "unresolved_token_count": getattr(result, "unresolved_token_count", None),
                },
            )

    def _step_import_make_commissions(self, *, now, effective_date, **_kwargs) -> StepOutcome:
        from app.models import AffiliateProgram
        from app.services.affiliate_commission_import_service import (
            AffiliateCommissionImportService,
        )

        days = int(self._policy.import_config("make_commissions").get("days", 30))
        date_from = effective_date - timedelta(days=days - 1)
        with self._session_factory() as session:
            programs = [
                p.id
                for p in session.scalars(
                    select(AffiliateProgram).where(AffiliateProgram.tracking_url.is_not(None))
                ).all()
            ]
        if not programs:
            return StepOutcome(
                step_name=STEP_MAKE_COMMISSIONS,
                status=OPS_SKIPPED,
                skip_reason="no affiliate program has a tracking url configured",
            )
        received = 0
        changed = 0
        last_run_id = None
        for program_id in programs:
            with self._session_factory() as session:
                result = AffiliateCommissionImportService(session).import_commissions(
                    affiliate_program_id=program_id,
                    date_from=date_from,
                    date_to=effective_date,
                    settings=self._settings,
                )
                received += getattr(result, "response_count", 0) or 0
                changed += (getattr(result, "inserted_count", 0) or 0) + (
                    getattr(result, "updated_count", 0) or 0
                )
                last_run_id = getattr(result, "run_id", last_run_id)
        return StepOutcome(
            step_name=STEP_MAKE_COMMISSIONS,
            status=OPS_SUCCEEDED,
            source_run_id=last_run_id,
            rows_received=received,
            rows_changed=changed,
            result={"program_ids": programs, "date_from": date_from.isoformat()},
        )

    def _step_check_indexability(self, *, profile, **_kwargs) -> StepOutcome:
        from app.services.article_indexability_report_service import (
            ArticleIndexabilityReportService,
        )

        inspect = self._inspect_enabled(profile)
        with self._session_factory() as session:
            report = ArticleIndexabilityReportService(session, settings=self._settings).build(
                inspect=inspect
            )
        rows = [
            {
                "article_id": row.article_id,
                "url": row.url,
                "live_state": row.live_state,
                "sitemap_state": row.sitemap_state,
                "google_index_state": row.google_index_state,
                "issues": list(row.live_issues),
            }
            for row in report.articles
        ]
        return StepOutcome(
            step_name=STEP_INDEXABILITY,
            status=OPS_SUCCEEDED,
            rows_received=len(rows),
            result={"inspect": inspect, "articles": rows},
        )

    def _step_evaluate_seo_candidates(self, *, profile, effective_date, **_kwargs) -> StepOutcome:
        from app.services.seo_improvement_candidate_service import (
            SeoImprovementCandidateService,
        )

        days = int(self._policy.gate("candidates", "window_days", 30))
        key = f"ops-seo-{profile}-{effective_date.isoformat()}"
        with self._session_factory() as session:
            service = SeoImprovementCandidateService(session, settings=self._settings)
            report = service.evaluate(days=days)
            run = service.persist(report, idempotency_key=key)
            return StepOutcome(
                step_name=STEP_SEO_CANDIDATES,
                status=OPS_SUCCEEDED,
                source_run_id=run.id,
                rows_received=report.article_count,
                rows_changed=report.total_candidates,
                result={"candidate_counts": report.candidate_counts, "idempotency_key": key},
            )

    def _step_evaluate_revenue_candidates(
        self, *, profile, effective_date, **_kwargs
    ) -> StepOutcome:
        from app.services.revenue_optimization_candidate_service import (
            RevenueOptimizationCandidateService,
        )

        days = int(self._policy.gate("candidates", "window_days", 30))
        key = f"ops-revenue-{profile}-{effective_date.isoformat()}"
        with self._session_factory() as session:
            service = RevenueOptimizationCandidateService(session, settings=self._settings)
            report = service.evaluate(days=days)
            run = service.persist(report, idempotency_key=key)
            return StepOutcome(
                step_name=STEP_REVENUE_CANDIDATES,
                status=OPS_SUCCEEDED,
                source_run_id=run.id,
                rows_received=report.article_count,
                rows_changed=report.total_candidates,
                result={
                    "candidate_counts": report.candidate_counts,
                    "raw_clicks": report.raw_clicks,
                    "excluded_clicks": report.excluded_clicks,
                    "clean_clicks": report.clean_clicks,
                    "idempotency_key": key,
                },
            )

    def _step_evaluate_monitoring(self, *, outcome, now, effective_date, **_kwargs) -> StepOutcome:
        """監視は :class:`OperationsMonitoringService` に委譲する (循環 import を避ける)。"""

        from app.services.operations_monitoring_service import OperationsMonitoringService

        with self._session_factory() as session:
            result = OperationsMonitoringService(
                session, settings=self._settings, policy=self._policy
            ).evaluate(outcome=outcome, now=now, today=effective_date)
        return StepOutcome(
            step_name=STEP_MONITORING,
            status=OPS_SUCCEEDED if result["notification_failures"] == 0 else OPS_PARTIAL,
            rows_received=result["alerts_evaluated"],
            rows_changed=result["alerts_recorded"],
            result=result,
        )

    # -- persistence ----------------------------------------------------------
    def _persist_step(self, run_id: int | None, step: StepOutcome) -> None:
        if run_id is None:
            return
        with self._session_factory() as session:
            session.add(
                OperationsStepRun(
                    operations_run_id=run_id,
                    step_name=step.step_name,
                    status=step.status,
                    source_run_id=step.source_run_id,
                    rows_received=step.rows_received,
                    rows_changed=step.rows_changed,
                    error_category=step.error_category,
                    error_message=step.error_message,
                    skip_reason=step.skip_reason,
                    attempt_count=step.attempt_count,
                    result_json=step.result or None,
                    started_at=to_storage_utc(step.started_at) if step.started_at else None,
                    finished_at=to_storage_utc(step.finished_at) if step.finished_at else None,
                )
            )
            session.commit()

    def _finalize(self, outcome: OperationsOutcome) -> None:
        failed = [s for s in outcome.steps if s.status == OPS_FAILED]
        skipped = [s for s in outcome.steps if s.status == OPS_SKIPPED]
        partial = [s for s in outcome.steps if s.status == OPS_PARTIAL]
        succeeded = [s for s in outcome.steps if s.status == OPS_SUCCEEDED]

        if failed and not succeeded:
            status = OPS_FAILED
        elif failed or skipped or partial:
            status = OPS_PARTIAL
        else:
            status = OPS_SUCCEEDED
        outcome.status = status

        with self._session_factory() as session:
            run = session.get(OperationsRun, outcome.run_id)
            if run is None:
                return
            run.status = status
            run.step_total = len(outcome.steps)
            run.step_succeeded = len(succeeded)
            run.step_failed = len(failed)
            run.step_skipped = len(skipped)
            run.failure_summary = (
                "; ".join(
                    f"{step.step_name}: {step.error_category or step.skip_reason}"
                    for step in failed + skipped
                )
                or None
            )
            run.summary_json = {"steps": [s.as_dict() for s in outcome.steps]}
            run.finished_at = to_storage_utc(datetime.now(UTC))
            session.commit()

    def _effective_date(self, now: datetime) -> date:
        return now.astimezone(self._policy.timezone).date()


# -- helpers -------------------------------------------------------------------
def _window(effective_date: date, config: dict) -> tuple[date, date]:
    days = int(config.get("days", 30))
    lag = int(config.get("lag_days", 1))
    end = effective_date - timedelta(days=lag)
    return end - timedelta(days=days - 1), end


def _git_revision() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:  # noqa: BLE001 - 取得できなくても運用は続く
        return None
    value = (result.stdout or "").strip()
    return value or None


def _schema_version(session: Session) -> str | None:
    try:
        return session.execute(select(func.max(_alembic_version()))).scalar()
    except Exception:  # noqa: BLE001
        return None


def _alembic_version():
    from sqlalchemy import column, table

    return table("alembic_version", column("version_num")).c.version_num


def latest_data_through(session: Session, source: str) -> date | None:
    """監視が使う「最後にデータが届いた日」。"""

    if source == "search_console":
        return session.scalar(select(func.max(SearchConsolePageDaily.metric_date)))
    if source == "ga4":
        return session.scalar(select(func.max(Ga4PageDaily.metric_date)))
    return None


def ever_had_data(session: Session, source: str) -> bool:
    """一度でもデータが届いたことがあるか (初期状態と故障を区別するため)。"""

    if source == "search_console":
        return bool(session.scalar(select(func.count()).select_from(SearchConsolePageDaily)))
    if source == "ga4":
        return bool(session.scalar(select(func.count()).select_from(Ga4PageDaily)))
    if source == "ga4_runs":
        return bool(session.scalar(select(func.count()).select_from(Ga4ImportRun)))
    return False
