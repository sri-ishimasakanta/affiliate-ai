"""OperationsNotificationService -- 日次インシデントと週次レポートの送信 (C8.7)。

C8 の通知ポリシーをメールへ広げるだけで、**新しい判断基準は増やさない**:

- 日次は「異常があったときだけ」送る。成功した日は送らない。
- 同じ run の問題は **1 通にまとめる**。症状ごとにメールを割らない。
- 個別アラートの重複抑止は既存の fingerprint / cooldown / severity に任せる
  (:class:`OperationsAlertService` がすでに判断している)。
- 週次は健全でも **必ず 1 通** 送る。ダイジェストは cooldown の対象外だが、
  同じ期間に二重送信しないよう決定的な dedupe key を持つ。
- **送信の失敗は取り込みや評価を壊さない。** 失敗は履歴として残すだけで、
  例外を上位に投げない。自動で何度も再送しない (SMTP を叩き続けない)。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.article.fact_freshness import to_storage_utc
from app.models import (
    DELIVERY_DUPLICATE,
    DELIVERY_FAILED,
    DELIVERY_SENT,
    DELIVERY_SKIPPED,
    NOTIFICATION_DAILY_INCIDENT,
    NOTIFICATION_WEEKLY_REPORT,
    NotificationDelivery,
    OperationsAlert,
    OperationsRun,
    OperationsStepRun,
)
from app.operations.email import EmailConfigError, build_email_config, build_email_notifier
from app.operations.notifications import sanitize_payload
from app.operations.policy import SEVERITY_ORDER, OperationsPolicy, get_policy
from app.operations.report_format import (
    render_daily_incident,
    render_weekly_report,
    weekly_subject,
    weekly_subject_severity,
)

#: 異常とみなす run status。
_UNHEALTHY_STATUSES = ("partial", "failed")


@dataclass
class NotificationOutcome:
    """1 回の通知判断の結果。送らなかった理由も必ず残す。"""

    notification_type: str
    sent: bool = False
    reason: str | None = None
    subject: str | None = None
    delivery_id: int | None = None
    error_category: str | None = None
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return sanitize_payload(
            {
                "notification_type": self.notification_type,
                "sent": self.sent,
                "reason": self.reason,
                "subject": self.subject,
                "delivery_id": self.delivery_id,
                "error_category": self.error_category,
                "detail": self.detail,
            }
        )


class OperationsNotificationService:
    def __init__(
        self,
        session: Session,
        *,
        settings,
        policy: OperationsPolicy | None = None,
        notifier=None,
        smtp_factory=None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._policy = policy or get_policy()
        self._notifier = notifier or build_email_notifier(settings, smtp_factory=smtp_factory)

    # -- public ---------------------------------------------------------------
    def notify_run(
        self, *, operations_run_id: int, now: datetime | None = None
    ) -> NotificationOutcome:
        """1 回の運用実行に対して、必要なメールを送る (profile ごとに規則が違う)。"""

        now = now or datetime.now(UTC)
        run = self._session.get(OperationsRun, operations_run_id)
        if run is None:
            return NotificationOutcome(
                NOTIFICATION_DAILY_INCIDENT, reason=f"operations run {operations_run_id} not found"
            )
        if run.profile == "weekly":
            return self.send_weekly_report(operations_run_id=run.id, now=now)
        return self.send_daily_incident(operations_run_id=run.id, now=now)

    def send_daily_incident(
        self, *, operations_run_id: int, now: datetime | None = None
    ) -> NotificationOutcome:
        """異常があるときだけ 1 通送る。健全な日は **送らない**。"""

        now = now or datetime.now(UTC)
        outcome = NotificationOutcome(NOTIFICATION_DAILY_INCIDENT)
        run = self._session.get(OperationsRun, operations_run_id)
        if run is None:
            outcome.reason = f"operations run {operations_run_id} not found"
            return outcome

        summary = self._run_summary(run)
        alerts = self._notifiable_alerts(run, now=now)
        unhealthy = run.status in _UNHEALTHY_STATUSES
        outcome.detail = {
            "run_status": run.status,
            "notifiable_alerts": len(alerts),
            "unhealthy": unhealthy,
        }

        if not unhealthy and not alerts:
            # 「全部成功しました」を毎日送らない。
            outcome.reason = "healthy run with no notify-worthy alert"
            return outcome

        severity = self._severity_for(run, alerts)
        title = {
            "failed": "Daily operations failed",
            "partial": "Daily operations requires attention",
        }.get(run.status, "Production monitoring alert")
        body = render_daily_incident(
            run_summary=summary,
            alerts=alerts,
            notes=[f"run status is {run.status}"] if unhealthy else [],
        )
        key = self._dedupe_key(NOTIFICATION_DAILY_INCIDENT, run.id, severity, len(alerts))
        return self._deliver(
            outcome,
            severity=severity,
            title=title,
            body=body,
            dedupe_key=key,
            operations_run_id=run.id,
            now=now,
        )

    def send_weekly_report(
        self,
        *,
        operations_run_id: int | None = None,
        now: datetime | None = None,
        report=None,
    ) -> NotificationOutcome:
        """健全でも **必ず 1 通** 送る。不完全なら件名でそう示す。"""

        now = now or datetime.now(UTC)
        outcome = NotificationOutcome(NOTIFICATION_WEEKLY_REPORT)
        if report is None:
            from app.services.operations_weekly_report_service import (
                OperationsWeeklyReportService,
            )

            report = OperationsWeeklyReportService(
                self._session, settings=self._settings, policy=self._policy
            ).build(operations_run_id=operations_run_id, now=now)

        outcome.detail = {
            "period_start": report.period_start,
            "period_end": report.period_end,
            "complete": report.complete,
        }
        # 同じ期間 x profile は 1 通だけ。スケジューラの再実行で二重送信しない。
        key = self._dedupe_key(
            NOTIFICATION_WEEKLY_REPORT, "weekly", report.period_end, report.reporting_timezone
        )
        return self._deliver(
            outcome,
            severity=weekly_subject_severity(report),
            title=weekly_subject(report),
            body=render_weekly_report(report),
            dedupe_key=key,
            operations_run_id=operations_run_id,
            now=now,
        )

    # -- internals ------------------------------------------------------------
    def _deliver(
        self,
        outcome: NotificationOutcome,
        *,
        severity: str,
        title: str,
        body: str,
        dedupe_key: str,
        operations_run_id: int | None,
        now: datetime,
    ) -> NotificationOutcome:
        try:
            config = build_email_config(self._settings)
        except EmailConfigError as exc:
            # 未設定は失敗ではない。運用は続く。
            outcome.reason = exc.reason
            self._record(
                outcome,
                subject=title,
                outcome_value=DELIVERY_SKIPPED,
                dedupe_key=None,
                operations_run_id=operations_run_id,
                recipients=(),
                now=now,
                error_category=None,
                error_message=exc.reason,
            )
            return outcome

        subject = config.severity_subject(severity, title)
        outcome.subject = subject

        if self._already_delivered(dedupe_key):
            outcome.reason = "an identical notification was already delivered"
            self._record(
                outcome,
                subject=subject,
                outcome_value=DELIVERY_DUPLICATE,
                dedupe_key=None,
                operations_run_id=operations_run_id,
                recipients=config.recipients,
                now=now,
            )
            return outcome

        if self._notifier is None:
            outcome.reason = "no email notifier is available"
            self._record(
                outcome,
                subject=subject,
                outcome_value=DELIVERY_SKIPPED,
                dedupe_key=None,
                operations_run_id=operations_run_id,
                recipients=config.recipients,
                now=now,
            )
            return outcome

        result = self._notifier.send_report(subject=subject, body=body)
        outcome.sent = result.delivered
        if not result.delivered:
            outcome.reason = "delivery failed"
            outcome.error_category = result.detail
        self._record(
            outcome,
            subject=subject,
            outcome_value=DELIVERY_SENT if result.delivered else DELIVERY_FAILED,
            # 失敗した送信で key を占有しない (修正後に再送できるようにする)。
            dedupe_key=dedupe_key if result.delivered else None,
            operations_run_id=operations_run_id,
            recipients=config.recipients,
            now=now,
            error_category=None if result.delivered else result.detail,
        )
        return outcome

    def _already_delivered(self, dedupe_key: str) -> bool:
        from sqlalchemy import select

        return (
            self._session.scalars(
                select(NotificationDelivery).where(
                    NotificationDelivery.dedupe_key == dedupe_key,
                    NotificationDelivery.outcome == DELIVERY_SENT,
                )
            ).first()
            is not None
        )

    def _record(
        self,
        outcome: NotificationOutcome,
        *,
        subject: str,
        outcome_value: str,
        dedupe_key: str | None,
        operations_run_id: int | None,
        recipients,
        now: datetime,
        error_category: str | None = None,
        error_message: str | None = None,
    ) -> None:
        row = NotificationDelivery(
            operations_run_id=operations_run_id,
            channel="email",
            notification_type=outcome.notification_type,
            recipient_fingerprint=_fingerprint(recipients),
            recipient_hint=_hint(recipients),
            subject=subject[:255],
            outcome=outcome_value,
            dedupe_key=dedupe_key,
            error_category=error_category,
            error_message=error_message,
            detail_json=outcome.detail or None,
            attempted_at=to_storage_utc(now),
            finished_at=to_storage_utc(datetime.now(UTC)),
        )
        self._session.add(row)
        try:
            self._session.commit()
        except IntegrityError:
            # 同じ dedupe key が並行して書かれた -- 二重送信を作らないための保険。
            self._session.rollback()
            outcome.reason = "an identical notification was already delivered"
            outcome.sent = False
            return
        self._session.refresh(row)
        outcome.delivery_id = row.id

    def _run_summary(self, run: OperationsRun) -> dict:
        from sqlalchemy import select

        steps = self._session.scalars(
            select(OperationsStepRun).where(OperationsStepRun.operations_run_id == run.id)
        ).all()
        return {
            "operations_run_id": run.id,
            "profile": run.profile,
            "status": run.status,
            "effective_date": run.effective_date.isoformat() if run.effective_date else None,
            "step_succeeded": run.step_succeeded,
            "step_failed": run.step_failed,
            "step_skipped": run.step_skipped,
            "failing_steps": [s.step_name for s in steps if s.status == "failed"],
            "skipped_steps": [s.step_name for s in steps if s.status == "skipped"],
            "partial_steps": [s.step_name for s in steps if s.status == "partial"],
        }

    def _notifiable_alerts(self, run: OperationsRun, *, now: datetime) -> list[dict]:
        """この run で **通知に値すると既に判断された** アラートだけを拾う。

        判断はやり直さない -- :class:`OperationsAlertService` が cooldown と
        severity をすでに適用し、通知したものに ``last_notified_at`` を立てている。
        """

        from sqlalchemy import select

        minimum = SEVERITY_ORDER.get(self._policy.notify_min_severity, 1)
        rows = self._session.scalars(
            select(OperationsAlert)
            .where(
                OperationsAlert.operations_run_id == run.id,
                OperationsAlert.status == "open",
            )
            .order_by(OperationsAlert.id)
        ).all()
        return [
            {
                "severity": row.severity,
                "alert_type": row.alert_type,
                "title": row.title,
                "summary": row.summary,
                "article_id": row.article_id,
                "occurrence_count": row.occurrence_count,
            }
            for row in rows
            if SEVERITY_ORDER.get(row.severity, 0) >= minimum and row.last_notified_at is not None
        ]

    def _severity_for(self, run: OperationsRun, alerts: list[dict]) -> str:
        worst = max(
            (SEVERITY_ORDER.get(a["severity"], 0) for a in alerts),
            default=0,
        )
        by_name = {value: name for name, value in SEVERITY_ORDER.items()}
        severity = by_name.get(worst, "info")
        if run.status == "failed" and SEVERITY_ORDER.get(severity, 0) < SEVERITY_ORDER.get(
            "error", 2
        ):
            return "error"
        if run.status == "partial" and SEVERITY_ORDER.get(severity, 0) < SEVERITY_ORDER.get(
            "warning", 1
        ):
            return "warning"
        return severity

    @staticmethod
    def _dedupe_key(*parts) -> str:
        raw = chr(31).join(str(part) for part in parts)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]


def _fingerprint(recipients) -> str:
    """宛先の安全な表現。生アドレスは DB に残さない。"""

    joined = ",".join(sorted(str(r).strip().lower() for r in recipients))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _hint(recipients) -> str | None:
    """「何件 / どのドメインか」だけを残す。"""

    items = [str(r) for r in recipients]
    if not items:
        return None
    domains = sorted({item.rsplit("@", 1)[-1] for item in items if "@" in item})
    return f"{len(items)} recipient(s) @{', @'.join(domains)}"[:255]
