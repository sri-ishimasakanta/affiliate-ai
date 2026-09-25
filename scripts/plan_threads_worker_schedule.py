"""管理用 CLI: 常駐 Threads worker のタスク登録の **計画のみ** を表示する (T4.3)。

    uv run python scripts/plan_threads_worker_schedule.py
    uv run python scripts/plan_threads_worker_schedule.py --profile operate

このコマンドは **タスクを登録しない**。登録に使うコマンドをそのまま表示するので、
内容を確認したうえで人が実行する (C8 の plan_operations_schedule.py と同じ)。

タスクの役割は常駐 worker の起動と復帰だけで、投稿の cron ではない。
``publish`` プロファイルでも、ポリシー ``automatic_publication.enabled`` が False の
あいだは 1 件も投稿しない。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.settings import get_settings  # noqa: E402
from app.operations.policy import get_policy  # noqa: E402
from app.operations.threads_worker_task import (  # noqa: E402
    DEFAULT_PROFILE,
    PROFILES,
    RECOVERY_INTERVAL_MINUTES,
    build_threads_worker_task_plan,
)
from app.operations.windows_scheduler import contains_secret  # noqa: E402
from app.social.threads.policy import get_operations_policy  # noqa: E402

EXIT_OK = 0
EXIT_UNSAFE = 1


def main(argv: list[str] | None = None, *, settings=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES, default=DEFAULT_PROFILE)
    parser.add_argument("--json", dest="json_path", help="計画を JSON で書き出すパス")
    args = parser.parse_args(argv)

    settings = settings or get_settings()
    scheduler = get_policy().section("scheduler")
    plan = build_threads_worker_task_plan(
        project_root=Path(__file__).resolve().parent.parent,
        profile=args.profile,
        log_directory=str(scheduler.get("log_directory") or "D:\\Logs\\affiliate-ai"),
        run_level=str(scheduler.get("run_level") or "LIMITED"),
    )
    secrets = [
        str(getattr(settings, name, "") or "")
        for name in (
            "threads_access_token",
            "approval_relay_shared_secret",
            "affiliate_runtime_shared_secret",
            "operations_email_password",
        )
    ]
    if contains_secret(plan, secrets=[s for s in secrets if len(s) >= 6]):
        print("refused: the task definition would contain a secret")
        return EXIT_UNSAFE

    auto_policy = get_operations_policy().automatic_publication_enabled
    print("=== threads worker schedule (PLAN ONLY - nothing is registered) ===")
    print(f"task name       = {plan.task_name}")
    print(f"profile         = {plan.profile}")
    print(f"launcher        = {plan.launcher}")
    print(f"trigger         = every {RECOVERY_INTERVAL_MINUTES} min (recovery of a stopped worker)")
    print(f"run level       = {plan.run_level} (logged-on user; no stored credentials)")
    print(f"log             = {plan.log_path}")
    print(f"auto-publish    = policy {'ENABLED' if auto_policy else 'disabled'}")
    if args.profile == "publish" and not auto_policy:
        print("  note: the publish profile posts nothing while the policy is disabled")
    print("\nThis is NOT a posting schedule. A running worker makes the next start exit")
    print("immediately (DB lock, exit code 4). The worker never catches up a backlog.")
    print("\nAfter creating the task, set in Task Scheduler (not settable by schtasks /Create):")
    print("  - Settings > 'Run task as soon as possible after a scheduled start is missed'")
    print("  - Settings > 'If the task is already running': Do not start a new instance")
    print("\nTo register (run it yourself after review):")
    print(f"  {plan.powershell_command}")
    print(f"\nto inspect afterwards: schtasks /Query /TN {plan.task_name}")
    print(f"to remove it later:    schtasks /Delete /TN {plan.task_name} /F")

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(plan.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nJSON written to {args.json_path}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
