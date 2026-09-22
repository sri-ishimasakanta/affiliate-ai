"""Windows タスクスケジューラ用の定義生成 (C8、pure)。

この module は **タスクを登録しない**。登録に使う `schtasks` コマンドと引数を
組み立てて返すだけで、実行するかどうかは人が決める。

Windows を選ぶ理由: この案件の開発・運用環境が Windows であり、`uv` と `.env` を
そのまま使える最小の常駐不要な仕組みだから。クラウド基盤は持ち込まない。

安全上の約束:

- 生成する定義に secret を一切埋め込まない。認証情報は既存の ``.env`` 読み込み
  経路からプロセス内で解決される。
- 対話シェルを前提にしない (``uv.exe`` の絶対パスを使う)。
- 標準出力/標準エラーはログファイルへ向ける (パスはポリシー設定)。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ScheduledTaskPlan:
    """1 つのタスク定義。``command`` をそのまま実行すれば登録できる。"""

    task_name: str
    profile: str
    schedule: str
    start_time: str
    day_of_week: str | None
    working_directory: str
    executable: str
    arguments: str
    log_path: str
    command: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "task_name": self.task_name,
            "profile": self.profile,
            "schedule": self.schedule,
            "start_time": self.start_time,
            "day_of_week": self.day_of_week,
            "working_directory": self.working_directory,
            "executable": self.executable,
            "arguments": self.arguments,
            "log_path": self.log_path,
            "command": self.command,
        }


def resolve_executable(explicit: str | None = None) -> str:
    """``uv`` の実体パス。見つからなければ素の ``uv`` を返す (人が直す)。"""

    if explicit:
        return explicit
    found = shutil.which("uv")
    return found or "uv"


def build_task_plans(
    *,
    policy,
    project_root: Path | str,
    executable: str | None = None,
    task_prefix: str = "affiliate-ai",
) -> list[ScheduledTaskPlan]:
    """daily / weekly の 2 つのタスク定義を組み立てる (登録はしない)。"""

    root = Path(project_root).resolve()
    scheduler = policy.section("scheduler")
    log_directory = str(scheduler.get("log_directory") or (root / "logs"))
    uv = resolve_executable(executable)
    plans: list[ScheduledTaskPlan] = []

    for profile, schedule, time_key, day in (
        ("daily", "DAILY", "daily_at", None),
        ("weekly", "WEEKLY", "weekly_at", scheduler.get("weekly_day", "SUN")),
    ):
        start_time = str(scheduler.get(time_key) or "06:30")
        log_path = str(Path(log_directory) / f"operations-{profile}.log")
        # cmd 経由で実行してリダイレクトを効かせる。対話シェルは前提にしない。
        inner = (
            f'"{uv}" run python scripts/run_operations.py '
            f"--profile {profile} --execute --trigger scheduler"
        )
        arguments = f'/c cd /d "{root}" && {inner} >> "{log_path}" 2>&1'
        task_name = f"{task_prefix}-operations-{profile}"
        command = [
            "schtasks",
            "/Create",
            "/TN",
            task_name,
            "/TR",
            f"cmd.exe {arguments}",
            "/SC",
            schedule,
            "/ST",
            start_time,
            "/RL",
            "LIMITED",
            "/F",
        ]
        if day:
            command += ["/D", str(day)]
        plans.append(
            ScheduledTaskPlan(
                task_name=task_name,
                profile=profile,
                schedule=schedule,
                start_time=start_time,
                day_of_week=day,
                working_directory=str(root),
                executable=uv,
                arguments=arguments,
                log_path=log_path,
                command=command,
            )
        )
    return plans


def contains_secret(plan: ScheduledTaskPlan, *, secrets: list[str]) -> bool:
    """定義に secret が紛れ込んでいないかの検査 (登録前の最終確認用)。"""

    blob = " ".join(plan.command) + plan.arguments
    return any(secret and secret in blob for secret in secrets)
