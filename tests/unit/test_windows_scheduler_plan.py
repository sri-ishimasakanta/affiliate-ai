"""app.operations.windows_scheduler の単体テスト (C8)。

pin する要点:

- **タスクを登録しない**。組み立てた定義を返すだけ。
- 定義に secret を埋め込まない (認証情報は .env 経路でプロセス内から解決する)。
- 対話シェルを前提にせず、作業ディレクトリとログ出力先を明示する。
"""

from __future__ import annotations

from app.operations.policy import load_policy
from app.operations.windows_scheduler import (
    build_task_plans,
    contains_secret,
    resolve_executable,
)

_POLICY = load_policy()
_ROOT = "D:/Projects/affiliate-ai"


def _plans():
    return build_task_plans(policy=_POLICY, project_root=_ROOT, executable="C:/tools/uv.exe")


def test_both_profiles_are_planned() -> None:
    plans = _plans()
    assert [p.profile for p in plans] == ["daily", "weekly"]


def test_daily_task_uses_the_daily_schedule() -> None:
    daily = _plans()[0]
    assert daily.schedule == "DAILY"
    assert daily.day_of_week is None
    assert "--profile daily" in daily.arguments
    assert "--execute" in daily.arguments
    assert "--trigger scheduler" in daily.arguments


def test_weekly_task_declares_a_day() -> None:
    weekly = _plans()[1]
    assert weekly.schedule == "WEEKLY"
    assert weekly.day_of_week
    assert "/D" in weekly.command


def test_working_directory_is_the_project_root() -> None:
    daily = _plans()[0]
    assert daily.working_directory.replace("\\", "/").endswith("affiliate-ai")
    assert "cd /d" in daily.arguments


def test_an_explicit_executable_is_used_rather_than_an_interactive_shell() -> None:
    daily = _plans()[0]
    assert daily.executable == "C:/tools/uv.exe"
    assert "C:/tools/uv.exe" in daily.arguments


def test_output_is_redirected_to_a_log_file() -> None:
    daily = _plans()[0]
    assert daily.log_path.endswith("operations-daily.log")
    assert ">>" in daily.arguments
    assert "2>&1" in daily.arguments


def test_the_plan_only_creates_a_schtasks_command() -> None:
    daily = _plans()[0]
    assert daily.command[0] == "schtasks"
    assert "/Create" in daily.command
    # 破壊的な操作は含まない。
    assert "/Delete" not in daily.command
    assert "/Run" not in daily.command


def test_no_secret_is_embedded() -> None:
    daily = _plans()[0]
    assert contains_secret(daily, secrets=["super-secret-token"]) is False


def test_a_secret_would_be_detected_if_present() -> None:
    daily = _plans()[0]
    assert contains_secret(daily, secrets=[daily.working_directory]) is True


def test_executable_falls_back_without_raising() -> None:
    assert resolve_executable("C:/x/uv.exe") == "C:/x/uv.exe"
    assert resolve_executable(None)


def test_task_names_are_distinct_and_prefixed() -> None:
    names = [p.task_name for p in _plans()]
    assert len(set(names)) == 2
    assert all(name.startswith("affiliate-ai-operations-") for name in names)
