"""システムの健康 (C10-3 / C10-F)。**点検は読むだけ・外に問い合わせない。**

- ``evaluate(now)``: 夜の分析・worker・データ・仕事の滞り・DB の点検 (``app/operations/
  system_health.py``) と、人向けの短い要約。
- ``sync_alerts(now, notifiers)``: 行動が要る点検 (warning / error) だけを既存の
  ``operations_alerts`` に記録 (出所 ``c10_health``、指紋は日付を含まない)。消えた問題の警告は
  自動で解決する (この出所の警告だけ)。日ごとの報告のメールには入れない
  (``operations_run_id`` を付けない)。知らせ直しは ``renotify_after_hours`` おき。運用の
  毎朝の点検の段 (06:30) から呼ぶ (夜中には知らせない)。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware
from app.operations import system_health as sh

WORKER_LOG = Path(r"D:\Logs\affiliate-ai\threads-worker.log")
_LOG_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}) threads-worker ERROR")


class SystemHealthService:
    def __init__(self, session: Session, *, settings=None, worker_log: Path | None = None,
                 policy: dict | None = None, task_registered=None,
                 schema_heads=None, expect_worker: bool = True) -> None:  # fmt: skip
        self._session = session
        # N6: 常駐の worker を持たないサイト (手元の試しのプロファイル) は worker を見ない。
        self._expect_worker = expect_worker
        # テストでは差し替える (schtasks / alembic を読まない)。
        self._task_registered_fn = task_registered or self._task_registered
        self._schema_heads_fn = schema_heads or self._schema_heads
        if settings is None:
            from app.config.settings import get_settings

            settings = get_settings()
        self._settings = settings
        self._log = worker_log or WORKER_LOG
        self._policy = policy or sh.load_health_policy()

    def _tz(self):
        from app.operations.policy import get_policy

        return get_policy().timezone

    # -- 点検 ---------------------------------------------------------------------------------
    def evaluate(self, *, now: datetime | None = None) -> dict:
        now = ensure_aware(now or datetime.now(UTC))
        nightly, nightly_summary = self._nightly(now)
        worker, worker_summary = (self._worker(now) if self._expect_worker
                                  else ([], {"expected": False}))  # fmt: skip
        data, data_summary = self._data(now)
        workflow, workflow_summary = self._workflow(now)
        db, db_summary = self._db()
        findings = nightly + worker + data + workflow + db
        self._session.rollback()
        return {"schema": sh.SCHEMA, "as_of": now.isoformat(),
                "findings": [f.as_dict() for f in findings],
                "actionable": [f.as_dict() for f in findings if f.actionable],
                "summary": {"nightly": nightly_summary, "worker": worker_summary,
                            "data": data_summary, "workflow": workflow_summary,
                            "db": db_summary},
                "healthy": not any(f.actionable for f in findings),
                "_objects": findings}

    def _nightly(self, now: datetime):
        from app.models.content_discovery import NightlyAnalysisRun
        from app.services.google_ads_refresh_service import GoogleAdsRefreshService
        from app.services.nightly_analysis_service import load_policy, tables_ready

        policy = load_policy()
        schedule = policy.get("schedule") or {}
        registered = self._task_registered_fn(schedule.get("task_name"))
        if not tables_ready(self._session):
            return [], {"tables_ready": False}
        runs = list(self._session.scalars(select(NightlyAnalysisRun)
                                          .order_by(NightlyAnalysisRun.id.desc()).limit(14)))
        last = runs[0] if runs else None
        success = next((r for r in runs if r.status == "succeeded"), None)
        last_dict = None if last is None else {
            "run_key": last.run_key, "status": last.status, "failure_reason": last.failure_reason,
            "attempt_count": last.attempt_count}
        tz = self._tz()
        success_at = ensure_aware(success.finished_at).astimezone(tz) if (
            success and success.finished_at) else None
        findings = sh.nightly_findings(
            self._policy, now_local=now.astimezone(tz),
            schedule_time=schedule.get("start_time", "03:30"), registered=registered,
            last_run=last_dict, last_success_at=success_at,
            eligible=last.eligible_count if last else None, budget=int(policy.get("budget", 80)))
        refresh = GoogleAdsRefreshService(self._session, settings=self._settings).plan(now=now)
        if refresh["terms"]:
            findings.append(sh.finding(
                self._policy, "google_ads_refresh_backlog", "nightly",
                "Google Ads refresh backlog",
                f"{refresh['terms']} term(s) in {refresh['calls_if_run']} batched call(s) wait "
                "for a human-approved refresh (refresh_google_ads_metrics.py)",
                terms=refresh["terms"]))
        duration = None
        if success and success.finished_at and success.started_at:
            duration = round((ensure_aware(success.finished_at)
                              - ensure_aware(success.started_at)).total_seconds(), 1)
        summary = {"registered": registered, "schedule": schedule.get("start_time"),
                   "last_run": last_dict,
                   "last_success": success.run_key if success else None,
                   "last_success_at": success_at.isoformat() if success_at else None,
                   "duration_seconds": duration,
                   "analyzed": success.analyzed_count if success else None,
                   "skipped": success.skipped_count if success else None,
                   "refresh_required": success.refresh_required_count if success else None,
                   "next_articles": success.next_article_count if success else None,
                   "google_ads_backlog_terms": refresh["terms"]}
        return findings, summary

    @staticmethod
    def _task_registered(task_name: str | None) -> bool:
        """タスクが登録済みか (読むだけ)。分からなければ方針の値。"""

        import subprocess

        if not task_name:
            return False
        try:
            r = subprocess.run(["schtasks", "/Query", "/TN", task_name], capture_output=True,
                               text=True, timeout=15)
            return r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            from app.services.nightly_analysis_service import load_policy

            return bool((load_policy().get("schedule") or {}).get("registered"))

    def _worker(self, now: datetime):
        from app.models import OperationsLock

        name = (self._policy.get("worker") or {}).get("lock_name", "threads_worker")
        row = self._session.scalars(select(OperationsLock).where(
            OperationsLock.lock_name == name)).first()  # fmt: skip
        lock = None if row is None else {
            "owner_label": row.owner_label,
            "heartbeat_at": ensure_aware(row.heartbeat_at) if row.heartbeat_at else None,
            "released_at": ensure_aware(row.released_at) if row.released_at else None}
        errors = self._log_errors(now)
        findings = sh.worker_findings(self._policy, now=now, lock=lock, log_errors=errors)
        return findings, {"lock_owner": lock["owner_label"] if lock else None,
                          "last_heartbeat": lock["heartbeat_at"].isoformat()
                          if lock and lock["heartbeat_at"] else None,
                          "log_errors_24h": errors}

    def _log_errors(self, now: datetime) -> int:
        hours = float((self._policy.get("worker") or {}).get("log_error_window_hours", 24))
        if not self._log.exists():
            return 0
        since = now - timedelta(hours=hours)
        count = 0
        with self._log.open("rb") as fh:
            fh.seek(max(0, self._log.stat().st_size - 400_000))
            for raw in fh.read().decode("utf-8", errors="replace").splitlines():
                m = _LOG_TIME.match(raw)
                if m and datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S%z") >= since:
                    count += 1
        return count

    def _data(self, now: datetime):
        from app.services.source_health_service import SourceHealthService

        statuses = SourceHealthService(self._session, settings=self._settings).collect(now=now)
        findings = []
        # 取り込みの出所 (GSC・GA4・クリック・成果) は既存の DATA_STALE / IMPORT_FAILURE の警告が
        # 見る。ここは、既存の警告が見ていない出所だけ。
        for name in ("url_inspection", "threads_insights", "google_ads"):
            s = statuses.get(name)
            # 一度も来ていない (missing) のは未設定で、壊れたのではない。要約にだけ出す。
            if s is not None and s.freshness_state in ("stale", "provider_error"):
                findings.append(sh.finding(
                    self._policy, f"source_stale_{name}", "data", f"{name} is {s.freshness_state}",
                    f"{name}: observed {s.observed_at}; {s.missing_reason or ''}".strip(),
                    subject=name))
        summary = {k: {"state": v.freshness_state, "data_through": v.data_through,
                       "observed_at": v.observed_at} for k, v in statuses.items()}
        return findings, summary

    def _workflow(self, now: datetime):
        from app.models import ChangeRequest, ThreadsPostProposal
        from app.models.growth_action import GrowthActionEvent, GrowthActionReview
        from app.models.growth_handoff import GH_OPEN_STATUSES, GrowthHandoffRequest
        from app.services.growth_handoff_service import handoff_ready

        cfg = self._policy.get("workflow") or {}
        findings = []

        def older(days=None, hours=None):
            return now - (timedelta(days=days) if days is not None else timedelta(hours=hours))

        pending_reviews = [r for r in self._session.scalars(select(GrowthActionReview).where(
            GrowthActionReview.status == "pending"))]
        old_reviews = [r for r in pending_reviews if r.requested_at and ensure_aware(
            r.requested_at) < older(days=cfg.get("review_backlog_days", 7))]
        if old_reviews:
            findings.append(sh.finding(self._policy, "growth_review_backlog", "workflow",
                                       "Growth reviews waiting",
                                       f"{len(old_reviews)} review(s) pending for more than "
                                       f"{cfg.get('review_backlog_days', 7)} days"))
        stuck = []
        if handoff_ready(self._session):
            stuck = [h for h in self._session.scalars(select(GrowthHandoffRequest).where(
                GrowthHandoffRequest.status.in_(tuple(GH_OPEN_STATUSES))))
                if ensure_aware(h.updated_at) < older(days=cfg.get("handoff_stuck_days", 14))]
            for h in stuck:
                findings.append(sh.finding(
                    self._policy, "handoff_stuck", "workflow", "Handoff request stuck",
                    f"handoff {h.id} ({h.workflow}) is {h.status} since {h.updated_at}",
                    subject=str(h.id)))
        crs = [c for c in self._session.scalars(select(ChangeRequest).where(
            ChangeRequest.status == "awaiting_approval"))
            if ensure_aware(c.created_at) < older(days=cfg.get("change_request_stuck_days", 14))]
        if crs:
            findings.append(sh.finding(self._policy, "change_request_stuck", "workflow",
                                       "Change requests waiting",
                                       f"{len(crs)} change request(s) awaiting approval"))
        proposals = [p for p in self._session.scalars(select(ThreadsPostProposal).where(
            ThreadsPostProposal.status == "awaiting_approval"))
            if ensure_aware(p.created_at) < older(hours=cfg.get("proposal_awaiting_hours", 72))]
        if proposals:
            findings.append(sh.finding(self._policy, "threads_proposal_awaiting", "workflow",
                                       "Threads proposals waiting",
                                       f"{len(proposals)} proposal(s) awaiting approval"))
        failures = self._session.scalar(select(func.count()).select_from(GrowthActionEvent)
                                        .where(GrowthActionEvent.event_type == "conversion_failed",
                                               GrowthActionEvent.occurred_at >= older(
                                                   days=cfg.get("conversion_failure_window_days",
                                                                7)))) or 0
        if failures:
            findings.append(sh.finding(self._policy, "conversion_failures", "workflow",
                                       "Growth conversion failures",
                                       f"{failures} conversion failure(s) recently"))
        from app.services.growth_measurement_service import GrowthMeasurementService, summarize

        measured, _stats = GrowthMeasurementService(self._session,
                                                    settings=self._settings).refresh(now=now)
        due = summarize(measured, now=now)["due_now"]
        if due:
            findings.append(sh.finding(self._policy, "measurements_due", "workflow",
                                       "Follow-up measurements due",
                                       f"{len(due)} growth action(s) have due checkpoints"))
        summary = {"growth_reviews_pending": len(pending_reviews),
                   "handoffs_stuck": len(stuck), "change_requests_waiting": len(crs),
                   "threads_proposals_waiting": len(proposals),
                   "conversion_failures": failures, "measurements_due": len(due)}
        return findings, summary

    def _db(self):
        from app.project_state.local_state import PENDING_PRODUCTION_MIGRATIONS

        current, heads = self._schema_heads_fn()
        findings = []
        pending = self._pending(current, heads) if current != heads else []
        declared = bool(pending) and all(r in PENDING_PRODUCTION_MIGRATIONS for r in pending)
        if current != heads and declared:
            # 人の確認点で待っていると宣言した migration だけが未適用 (開発の通常の状態)。
            findings.append(sh.finding(self._policy, "schema_pending_declared", "db",
                                       "Migration waiting for a human production apply",
                                       f"pending {pending} (declared; database "
                                       f"{sorted(current)})"))  # fmt: skip
        elif current != heads:
            findings.append(sh.finding(self._policy, "schema_mismatch", "db",
                                       "Database schema is not at the code head",
                                       f"database {sorted(current)}, code {sorted(heads)}"))
        return findings, {"current": sorted(current), "heads": sorted(heads),
                          "pending": pending, "pending_declared": declared}  # fmt: skip

    @staticmethod
    def _pending(current: set, heads: set) -> list[str]:
        """今から head までの未適用の revision。分からなければ head との差。"""

        try:
            from alembic.config import Config
            from alembic.script import ScriptDirectory

            root = Path(__file__).resolve().parents[2]
            script = ScriptDirectory.from_config(Config(str(root / "alembic.ini")))
            base = next(iter(current)) if len(current) == 1 else None
            chain = [r.revision for r in script.iterate_revisions(
                next(iter(heads)) if len(heads) == 1 else "heads", base)
                if r.revision not in current]  # fmt: skip
            return chain
        except Exception:  # noqa: BLE001 - 分からなければ head との差だけ
            return sorted(heads - current)

    def _schema_heads(self) -> tuple[set, set]:
        from alembic.config import Config
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory

        root = Path(__file__).resolve().parents[2]
        config = Config(str(root / "alembic.ini"))
        heads = set(ScriptDirectory.from_config(config).get_heads())
        current = set(MigrationContext.configure(self._session.connection()).get_current_heads())
        return current, heads

    # -- 警告の同期 -------------------------------------------------------------------------------
    def sync_alerts(self, *, now: datetime | None = None, notifiers=None) -> dict:
        from app.models import OperationsAlert
        from app.operations.policy import get_policy
        from app.services.operations_alert_service import OperationsAlertService

        now = ensure_aware(now or datetime.now(UTC))
        result = self.evaluate(now=now)
        findings = [f for f in result["_objects"] if f.actionable]
        service = OperationsAlertService(self._session, policy=get_policy(),
                                         notifiers=notifiers or [])
        outcome = service.record_and_notify([f.as_draft() for f in findings], now=now)
        present = {f.fingerprint for f in findings}
        resolved = []
        for row in self._session.scalars(select(OperationsAlert).where(
                OperationsAlert.source == sh.SOURCE, OperationsAlert.status != "resolved")):
            if row.fingerprint not in present:
                resolved.append(row.fingerprint)
        for fp in resolved:
            service.resolve(fp, now=now, reason="the condition cleared (system health)",
                            resolved_by="system_health")
        return {"recorded": len(findings), "notified": getattr(outcome, "notified", 0),
                "resolved": len(resolved), "healthy": result["healthy"]}


__all__ = ["SystemHealthService"]
