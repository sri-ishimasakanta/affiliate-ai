"""Windows タスクスケジューラ用の定義生成 (C8.6、pure)。

この module は **タスクを登録しない**。登録に使う `schtasks` の引数を組み立てて
返すだけで、実行するかどうかは人が決める。

**C8.6 で直した 2 点:**

1. 引用符の入れ子。以前は ``/TR`` にコマンド全体を書いていたため
   ``/TR "cmd.exe /c cd /d "D:\\Projects\\affiliate-ai" && ..."`` のように
   二重引用符が入れ子になり、PowerShell でも cmd.exe でも途中で引数が切れた。
   いまは **リポジトリ内のランチャ** (``scripts/run_operations_task.cmd``) を
   スケジュールし、``/TR`` は「ランチャのパス + プロファイル名」だけにした。
   入れ子の引用符が構造的に発生しない。
2. 日曜の二重実行。weekly は daily の全ステップに URL Inspection を足したもの
   なので、日曜に daily を走らせる意味が無い。daily を MON-SAT、weekly を SUN に
   分け、スケジューラ設定だけで表現する (runner に曜日の分岐を入れない)。

出力は **Windows PowerShell にそのまま貼れる形** にする。PowerShell の
単一引用符文字列はリテラルで、1 つの引数として native exe に渡るため、空白を
含むパス (``C:\\Program Files\\...``) も安全に運べる。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

#: スケジュールするランチャ (リポジトリ内)。
LAUNCHER_RELATIVE_PATH = "scripts/run_operations_task.cmd"


@dataclass(frozen=True)
class ScheduledTaskPlan:
    """1 つのタスク定義。``powershell_command`` をそのまま貼れば登録できる。"""

    task_name: str
    profile: str
    schedule: str
    days: str
    start_time: str
    working_directory: str
    executable: str
    launcher: str
    #: schtasks が受け取る ``/TR`` の値 (1 つの論理引数)。
    task_action: str
    log_path: str
    run_level: str
    requires_logged_on_user: bool
    arguments: list[str] = field(default_factory=list)

    @property
    def powershell_command(self) -> str:
        """PowerShell にそのまま貼れる 1 行。

        先頭のコマンド名は **引用しない** -- PowerShell では引用符で始まる行は
        コマンドではなく文字列式として評価されてしまうため。以降の引数だけを
        単一引用符で包む。
        """

        head, *rest = self.arguments
        return " ".join([head, *(_powershell_quote(part) for part in rest)])

    def as_dict(self) -> dict:
        return {
            "task_name": self.task_name,
            "profile": self.profile,
            "schedule": self.schedule,
            "days": self.days,
            "start_time": self.start_time,
            "working_directory": self.working_directory,
            "executable": self.executable,
            "launcher": self.launcher,
            "task_action": self.task_action,
            "log_path": self.log_path,
            "run_level": self.run_level,
            "requires_logged_on_user": self.requires_logged_on_user,
            "arguments": self.arguments,
            "powershell_command": self.powershell_command,
        }


def _powershell_quote(value: str) -> str:
    """PowerShell の単一引用符文字列に包む (中の ``'`` は ``''`` で退避)。

    単一引用符文字列はリテラルなので、``\\`` も空白も ``&`` も解釈されない。
    native exe には 1 つの引数としてそのまま渡る。
    """

    return "'" + value.replace("'", "''") + "'"


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
    run_level = str(scheduler.get("run_level") or "LIMITED")
    launcher = str(root / LAUNCHER_RELATIVE_PATH).replace("/", "\\")
    uv = resolve_executable(executable)

    specs = (
        (
            "daily",
            str(scheduler.get("daily_days") or "MON,TUE,WED,THU,FRI,SAT"),
            str(scheduler.get("daily_at") or "06:30"),
        ),
        (
            "weekly",
            str(scheduler.get("weekly_day") or "SUN"),
            str(scheduler.get("weekly_at") or "07:30"),
        ),
    )

    plans: list[ScheduledTaskPlan] = []
    for profile, days, start_time in specs:
        # /TR は「ランチャ + プロファイル」だけ。入れ子の引用符が生じない。
        task_action = f"{launcher} {profile}"
        task_name = f"{task_prefix}-operations-{profile}"
        arguments = [
            "schtasks",
            "/Create",
            "/TN",
            task_name,
            "/TR",
            task_action,
            # 曜日を指定するため daily/weekly とも WEEKLY スケジュールを使う。
            "/SC",
            "WEEKLY",
            "/D",
            days,
            "/ST",
            start_time,
            "/RL",
            run_level,
            "/F",
        ]
        plans.append(
            ScheduledTaskPlan(
                task_name=task_name,
                profile=profile,
                schedule="WEEKLY",
                days=days,
                start_time=start_time,
                working_directory=str(root),
                executable=uv,
                launcher=launcher,
                task_action=task_action,
                log_path=str(Path(log_directory) / f"operations-{profile}.log"),
                run_level=run_level,
                # /RU を渡さない = 対話トークンで動く。つまりログオン中のみ実行。
                # SYSTEM 化も資格情報の埋め込みもしない (人が UI で選ぶこと)。
                requires_logged_on_user=True,
                arguments=arguments,
            )
        )
    return plans


def task_action_of(arguments: list[str]) -> str | None:
    """``/TR`` に渡る値を取り出す (1 つの論理引数であることの検証用)。"""

    try:
        return arguments[arguments.index("/TR") + 1]
    except (ValueError, IndexError):
        return None


def schedules_overlap(plans: list[ScheduledTaskPlan]) -> bool:
    """同じ曜日に 2 つ以上のタスクが走る設定になっていないか。"""

    seen: set[str] = set()
    for plan in plans:
        days = {d.strip().upper() for d in plan.days.split(",") if d.strip()}
        if days & seen:
            return True
        seen |= days
    return False


def contains_secret(plan: ScheduledTaskPlan, *, secrets: list[str]) -> bool:
    """定義に secret が紛れ込んでいないかの検査 (登録前の最終確認用)。"""

    blob = " ".join(plan.arguments) + plan.powershell_command + plan.task_action
    return any(secret and secret in blob for secret in secrets)
