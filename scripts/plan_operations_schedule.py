"""管理用 CLI: Windows タスクスケジューラ登録の **計画のみ** を表示する (C8)。

    uv run python scripts/plan_operations_schedule.py

このコマンドは **タスクを登録しない**。登録に使うコマンドをそのまま表示するので、
内容を確認したうえで人が実行する。自動登録は明示的な承認を要する操作であり、
ここでは行わない。

生成される定義に secret は含まれない。認証情報は既存の ``.env`` 読み込み経路で
プロセス内から解決される。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.settings import get_settings  # noqa: E402
from app.operations.policy import get_policy  # noqa: E402
from app.operations.windows_scheduler import (  # noqa: E402
    build_task_plans,
    contains_secret,
)

EXIT_OK = 0
EXIT_UNSAFE = 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", help="uv の絶対パス (既定は PATH から解決)")
    parser.add_argument("--json", dest="json_path", help="計画を JSON で書き出すパス")
    args = parser.parse_args(argv)

    settings = get_settings()
    policy = get_policy()
    root = Path(__file__).resolve().parent.parent
    plans = build_task_plans(policy=policy, project_root=root, executable=args.executable)

    secrets = [
        str(value)
        for value in (
            getattr(settings, "wordpress_app_password", None),
            getattr(settings, "make_api_token", None),
            getattr(settings, "affiliate_runtime_shared_secret", None),
            getattr(settings, "operations_webhook_url", None),
        )
        if value
    ]

    print("=== windows task scheduler PLAN (nothing is registered) ===")
    print(f"policy_version = {policy.policy_version}")
    print(f"timezone       = {policy.timezone_name} (task scheduler uses the OS local time)")
    unsafe = False
    for plan in plans:
        print(f"\n--- {plan.task_name} ---")
        print(f"  profile        = {plan.profile}")
        print(
            f"  schedule       = {plan.schedule} at {plan.start_time}"
            f"{f' on {plan.day_of_week}' if plan.day_of_week else ''}"
        )
        print(f"  working dir    = {plan.working_directory}")
        print(f"  executable     = {plan.executable}")
        print(f"  log            = {plan.log_path}")
        if contains_secret(plan, secrets=secrets):
            unsafe = True
            print("  !! refusing: the generated definition would contain a secret")
            continue
        print("  command to run (review before executing):")
        print("    " + " ".join(_quote(part) for part in plan.command))

    print("\nnothing was registered. run the command above yourself to register a task.")
    print("to remove one later: schtasks /Delete /TN <task name> /F")

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps([p.as_dict() for p in plans], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        print(f"\nwrote {args.json_path}")
    return EXIT_UNSAFE if unsafe else EXIT_OK


def _quote(part: str) -> str:
    return f'"{part}"' if " " in part and not part.startswith('"') else part


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
