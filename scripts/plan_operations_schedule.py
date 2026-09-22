"""管理用 CLI: Windows タスクスケジューラ登録の **計画のみ** を表示する (C8.6)。

    uv run python scripts/plan_operations_schedule.py

このコマンドは **タスクを登録しない**。登録に使うコマンドをそのまま表示するので、
内容を確認したうえで人が実行する。自動登録は明示的な承認を要する操作であり、
ここでは行わない。ログ用ディレクトリも **作らない** -- 不足していれば前提条件と
して報告するだけ。

出力するコマンドは Windows PowerShell にそのまま貼れる形にしてある。実処理は
リポジトリ内のランチャ ``scripts/run_operations_task.cmd`` に委ねているので、
``/TR`` に引用符が入れ子になることはない。

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
    schedules_overlap,
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
    print(f"timezone       = {policy.timezone_name} (task scheduler fires on the OS local clock)")
    print(f"launcher       = {plans[0].launcher}")
    print(f"executable     = {plans[0].executable}")
    print(
        "run as         = the user who registers the task "
        "(no /RU, so it runs only while that user is logged on)"
    )
    print(f"run level      = {plans[0].run_level} (no administrator rights required)")
    print("secrets        = none in the task definition; credentials stay in .env")

    # -- 前提条件: ログ用ディレクトリは作らず、無ければ報告するだけ ------------
    print("\n--- prerequisites ---")
    missing: list[str] = []
    for plan in plans:
        log_directory = Path(plan.log_path).parent
        exists = log_directory.is_dir()
        print(f"  log directory {log_directory}: {'present' if exists else 'MISSING'}")
        if not exists and str(log_directory) not in missing:
            missing.append(str(log_directory))
        break
    launcher = Path(plans[0].launcher)
    print(f"  launcher {launcher}: {'present' if launcher.is_file() else 'MISSING'}")
    for directory in missing:
        print(f"  create it first:  New-Item -ItemType Directory -Force '{directory}'")

    print(f"\n--- schedule (overlap: {'YES' if schedules_overlap(plans) else 'none'}) ---")
    for plan in plans:
        print(f"  {plan.profile:7} {plan.days:28} {plan.start_time}")

    unsafe = False
    for plan in plans:
        print(f"\n--- {plan.task_name} ---")
        print(f"  profile        = {plan.profile}")
        print(f"  days           = {plan.days}")
        print(f"  start time     = {plan.start_time}")
        print(f"  working dir    = {plan.working_directory} (set by the launcher)")
        print(f"  task action    = {plan.task_action}")
        print(f"  log            = {plan.log_path} (stdout and stderr, appended)")
        if contains_secret(plan, secrets=secrets):
            unsafe = True
            print("  !! refusing: the generated definition would contain a secret")
            continue
        print("  PowerShell command to run (review before executing):")
        print(f"    {plan.powershell_command}")

    print("\nnothing was registered. run the command above yourself to register a task.")
    print("to inspect afterwards: schtasks /Query /TN affiliate-ai-operations-daily")
    print("to remove one later:   schtasks /Delete /TN affiliate-ai-operations-daily /F")

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps([p.as_dict() for p in plans], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        print(f"\nwrote {args.json_path}")
    return EXIT_UNSAFE if unsafe else EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
