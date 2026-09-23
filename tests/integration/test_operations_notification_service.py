"""日次インシデントと週次レポートの送信ポリシー (C8.7)。

pin する契約:

- 健全な日次実行では **メールを送らない** (成功報告を毎日送らない)。
- partial / failed の日次実行では 1 通だけ送る。
- 同じ run の複数アラートは **1 通にまとめる**。
- 個別アラートの抑止は既存の cooldown / severity に従う (ここで作り直さない)。
- 週次は健全でも必ず 1 通送る。不完全なら件名でそう示す。
- 同じ週次 dedupe key では二重に送らない (スケジューラ再実行を含む)。
- 送信の成否にかかわらず履歴が 1 行残る。password は残らない。
- 未設定は「失敗」ではなく ``skipped``。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

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
from app.operations.notifications import NotificationResult
from app.services.operations_notification_service import OperationsNotificationService

_NOW = datetime(2026, 9, 23, 6, 40, tzinfo=UTC)
_PASSWORD = "super-secret-app-password"


class _Settings:
    operations_email_enabled = True
    operations_email_smtp_host = "smtp.example.com"
    operations_email_smtp_port = 587
    operations_email_username = "ops@example.com"
    operations_email_password = _PASSWORD
    operations_email_from = "ops@example.com"
    operations_email_use_starttls = True
    operations_email_subject_prefix = "BizFluxLab"
    operations_webhook_url = None
    wordpress_base_url = "https://example.com"
    search_console_property_uri = "sc-domain:example.com"
    search_console_credentials_file = None
    ga4_property_id = None

    @property
    def operations_email_recipients(self):
        return ["owner@example.com"]


class _RecordingNotifier:
    """送った件名と本文を覚えるだけ。SMTP には触れない。"""

    name = "email"

    def __init__(self, *, delivered: bool = True) -> None:
        self.sent: list[tuple[str, str]] = []
        self._delivered = delivered

    def send_report(self, *, subject: str, body: str) -> NotificationResult:
        self.sent.append((subject, body))
        return NotificationResult(
            self.name, self._delivered, None if self._delivered else "TimeoutError"
        )


def _run(session: Session, *, profile: str = "daily", status: str = "succeeded") -> OperationsRun:
    run = OperationsRun(
        profile=profile,
        policy_version="ops-1",
        effective_date=date(2026, 9, 23),
        timezone_name="Asia/Tokyo",
        trigger="scheduler",
        status=status,
        step_total=8,
        step_succeeded=8 if status == "succeeded" else 6,
        step_failed=0 if status != "failed" else 2,
        step_skipped=0,
        started_at=_NOW,
        finished_at=_NOW,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def _step(session: Session, run: OperationsRun, name: str, status: str) -> None:
    session.add(
        OperationsStepRun(operations_run_id=run.id, step_name=name, status=status, attempt_count=1)
    )
    session.commit()


def _alert(
    session: Session,
    run: OperationsRun,
    *,
    severity: str,
    fingerprint: str,
    notified: bool = True,
    status: str = "open",
) -> OperationsAlert:
    row = OperationsAlert(
        alert_type="DATA_STALE",
        severity=severity,
        source="search_console",
        title=f"{fingerprint} title",
        summary=f"{fingerprint} summary",
        fingerprint=fingerprint,
        status=status,
        occurrence_count=1,
        first_seen_at=_NOW,
        last_seen_at=_NOW,
        last_notified_at=_NOW if notified else None,
        evidence_json={"coverage_through": "2026-09-22"},
        operations_run_id=run.id,
    )
    session.add(row)
    session.commit()
    return row


def _service(session: Session, notifier=None) -> OperationsNotificationService:
    return OperationsNotificationService(
        session, settings=_Settings(), notifier=notifier or _RecordingNotifier()
    )


# -- daily ---------------------------------------------------------------------
def test_a_healthy_daily_run_sends_nothing(session: Session) -> None:
    run = _run(session, status="succeeded")
    notifier = _RecordingNotifier()

    outcome = _service(session, notifier).send_daily_incident(operations_run_id=run.id, now=_NOW)

    assert outcome.sent is False
    assert "healthy run" in outcome.reason
    assert notifier.sent == []
    assert session.scalars(select(NotificationDelivery)).all() == []


def test_a_failed_daily_run_sends_one_email(session: Session) -> None:
    run = _run(session, status="failed")
    _step(session, run, "import_ga4", "failed")
    notifier = _RecordingNotifier()

    outcome = _service(session, notifier).send_daily_incident(operations_run_id=run.id, now=_NOW)

    assert outcome.sent is True
    assert len(notifier.sent) == 1
    subject, body = notifier.sent[0]
    assert subject == "[BizFluxLab][ERROR] Daily operations failed"
    assert "import_ga4" in body


def test_a_partial_daily_run_sends_one_warning(session: Session) -> None:
    run = _run(session, status="partial")
    _step(session, run, "import_make_commissions", "skipped")
    notifier = _RecordingNotifier()

    _service(session, notifier).send_daily_incident(operations_run_id=run.id, now=_NOW)

    subject, body = notifier.sent[0]
    assert subject == "[BizFluxLab][WARNING] Daily operations requires attention"
    assert "import_make_commissions" in body


def test_multiple_alerts_are_consolidated_into_one_email(session: Session) -> None:
    run = _run(session, status="succeeded")
    _alert(session, run, severity="warning", fingerprint="fp-a")
    _alert(session, run, severity="error", fingerprint="fp-b")
    _alert(session, run, severity="warning", fingerprint="fp-c")
    notifier = _RecordingNotifier()

    outcome = _service(session, notifier).send_daily_incident(operations_run_id=run.id, now=_NOW)

    assert outcome.sent is True
    # 症状ごとに割らない。
    assert len(notifier.sent) == 1
    subject, body = notifier.sent[0]
    # いちばん重い severity が件名になる。
    assert subject.startswith("[BizFluxLab][ERROR]")
    for fingerprint in ("fp-a", "fp-b", "fp-c"):
        assert f"{fingerprint} title" in body


def test_an_alert_suppressed_by_cooldown_does_not_email(session: Session) -> None:
    """既存の cooldown 判定 (last_notified_at が立っていない) をやり直さない。"""

    run = _run(session, status="succeeded")
    _alert(session, run, severity="warning", fingerprint="fp-quiet", notified=False)
    notifier = _RecordingNotifier()

    outcome = _service(session, notifier).send_daily_incident(operations_run_id=run.id, now=_NOW)

    assert outcome.sent is False
    assert notifier.sent == []


def test_a_low_severity_alert_does_not_email(session: Session) -> None:
    run = _run(session, status="succeeded")
    _alert(session, run, severity="info", fingerprint="fp-info")
    notifier = _RecordingNotifier()

    outcome = _service(session, notifier).send_daily_incident(operations_run_id=run.id, now=_NOW)

    assert outcome.sent is False
    assert notifier.sent == []


def test_a_resolved_alert_is_not_reported_as_open(session: Session) -> None:
    run = _run(session, status="succeeded")
    _alert(session, run, severity="error", fingerprint="fp-done", status="resolved")
    notifier = _RecordingNotifier()

    outcome = _service(session, notifier).send_daily_incident(operations_run_id=run.id, now=_NOW)

    assert outcome.sent is False


def test_a_reopened_alert_can_email_again(session: Session) -> None:
    run = _run(session, status="succeeded")
    row = _alert(session, run, severity="error", fingerprint="fp-again", status="resolved")
    notifier = _RecordingNotifier()
    assert (
        _service(session, notifier).send_daily_incident(operations_run_id=run.id, now=_NOW).sent
        is False
    )

    # 再発 -- OperationsAlertService と同じ遷移を再現する。
    row.status = "open"
    row.resolved_at = None
    session.commit()

    assert (
        _service(session, notifier).send_daily_incident(operations_run_id=run.id, now=_NOW).sent
        is True
    )


# -- weekly --------------------------------------------------------------------
def test_a_healthy_weekly_run_always_sends_one_digest(session: Session) -> None:
    run = _run(session, profile="weekly", status="succeeded")
    notifier = _RecordingNotifier()

    outcome = _service(session, notifier).send_weekly_report(operations_run_id=run.id, now=_NOW)

    assert outcome.sent is True
    subject, body = notifier.sent[0]
    assert subject == "[BizFluxLab] Weekly Operations Report - 2026-09-23"
    assert "incomplete" not in subject
    assert "SYSTEM" in body and "ALERTS" in body


def test_a_partial_weekly_run_sends_an_incomplete_digest(session: Session) -> None:
    run = _run(session, profile="weekly", status="partial")
    _step(session, run, "import_ga4", "failed")
    notifier = _RecordingNotifier()

    outcome = _service(session, notifier).send_weekly_report(operations_run_id=run.id, now=_NOW)

    assert outcome.sent is True
    subject, body = notifier.sent[0]
    assert subject.startswith("[BizFluxLab][WARNING]")
    assert "incomplete" in subject
    assert "このレポートは不完全である" in body


def test_the_same_weekly_period_is_not_sent_twice(session: Session) -> None:
    """スケジューラの再実行で週次メールを二重に送らない。"""

    run = _run(session, profile="weekly", status="succeeded")
    notifier = _RecordingNotifier()
    service = _service(session, notifier)

    first = service.send_weekly_report(operations_run_id=run.id, now=_NOW)
    second = service.send_weekly_report(operations_run_id=run.id, now=_NOW)

    assert first.sent is True
    assert second.sent is False
    assert len(notifier.sent) == 1
    rows = session.scalars(select(NotificationDelivery)).all()
    assert [r.outcome for r in rows] == [DELIVERY_SENT, DELIVERY_DUPLICATE]


def test_notify_run_routes_by_profile(session: Session) -> None:
    weekly = _run(session, profile="weekly", status="succeeded")
    notifier = _RecordingNotifier()

    outcome = _service(session, notifier).notify_run(operations_run_id=weekly.id, now=_NOW)

    assert outcome.notification_type == NOTIFICATION_WEEKLY_REPORT
    assert outcome.sent is True


# -- delivery history ----------------------------------------------------------
def test_delivery_history_is_recorded_without_secrets(session: Session) -> None:
    run = _run(session, status="failed")
    notifier = _RecordingNotifier()

    _service(session, notifier).send_daily_incident(operations_run_id=run.id, now=_NOW)

    row = session.scalars(select(NotificationDelivery)).one()
    assert row.notification_type == NOTIFICATION_DAILY_INCIDENT
    assert row.outcome == DELIVERY_SENT
    assert row.operations_run_id == run.id
    assert row.channel == "email"
    assert row.recipient_hint == "1 recipient(s) @example.com"
    # 生アドレスも password も残らない。
    assert "owner@example.com" not in repr(row.__dict__)
    assert _PASSWORD not in repr(row.__dict__)
    assert len(row.recipient_fingerprint) == 64


def test_a_failed_delivery_is_recorded_and_can_be_retried(session: Session) -> None:
    run = _run(session, profile="weekly", status="succeeded")
    failing = _RecordingNotifier(delivered=False)

    outcome = _service(session, failing).send_weekly_report(operations_run_id=run.id, now=_NOW)

    assert outcome.sent is False
    row = session.scalars(select(NotificationDelivery)).one()
    assert row.outcome == DELIVERY_FAILED
    assert row.error_category == "TimeoutError"
    # 失敗は dedupe key を占有しない -- 修正後に人が送り直せる。
    assert row.dedupe_key is None

    working = _RecordingNotifier()
    retried = _service(session, working).send_weekly_report(operations_run_id=run.id, now=_NOW)
    assert retried.sent is True


def test_an_unconfigured_provider_skips_without_failing(session: Session) -> None:
    class _Disabled(_Settings):
        operations_email_enabled = False

    run = _run(session, status="failed")
    service = OperationsNotificationService(session, settings=_Disabled(), notifier=None)

    outcome = service.send_daily_incident(operations_run_id=run.id, now=_NOW)

    assert outcome.sent is False
    assert "ENABLED is false" in outcome.reason
    row = session.scalars(select(NotificationDelivery)).one()
    # 未設定は失敗ではない。
    assert row.outcome == DELIVERY_SKIPPED


def test_a_missing_run_is_reported_not_raised(session: Session) -> None:
    outcome = _service(session).send_daily_incident(operations_run_id=999, now=_NOW)

    assert outcome.sent is False
    assert "not found" in outcome.reason


# -- no side effects -----------------------------------------------------------
def test_notification_performs_no_http(session: Session, monkeypatch) -> None:
    """メール通知は WordPress にも /go/ にも触れない。"""

    import httpx

    def _boom(*args, **kwargs):  # pragma: no cover
        raise AssertionError("notifications must not perform HTTP requests")

    monkeypatch.setattr(httpx, "Client", _boom)
    monkeypatch.setattr(httpx, "get", _boom)
    monkeypatch.setattr(httpx, "post", _boom)

    run = _run(session, profile="weekly", status="succeeded")
    outcome = _service(session).send_weekly_report(operations_run_id=run.id, now=_NOW)

    assert outcome.sent is True


def test_unit_tests_never_open_a_real_smtp_connection(session: Session, monkeypatch) -> None:
    import smtplib

    def _boom(*args, **kwargs):  # pragma: no cover
        raise AssertionError("tests must never contact a real SMTP server")

    monkeypatch.setattr(smtplib, "SMTP", _boom)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _boom)

    run = _run(session, status="failed")
    outcome = _service(session).send_daily_incident(operations_run_id=run.id, now=_NOW)

    assert outcome.sent is True


@pytest.mark.parametrize("profile", ["daily", "weekly"])
def test_notification_never_approves_or_applies_a_change(session: Session, profile) -> None:
    """通知は記事にも change_* にも触れない。"""

    from app.models import ChangeApplication, ChangeRequest

    run = _run(session, profile=profile, status="partial")
    _service(session).notify_run(operations_run_id=run.id, now=_NOW)

    assert session.scalars(select(ChangeRequest)).all() == []
    assert session.scalars(select(ChangeApplication)).all() == []
