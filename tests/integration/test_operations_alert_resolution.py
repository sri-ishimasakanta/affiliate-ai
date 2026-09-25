"""アラートの解決と再発 (C8 のライフサイクル、2026-09-25 の片付けで理由の記録を追加)。

pin する契約:

- 解決しても行は消さない。解決の理由・主体・時刻を evidence に残す。元の証拠も残る。
- 同じ fingerprint が再発すれば開き直る。前の解決の記録は ``previous_resolution`` に残る。
- 自動では解決しない (解決は人の操作)。
- Threads の権限の失敗は、再発しても ``threads_media_unreadable`` ではなく、
  分類どおりの ``threads_insights:threads_permission`` として新しく立つ。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OperationsAlert
from app.operations.monitoring import AUTOMATION_HEALTH, AlertDraft
from app.operations.policy import load_policy
from app.operations.threads_health import ThreadsHealthInput, build_threads_alert_drafts
from app.services.operations_alert_service import OperationsAlertService

_NOW = datetime(2026, 9, 24, 21, 30, tzinfo=UTC)
_REASON = "Meta Threads API access recovered and subsequent live insight reads succeeded."


def _service(session: Session) -> OperationsAlertService:
    return OperationsAlertService(session, policy=load_policy(), notifiers=[])


def _unreadable() -> AlertDraft:
    return AlertDraft(
        alert_type=AUTOMATION_HEALTH,
        severity="warning",
        source="threads_insights",
        title="公開済み Threads 投稿が読めない",
        summary="削除・非公開・ID の不整合のいずれか。",
        fingerprint="threads_media_unreadable:1",
        evidence={"publication_id": 1},
    )


def _row(session: Session, fingerprint: str) -> OperationsAlert:
    return session.scalars(
        select(OperationsAlert).where(OperationsAlert.fingerprint == fingerprint)
    ).one()


def test_resolving_records_the_reason_and_keeps_the_row(session: Session) -> None:
    _service(session).record_and_notify([_unreadable()], now=_NOW)
    resolved_at = _NOW + timedelta(hours=14)

    assert _service(session).resolve(
        "threads_media_unreadable:1", now=resolved_at, reason=_REASON, resolved_by="human-cli"
    )
    row = _row(session, "threads_media_unreadable:1")
    assert row.status == "resolved"
    assert row.resolved_at.replace(tzinfo=UTC) == resolved_at
    assert row.evidence_json["publication_id"] == 1  # 元の証拠は残る
    assert row.evidence_json["resolution"]["reason"] == _REASON
    assert row.evidence_json["resolution"]["resolved_by"] == "human-cli"


def test_resolving_twice_is_a_no_op(session: Session) -> None:
    _service(session).record_and_notify([_unreadable()], now=_NOW)
    assert _service(session).resolve("threads_media_unreadable:1", reason=_REASON)
    assert _service(session).resolve("threads_media_unreadable:1", reason=_REASON) is False


def test_resolving_without_a_reason_keeps_the_old_behaviour(session: Session) -> None:
    _service(session).record_and_notify([_unreadable()], now=_NOW)
    assert _service(session).resolve("threads_media_unreadable:1")
    row = _row(session, "threads_media_unreadable:1")
    assert row.status == "resolved"
    assert "resolution" not in row.evidence_json


def test_a_recurrence_reopens_and_keeps_the_previous_resolution(session: Session) -> None:
    service = _service(session)
    service.record_and_notify([_unreadable()], now=_NOW)
    service.resolve("threads_media_unreadable:1", now=_NOW + timedelta(hours=1), reason=_REASON)

    service.record_and_notify([_unreadable()], now=_NOW + timedelta(days=2))
    row = _row(session, "threads_media_unreadable:1")
    assert row.status == "open"
    assert row.resolved_at is None
    assert row.occurrence_count == 2
    assert row.evidence_json["previous_resolution"]["reason"] == _REASON


def test_a_future_permission_failure_opens_the_correct_alert(session: Session) -> None:
    """同じ "API access blocked." が再発しても、「投稿が読めない」は開き直さない。"""

    service = _service(session)
    service.record_and_notify([_unreadable()], now=_NOW)
    service.resolve("threads_media_unreadable:1", reason=_REASON)

    drafts = build_threads_alert_drafts(
        [
            ThreadsHealthInput(
                publication_id=1,
                failure_category="threads_permission",
                failure_reason="/18075152792458838/insights failed: API access blocked.",
                consecutive_failures=1,
            )
        ]
    )
    service.record_and_notify(drafts, now=_NOW + timedelta(days=3))

    assert _row(session, "threads_media_unreadable:1").status == "resolved"
    permission = _row(session, "threads_insights:threads_permission")
    assert permission.status == "open"
    assert permission.severity == "error"
    assert permission.alert_type == "IMPORT_FAILURE"
