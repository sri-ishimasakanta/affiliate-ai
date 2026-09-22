"""OperationsAlertService -- アラートの記録と通知 (C8)。

ノイズを出さないことがこの service の目的:

- 同じ ``fingerprint`` のアラートは 1 行に集約し、``last_seen_at`` と
  ``occurrence_count`` を進めるだけにする (毎日新しい行を作らない)。
- 通知は「新規」か「cooldown を過ぎた」場合のみ。同じ問題を毎日通知しない。
- ポリシーの ``notify_min_severity`` を下回る重大度は記録するが通知しない。
- **通知の失敗は取り込みや候補評価を壊さない**。送信結果は事実として返すだけ。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.article.fact_freshness import to_storage_utc
from app.models import Article, OperationsAlert
from app.operations.monitoring import AlertDraft
from app.operations.notifications import (
    NotificationMessage,
    NotificationResult,
    Notifier,
)
from app.operations.policy import SEVERITY_ORDER, OperationsPolicy


@dataclass
class AlertOutcome:
    recorded: int = 0
    new_alerts: int = 0
    repeated_alerts: int = 0
    notified: int = 0
    suppressed_by_severity: int = 0
    suppressed_by_cooldown: int = 0
    notification_results: list[NotificationResult] = field(default_factory=list)
    notification_failures: int = 0


class OperationsAlertService:
    """transaction owner。アラートの記録は必ず commit してから通知する。"""

    def __init__(
        self,
        session: Session,
        *,
        policy: OperationsPolicy,
        notifiers: list[Notifier] | None = None,
    ) -> None:
        self._session = session
        self._policy = policy
        self._notifiers = notifiers or []

    def record_and_notify(
        self,
        drafts: list[AlertDraft],
        *,
        operations_run_id: int | None = None,
        now: datetime | None = None,
    ) -> AlertOutcome:
        now = now or datetime.now(UTC)
        stored_now = to_storage_utc(now)
        outcome = AlertOutcome()
        pending: list[tuple[OperationsAlert, AlertDraft]] = []

        for draft in drafts:
            row = self._session.scalars(
                select(OperationsAlert).where(OperationsAlert.fingerprint == draft.fingerprint)
            ).first()
            if row is None:
                row = OperationsAlert(
                    alert_type=draft.alert_type,
                    severity=draft.severity,
                    source=draft.source,
                    title=draft.title[:255],
                    summary=draft.summary,
                    article_id=draft.article_id,
                    affiliate_program_id=draft.affiliate_program_id,
                    fingerprint=draft.fingerprint,
                    status="open",
                    occurrence_count=1,
                    first_seen_at=stored_now,
                    last_seen_at=stored_now,
                    evidence_json=draft.evidence,
                    operations_run_id=operations_run_id,
                )
                self._session.add(row)
                outcome.new_alerts += 1
                is_new = True
            else:
                row.occurrence_count += 1
                row.last_seen_at = stored_now
                row.severity = draft.severity
                row.summary = draft.summary
                row.evidence_json = draft.evidence
                row.operations_run_id = operations_run_id
                # 再発したら解決済みでは無くなる。
                if row.status == "resolved":
                    row.status = "open"
                    row.resolved_at = None
                outcome.repeated_alerts += 1
                is_new = False
            outcome.recorded += 1

            if not self._should_notify(row, draft, now=now, is_new=is_new, outcome=outcome):
                continue
            pending.append((row, draft))

        # 記録を先に確定させる -- 通知が失敗しても事実は残る。
        self._session.commit()

        for row, draft in pending:
            delivered = False
            for notifier in self._notifiers:
                result = notifier.send(_message(draft, row, operations_run_id, now))
                outcome.notification_results.append(result)
                if result.delivered:
                    delivered = True
                else:
                    outcome.notification_failures += 1
            if delivered:
                row.last_notified_at = stored_now
                outcome.notified += 1
        self._session.commit()
        return outcome

    # -- internals ------------------------------------------------------------
    def _should_notify(
        self,
        row: OperationsAlert,
        draft: AlertDraft,
        *,
        now: datetime,
        is_new: bool,
        outcome: AlertOutcome,
    ) -> bool:
        minimum = SEVERITY_ORDER.get(self._policy.notify_min_severity, 1)
        if SEVERITY_ORDER.get(draft.severity, 0) < minimum:
            outcome.suppressed_by_severity += 1
            return False
        if is_new or row.last_notified_at is None:
            return True
        last = row.last_notified_at
        last = last if last.tzinfo else last.replace(tzinfo=UTC)
        if now - last < timedelta(hours=self._policy.cooldown_hours):
            outcome.suppressed_by_cooldown += 1
            return False
        return True

    def active_alerts(self) -> list[OperationsAlert]:
        return list(
            self._session.scalars(
                select(OperationsAlert)
                .where(OperationsAlert.status == "open")
                .order_by(OperationsAlert.last_seen_at.desc())
            ).all()
        )

    def acknowledge(self, fingerprint: str, *, now: datetime | None = None) -> bool:
        row = self._session.scalars(
            select(OperationsAlert).where(OperationsAlert.fingerprint == fingerprint)
        ).first()
        if row is None or row.status != "open":
            return False
        row.status = "acknowledged"
        row.acknowledged_at = to_storage_utc(now or datetime.now(UTC))
        self._session.commit()
        return True

    def resolve(self, fingerprint: str, *, now: datetime | None = None) -> bool:
        row = self._session.scalars(
            select(OperationsAlert).where(OperationsAlert.fingerprint == fingerprint)
        ).first()
        if row is None or row.status == "resolved":
            return False
        row.status = "resolved"
        row.resolved_at = to_storage_utc(now or datetime.now(UTC))
        self._session.commit()
        return True

    def _slug_for(self, article_id: int | None) -> str | None:
        if article_id is None:
            return None
        article = self._session.get(Article, article_id)
        return article.slug if article else None


def _message(
    draft: AlertDraft,
    row: OperationsAlert,
    operations_run_id: int | None,
    now: datetime,
) -> NotificationMessage:
    return NotificationMessage(
        severity=draft.severity,
        title=draft.title,
        summary=draft.summary,
        alert_type=draft.alert_type,
        fingerprint=draft.fingerprint,
        operations_run_id=operations_run_id,
        article_id=draft.article_id,
        affiliate_program_id=draft.affiliate_program_id,
        occurred_at=now,
        evidence=draft.evidence,
    )
