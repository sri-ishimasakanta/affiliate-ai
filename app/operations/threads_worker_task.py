"""常駐 Threads worker のタスクスケジューラ定義 (T4.3、**計画のみ**)。

この module は **タスクを登録しない**。``schtasks`` の引数を組み立てるだけで、
登録するかどうかは人が決める (C8.6 と同じ方針)。

タスクの役割は「常駐 worker を起動し、落ちていたら立ち上げ直す」ことであって、
「決まった時刻に投稿する cron」ではない。投稿の時刻は worker が状態から決める。

- ``/SC MINUTE /MO 15``: 15 分おきに起動を試みる。worker が動いていれば、
  2 つ目はロックで即座に終わる (終了コード 4)。止まっていれば、ここで復帰する。
- ``/TR`` は「ランチャ + プロファイル」だけ (入れ子の引用符を作らない)。
- ``/RU`` を渡さない = ログオン中のユーザーで動く。資格情報を埋め込まない。
- 秘密情報は定義に入らない (``contains_secret`` で確認する)。
"""

from __future__ import annotations

from pathlib import Path

from app.operations.windows_scheduler import ScheduledTaskPlan, resolve_executable

LAUNCHER_RELATIVE_PATH = "scripts/run_threads_worker_task.cmd"
TASK_NAME = "affiliate-ai-threads-worker"
PROFILES = ("observe", "operate", "publish")
DEFAULT_PROFILE = "observe"
RECOVERY_INTERVAL_MINUTES = 15


def build_threads_worker_task_plan(
    *,
    project_root: Path | str,
    profile: str = DEFAULT_PROFILE,
    log_directory: str = "D:\\Logs\\affiliate-ai",
    run_level: str = "LIMITED",
    executable: str | None = None,
) -> ScheduledTaskPlan:
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; expected one of {', '.join(PROFILES)}")
    root = Path(project_root).resolve()
    launcher = str(root / LAUNCHER_RELATIVE_PATH).replace("/", "\\")
    task_action = f"{launcher} {profile}"
    arguments = [
        "schtasks",
        "/Create",
        "/TN",
        TASK_NAME,
        "/TR",
        task_action,
        "/SC",
        "MINUTE",
        "/MO",
        str(RECOVERY_INTERVAL_MINUTES),
        "/RL",
        run_level,
        "/F",
    ]
    return ScheduledTaskPlan(
        task_name=TASK_NAME,
        profile=profile,
        schedule="MINUTE",
        days="-",
        start_time=f"every {RECOVERY_INTERVAL_MINUTES} min (recovery only)",
        working_directory=str(root),
        executable=resolve_executable(executable),
        launcher=launcher,
        task_action=task_action,
        log_path=str(Path(log_directory) / "threads-worker.log"),
        run_level=run_level,
        requires_logged_on_user=True,
        arguments=arguments,
    )


__all__ = [
    "DEFAULT_PROFILE",
    "LAUNCHER_RELATIVE_PATH",
    "PROFILES",
    "RECOVERY_INTERVAL_MINUTES",
    "TASK_NAME",
    "build_threads_worker_task_plan",
]
