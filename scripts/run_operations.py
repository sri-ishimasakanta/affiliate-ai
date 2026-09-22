"""管理用 CLI: 定期運用パイプライン (C8)。

    # 何を実行するかだけ表示する (既定。通信も書き込みもしない)
    uv run python scripts/run_operations.py --profile daily

    # 実行する
    uv run python scripts/run_operations.py --profile daily --execute

    # 週次 (URL Inspection を含む深いチェック)
    uv run python scripts/run_operations.py --profile weekly --execute

    # 直近の運用 run を見る / アラートを見る
    uv run python scripts/run_operations.py --show-last
    uv run python scripts/run_operations.py --list-alerts

    # アラートの状態を変える (DB を手で編集しないため)
    uv run python scripts/run_operations.py --acknowledge <fingerprint>
    uv run python scripts/run_operations.py --resolve <fingerprint>

**このコマンドは WordPress に書き込まない。記事本文を変更しない。**
アフィリエイト URL を作らず、``/go/<token>`` へのリクエストも一切行わない
(そのリクエスト自体がクリック計測を汚すため)。

閾値・ウィンドウ・通知条件は ``app/config/operations_policy.json`` にある。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config.database import SessionLocal  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.models import OperationsRun, OperationsStepRun  # noqa: E402
from app.operations.policy import get_policy  # noqa: E402
from app.services.operations_alert_service import OperationsAlertService  # noqa: E402
from app.services.operations_runner_service import OperationsRunner  # noqa: E402

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_FAILED = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="daily", choices=("daily", "weekly"))
    parser.add_argument("--execute", action="store_true", help="実際に実行する")
    parser.add_argument("--trigger", default="manual", help="manual / scheduler など")
    parser.add_argument("--json", dest="json_path", help="結果を JSON で書き出すパス")
    parser.add_argument("--show-last", action="store_true", help="直近の運用 run を表示する")
    parser.add_argument("--list-alerts", action="store_true", help="未解決のアラートを表示する")
    parser.add_argument("--acknowledge", help="この fingerprint のアラートを確認済みにする")
    parser.add_argument("--resolve", help="この fingerprint のアラートを解決済みにする")
    args = parser.parse_args(argv)

    settings = get_settings()
    policy = get_policy()

    if args.show_last:
        return _show_last()
    if args.list_alerts:
        return _list_alerts(policy)
    if args.acknowledge or args.resolve:
        return _change_alert(policy, args.acknowledge, args.resolve)

    runner = OperationsRunner(SessionLocal, settings=settings, policy=policy)
    if not args.execute:
        outcome = runner.plan(profile=args.profile)
    else:
        outcome = runner.execute(profile=args.profile, trigger=args.trigger)

    print(f"=== operations ({'PLAN' if not args.execute else 'EXECUTE'}) ===")
    print(f"profile          = {outcome.profile}")
    print(f"policy_version   = {outcome.policy_version}")
    print(f"effective_date   = {outcome.effective_date} ({outcome.timezone_name})")
    print(f"run_id           = {outcome.run_id}")
    print(f"status           = {outcome.status}")
    if outcome.lock_conflict:
        print(f"lock conflict    = run {outcome.blocking_owner_run_id} holds the pipeline lock")
    if outcome.reclaimed_stale_lock:
        print("lock             = reclaimed a stale lock from a dead run")

    print("\n--- steps ---")
    for step in outcome.steps:
        detail = ""
        if step.status == "skipped" and step.skip_reason:
            detail = f" ({step.skip_reason})"
        elif step.status == "failed":
            detail = f" ({step.error_category}: {step.error_message})"
        elif step.rows_received is not None:
            detail = f" received={step.rows_received} changed={step.rows_changed}"
        print(f"  {step.step_name:28} {step.status:10}{detail}")

    for note in outcome.notes:
        print(f"\nnote: {note}")

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(outcome.as_dict(), ensure_ascii=False, indent=1, default=str),
            encoding="utf-8",
        )
        print(f"\nwrote {args.json_path}")

    if not args.execute:
        print("\nplan only: no network call, no database write. pass --execute to run.")
        return EXIT_OK
    if outcome.status == "failed":
        return EXIT_FAILED
    return EXIT_PARTIAL if outcome.status in ("partial", "skipped") else EXIT_OK


def _show_last() -> int:
    with SessionLocal() as session:
        run = session.scalars(
            select(OperationsRun).order_by(OperationsRun.id.desc()).limit(1)
        ).first()
        if run is None:
            print("no operations run has been recorded yet")
            return EXIT_OK
        print(
            f"run {run.id}: profile={run.profile} status={run.status} "
            f"effective_date={run.effective_date} trigger={run.trigger}"
        )
        print(f"  policy={run.policy_version} schema={run.schema_version} git={run.git_revision}")
        print(
            f"  steps: total={run.step_total} ok={run.step_succeeded} "
            f"failed={run.step_failed} skipped={run.step_skipped}"
        )
        if run.failure_summary:
            print(f"  failures: {run.failure_summary}")
        for step in session.scalars(
            select(OperationsStepRun).where(OperationsStepRun.operations_run_id == run.id)
        ).all():
            print(
                f"    {step.step_name:28} {step.status:10} "
                f"received={step.rows_received} changed={step.rows_changed}"
            )
    return EXIT_OK


def _list_alerts(policy) -> int:
    with SessionLocal() as session:
        alerts = OperationsAlertService(session, policy=policy).active_alerts()
        if not alerts:
            print("no open alerts")
            return EXIT_OK
        for alert in alerts:
            print(
                f"[{alert.severity:7}] {alert.alert_type:34} x{alert.occurrence_count} "
                f"last_seen={alert.last_seen_at}"
            )
            print(f"    {alert.title}")
            print(f"    fingerprint={alert.fingerprint}")
    return EXIT_OK


def _change_alert(policy, acknowledge: str | None, resolve: str | None) -> int:
    with SessionLocal() as session:
        service = OperationsAlertService(session, policy=policy)
        if acknowledge:
            print("acknowledged" if service.acknowledge(acknowledge) else "no open alert matched")
        if resolve:
            print("resolved" if service.resolve(resolve) else "no alert matched")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
