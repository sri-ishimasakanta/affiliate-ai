"""OperationsRunner / OperationsAlertService の統合テスト (C8)。

すべてのステップを注入で差し替えて、**実ネットワーク・WordPress・``/go/`` に一切
触れずに** オーケストレーションの契約を検証する。

pin する契約:

- 依存順どおりに実行し、依存元が失敗したステップは理由付きで skip する。
- 取り込み 1 つの失敗で他の成功を巻き戻さない (partial)。
- ロックが取れなければ「成功」ではなく skip として記録する。
- 通知の失敗は run を partial にするが、取り込み結果は有効なまま。
- 同じ fingerprint のアラートは 1 行にまとめ、cooldown 中は再通知しない。
- ``--execute`` 相当でも記事・WordPress には一切書き込まない。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Article,
    OperationsAlert,
    OperationsRun,
    OperationsStepRun,
)
from app.operations.monitoring import AlertDraft, fingerprint
from app.operations.notifications import NotificationResult
from app.operations.policy import OperationsPolicy, load_policy
from app.services.operations_alert_service import OperationsAlertService
from app.services.operations_runner_service import (
    DAILY_STEPS,
    STEP_AFFILIATE_CLICKS,
    STEP_MONITORING,
    STEP_REVENUE_CANDIDATES,
    STEP_SEARCH_CONSOLE,
    STEP_SEO_CANDIDATES,
    OperationsRunner,
    StepOutcome,
)

_NOW = datetime(2026, 9, 23, 6, 30, tzinfo=UTC)


class _Settings:
    wordpress_base_url = "https://bizfluxlab.com"
    search_console_property_uri = "sc-domain:bizfluxlab.com"
    search_console_credentials_file = None
    ga4_property_id = None
    operations_webhook_url = None


def _policy(**over) -> OperationsPolicy:
    raw = dict(load_policy().raw)
    for section, values in over.items():
        raw[section] = {**raw.get(section, {}), **values}
    return OperationsPolicy(policy_version="test", raw=raw)


def _factory(session: Session):
    """同一セッションを使い回す factory (テスト用の in-memory DB を共有する)。"""

    class _Scoped:
        def __call__(self):
            return self

        def __enter__(self):
            return session

        def __exit__(self, *_exc):
            return False

    return _Scoped()


def _ok(name: str, **result):
    def handler(**_kwargs):
        return StepOutcome(step_name=name, status="succeeded", result=result or {})

    return handler


def _fail(name: str, exc: Exception):
    def handler(**_kwargs):
        raise exc

    return handler


def _all_ok(extra: dict | None = None) -> dict:
    overrides = {name: _ok(name) for name in DAILY_STEPS}
    overrides[STEP_MONITORING] = _ok(
        STEP_MONITORING, notification_failures=0, alerts_evaluated=0, alerts_recorded=0
    )
    overrides.update(extra or {})
    return overrides


def _runner(session: Session, *, overrides=None, policy=None) -> OperationsRunner:
    return OperationsRunner(
        _factory(session),
        settings=_Settings(),
        policy=policy or _policy(),
        step_overrides=overrides if overrides is not None else _all_ok(),
    )


# ==================== plan ====================================================
def test_plan_lists_steps_without_touching_anything(session: Session) -> None:
    outcome = _runner(session).plan(profile="daily", now=_NOW)

    assert outcome.plan_only is True
    assert [s.step_name for s in outcome.steps] == list(DAILY_STEPS)
    assert outcome.run_id is None
    assert session.scalars(select(OperationsRun)).all() == []
    assert any("/go/" in note for note in outcome.notes)


def test_weekly_plan_enables_url_inspection(session: Session) -> None:
    daily = _runner(session).plan(profile="daily", now=_NOW)
    weekly = _runner(session).plan(profile="weekly", now=_NOW)

    assert daily.step("check_indexability").result["inspect"] is False
    assert weekly.step("check_indexability").result["inspect"] is True


# ==================== execute =================================================
def test_daily_pipeline_runs_every_step_in_order(session: Session) -> None:
    outcome = _runner(session).execute(profile="daily", now=_NOW)

    assert outcome.status == "succeeded"
    assert [s.step_name for s in outcome.steps] == list(DAILY_STEPS)
    run = session.scalars(select(OperationsRun)).one()
    assert run.profile == "daily"
    assert run.status == "succeeded"
    assert run.step_succeeded == len(DAILY_STEPS)
    assert run.effective_date.isoformat() == "2026-09-23"
    assert run.timezone_name == "Asia/Tokyo"


def test_weekly_pipeline_runs(session: Session) -> None:
    outcome = _runner(session).execute(profile="weekly", now=_NOW)
    assert outcome.status == "succeeded"
    assert session.scalars(select(OperationsRun)).one().profile == "weekly"


def test_every_step_is_persisted(session: Session) -> None:
    _runner(session).execute(profile="daily", now=_NOW)
    steps = session.scalars(select(OperationsStepRun)).all()
    assert {s.step_name for s in steps} == set(DAILY_STEPS)


def test_a_failed_import_makes_the_run_partial_not_failed(session: Session) -> None:
    overrides = _all_ok({STEP_SEARCH_CONSOLE: _fail(STEP_SEARCH_CONSOLE, RuntimeError("boom"))})
    outcome = _runner(session, overrides=overrides).execute(profile="daily", now=_NOW)

    assert outcome.status == "partial"
    failed = outcome.step(STEP_SEARCH_CONSOLE)
    assert failed.status == "failed"
    assert failed.error_category == "RuntimeError"
    # 他の取り込みは成功したまま (巻き戻さない)。
    assert outcome.step(STEP_AFFILIATE_CLICKS).status == "succeeded"


def test_dependent_step_is_skipped_with_a_reason(session: Session) -> None:
    overrides = _all_ok({STEP_SEARCH_CONSOLE: _fail(STEP_SEARCH_CONSOLE, RuntimeError("boom"))})
    outcome = _runner(session, overrides=overrides).execute(profile="daily", now=_NOW)

    skipped = outcome.step(STEP_SEO_CANDIDATES)
    assert skipped.status == "skipped"
    assert STEP_SEARCH_CONSOLE in skipped.skip_reason
    # 依存していない方は動く。
    assert outcome.step(STEP_REVENUE_CANDIDATES).status == "succeeded"


def test_all_steps_failing_makes_the_run_failed(session: Session) -> None:
    overrides = {name: _fail(name, RuntimeError("x")) for name in DAILY_STEPS}
    outcome = _runner(session, overrides=overrides).execute(profile="daily", now=_NOW)
    assert outcome.status == "failed"


def test_monitoring_notification_failure_makes_the_run_partial(session: Session) -> None:
    overrides = _all_ok(
        {
            STEP_MONITORING: _ok(
                STEP_MONITORING, notification_failures=0, alerts_evaluated=0, alerts_recorded=0
            )
        }
    )

    def partial_monitoring(**_kwargs):
        return StepOutcome(
            step_name=STEP_MONITORING,
            status="partial",
            result={"notification_failures": 1},
        )

    overrides[STEP_MONITORING] = partial_monitoring
    outcome = _runner(session, overrides=overrides).execute(profile="daily", now=_NOW)

    assert outcome.status == "partial"
    # 取り込みは成功のまま (通知の失敗で巻き戻さない)。
    assert outcome.step(STEP_SEARCH_CONSOLE).status == "succeeded"


# ==================== locking =================================================
def test_overlapping_run_is_skipped_not_succeeded(session: Session) -> None:
    from app.operations.lock import OperationsLockService

    OperationsLockService(session).acquire(owner_run_id=999, now=_NOW)

    outcome = _runner(session).execute(profile="daily", now=_NOW)

    assert outcome.status == "skipped"
    assert outcome.lock_conflict is True
    assert outcome.blocking_owner_run_id == 999
    # 実際のステップは動かない。
    assert outcome.steps == []
    run = session.scalars(select(OperationsRun)).one()
    assert run.status == "skipped"
    assert "holds the lock" in run.failure_summary


def test_lock_is_released_after_a_successful_run(session: Session) -> None:
    _runner(session).execute(profile="daily", now=_NOW)
    second = _runner(session).execute(profile="daily", now=_NOW + timedelta(minutes=1))
    assert second.status == "succeeded"
    assert second.lock_conflict is False


def test_lock_is_released_even_when_a_step_fails(session: Session) -> None:
    overrides = {name: _fail(name, RuntimeError("x")) for name in DAILY_STEPS}
    _runner(session, overrides=overrides).execute(profile="daily", now=_NOW)

    second = _runner(session).execute(profile="daily", now=_NOW + timedelta(minutes=1))
    assert second.lock_conflict is False


def test_stale_lock_is_reclaimed_and_recorded(session: Session) -> None:
    from app.operations.lock import OperationsLockService

    OperationsLockService(session).acquire(owner_run_id=999, now=_NOW - timedelta(hours=5))

    outcome = _runner(session).execute(profile="daily", now=_NOW)

    assert outcome.status == "succeeded"
    assert outcome.reclaimed_stale_lock is True
    assert any("stale" in note for note in outcome.notes)


# ==================== safety ==================================================
def test_run_does_not_mutate_articles(session: Session) -> None:
    article = Article(
        id=1,
        title="t",
        slug="s",
        keyword_id=None,
        body="本文",
        status="published",
        published_url="https://bizfluxlab.com/s/",
    )
    session.add(article)
    session.commit()
    before = (article.body, article.status, article.published_url)

    _runner(session).execute(profile="daily", now=_NOW)

    session.refresh(article)
    assert (article.body, article.status, article.published_url) == before


def test_runner_has_no_wordpress_or_go_surface() -> None:
    """runner のソースに WordPress 書き込みや /go/ 参照が無いことを固定する。"""

    from pathlib import Path

    source = Path("app/services/operations_runner_service.py").read_text(encoding="utf-8")
    assert "wordpress_publication" not in source
    assert "update_post_content_exact" not in source
    assert "/go/" in source  # 「呼ばない」と明記したコメントのみ
    assert "httpx" not in source


# ==================== alerts ==================================================
class _CountingNotifier:
    name = "counting"

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[str] = []
        self._fail = fail

    def send(self, message):
        self.sent.append(message.fingerprint)
        return NotificationResult(self.name, not self._fail, "test")


def _draft(*, severity="error", key="a") -> AlertDraft:
    return AlertDraft(
        alert_type="IMPORT_FAILURE",
        severity=severity,
        source="test",
        title="t",
        summary="s",
        fingerprint=fingerprint("IMPORT_FAILURE", key),
        evidence={"k": 1},
    )


def test_new_alert_is_recorded_and_notified(session: Session) -> None:
    notifier = _CountingNotifier()
    outcome = OperationsAlertService(
        session, policy=_policy(), notifiers=[notifier]
    ).record_and_notify([_draft()], now=_NOW)

    assert outcome.new_alerts == 1
    assert outcome.notified == 1
    assert len(notifier.sent) == 1


def test_same_alert_is_deduplicated_into_one_row(session: Session) -> None:
    notifier = _CountingNotifier()
    service = OperationsAlertService(session, policy=_policy(), notifiers=[notifier])
    service.record_and_notify([_draft()], now=_NOW)
    service.record_and_notify([_draft()], now=_NOW + timedelta(hours=1))

    rows = session.scalars(select(OperationsAlert)).all()
    assert len(rows) == 1
    assert rows[0].occurrence_count == 2


def test_cooldown_suppresses_repeat_notification(session: Session) -> None:
    notifier = _CountingNotifier()
    service = OperationsAlertService(session, policy=_policy(), notifiers=[notifier])
    service.record_and_notify([_draft()], now=_NOW)
    outcome = service.record_and_notify([_draft()], now=_NOW + timedelta(hours=1))

    assert outcome.suppressed_by_cooldown == 1
    assert len(notifier.sent) == 1


def test_notification_resumes_after_the_cooldown(session: Session) -> None:
    notifier = _CountingNotifier()
    service = OperationsAlertService(session, policy=_policy(), notifiers=[notifier])
    service.record_and_notify([_draft()], now=_NOW)
    service.record_and_notify([_draft()], now=_NOW + timedelta(hours=25))

    assert len(notifier.sent) == 2


def test_low_severity_is_recorded_but_not_notified(session: Session) -> None:
    notifier = _CountingNotifier()
    outcome = OperationsAlertService(
        session, policy=_policy(), notifiers=[notifier]
    ).record_and_notify([_draft(severity="info")], now=_NOW)

    assert outcome.recorded == 1
    assert outcome.suppressed_by_severity == 1
    assert notifier.sent == []


def test_notification_failure_still_keeps_the_alert(session: Session) -> None:
    notifier = _CountingNotifier(fail=True)
    outcome = OperationsAlertService(
        session, policy=_policy(), notifiers=[notifier]
    ).record_and_notify([_draft()], now=_NOW)

    assert outcome.notification_failures == 1
    assert outcome.notified == 0
    assert len(session.scalars(select(OperationsAlert)).all()) == 1


def test_alert_can_be_acknowledged_and_resolved(session: Session) -> None:
    service = OperationsAlertService(session, policy=_policy(), notifiers=[])
    service.record_and_notify([_draft()], now=_NOW)
    key = fingerprint("IMPORT_FAILURE", "a")

    assert service.acknowledge(key) is True
    assert service.active_alerts() == []
    assert service.resolve(key) is True


def test_recurrence_reopens_a_resolved_alert(session: Session) -> None:
    service = OperationsAlertService(session, policy=_policy(), notifiers=[])
    service.record_and_notify([_draft()], now=_NOW)
    service.resolve(fingerprint("IMPORT_FAILURE", "a"))

    service.record_and_notify([_draft()], now=_NOW + timedelta(days=2))

    row = session.scalars(select(OperationsAlert)).one()
    assert row.status == "open"
    assert row.resolved_at is None
