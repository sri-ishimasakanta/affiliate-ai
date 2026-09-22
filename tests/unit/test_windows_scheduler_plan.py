"""app.operations.windows_scheduler と起動ランチャの単体テスト (C8.6)。

C8.4 のスケジューラ計画には 2 つの欠陥があった。ここはその回帰テスト。

1. ``/TR`` にコマンド全体を書いたため二重引用符が入れ子になり、PowerShell でも
   cmd.exe でも引数が途中で切れた。いまはリポジトリ内のランチャを
   スケジュールし、``/TR`` は「ランチャ + プロファイル」だけにしている。
2. 日曜に daily と weekly が両方走る計画になっていた。weekly は daily の全ステップ
   に URL Inspection を足したものなので、同じ日に両方走らせる意味が無い。

いずれも **タスクを登録せずに** 検証する。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from app.operations.policy import OperationsPolicy, load_policy
from app.operations.windows_scheduler import (
    LAUNCHER_RELATIVE_PATH,
    build_task_plans,
    contains_secret,
    resolve_executable,
    schedules_overlap,
    task_action_of,
)

_POLICY = load_policy()
_ROOT = Path(__file__).resolve().parents[2]
_UV_WITH_SPACES = r"C:\Program Files\Python312\Scripts\uv.EXE"


def _plans(root=_ROOT, policy=_POLICY):
    return build_task_plans(policy=policy, project_root=root, executable=_UV_WITH_SPACES)


def _policy(**over) -> OperationsPolicy:
    raw = dict(_POLICY.raw)
    for section, values in over.items():
        raw[section] = {**raw.get(section, {}), **values}
    return OperationsPolicy(policy_version="test", raw=raw)


# ==================== /TR quoting =============================================
def test_task_action_is_one_logical_argument() -> None:
    """``/TR`` の値は 1 つの引数であり、引用符を含まない。"""

    for plan in _plans():
        action = task_action_of(plan.arguments)
        assert action == plan.task_action
        assert '"' not in action
        assert "'" not in action


def test_task_action_is_launcher_plus_profile_only() -> None:
    for plan in _plans():
        assert plan.task_action == f"{plan.launcher} {plan.profile}"
        # 以前の壊れた形 (cmd.exe /c ... && ...) に戻っていないこと。
        assert "cmd.exe" not in plan.task_action
        assert "&&" not in plan.task_action


def test_no_nested_double_quotes_anywhere_in_the_command() -> None:
    for plan in _plans():
        assert '"' not in plan.powershell_command


def test_powershell_command_starts_with_an_unquoted_command_name() -> None:
    """先頭を引用すると PowerShell は文字列式として評価してしまう。"""

    for plan in _plans():
        assert plan.powershell_command.startswith("schtasks '")


def test_arguments_with_spaces_are_single_quoted() -> None:
    plan = _plans()[0]
    assert f"'{plan.task_action}'" in plan.powershell_command


def test_paths_with_spaces_survive_generation() -> None:
    spaced_root = Path(r"C:\Program Files\affiliate ai")
    plans = build_task_plans(policy=_POLICY, project_root=spaced_root, executable=_UV_WITH_SPACES)
    for plan in plans:
        assert "Program Files" in plan.launcher
        assert f"'{plan.task_action}'" in plan.powershell_command
        assert '"' not in plan.powershell_command


def test_single_quotes_in_a_path_are_escaped() -> None:
    plans = build_task_plans(
        policy=_POLICY, project_root=Path(r"C:\it's here\app"), executable="uv"
    )
    command = plans[0].powershell_command
    # PowerShell の単一引用符文字列では '' がリテラルの ' を表す。
    assert "it''s here" in command


def test_powershell_parses_the_task_action_as_one_argument() -> None:
    """実際の PowerShell に通して argv を確認する (タスクは登録しない)。"""

    if sys.platform != "win32":  # pragma: no cover - Windows 以外では検証しない
        return
    plan = _plans()[0]
    # schtasks の代わりに argv を出力するだけのプローブを呼ぶ (タスクは登録しない)。
    probe = _ROOT / "tests" / "support" / "echo_argv.py"
    assert probe.is_file()
    quoted = plan.powershell_command.split(" ", 1)[1]
    script = f"& '{sys.executable}' '{probe}' {quoted}"
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    import json

    argv = json.loads(result.stdout.strip().splitlines()[-1])
    assert argv[argv.index("/TR") + 1] == plan.task_action


# ==================== schedule ================================================
def test_daily_excludes_sunday() -> None:
    daily = _plans()[0]
    assert daily.profile == "daily"
    assert "SUN" not in daily.days
    assert daily.days == "MON,TUE,WED,THU,FRI,SAT"


def test_weekly_runs_on_sunday() -> None:
    weekly = _plans()[1]
    assert weekly.profile == "weekly"
    assert weekly.days == "SUN"


def test_daily_and_weekly_never_share_a_day() -> None:
    assert schedules_overlap(_plans()) is False


def test_overlap_is_detected_when_it_exists() -> None:
    overlapping = _policy(scheduler={"daily_days": "MON,SUN", "weekly_day": "SUN"})
    assert schedules_overlap(_plans(policy=overlapping)) is True


def test_both_tasks_use_a_weekly_schedule_with_explicit_days() -> None:
    for plan in _plans():
        assert plan.schedule == "WEEKLY"
        assert "/D" in plan.arguments
        assert plan.arguments[plan.arguments.index("/D") + 1] == plan.days


def test_start_times_come_from_policy() -> None:
    daily, weekly = _plans()
    assert daily.start_time == "06:30"
    assert weekly.start_time == "07:30"


# ==================== execution context =======================================
def test_profile_is_passed_to_the_launcher() -> None:
    for plan in _plans():
        assert plan.task_action.endswith(f" {plan.profile}")


def test_working_directory_is_the_project_root() -> None:
    for plan in _plans():
        assert Path(plan.working_directory) == _ROOT


def test_launcher_lives_in_the_repository() -> None:
    plan = _plans()[0]
    assert plan.launcher.endswith(LAUNCHER_RELATIVE_PATH.replace("/", "\\"))
    assert (_ROOT / LAUNCHER_RELATIVE_PATH).is_file()


def test_absolute_uv_executable_is_recorded() -> None:
    assert _plans()[0].executable == _UV_WITH_SPACES


def test_log_paths_are_per_profile() -> None:
    daily, weekly = _plans()
    assert daily.log_path.endswith("operations-daily.log")
    assert weekly.log_path.endswith("operations-weekly.log")
    assert daily.log_path != weekly.log_path


def test_tasks_run_at_limited_level_and_need_a_logged_on_user() -> None:
    for plan in _plans():
        assert plan.run_level == "LIMITED"
        assert plan.requires_logged_on_user is True
        # 資格情報を要求しない (/RU も /RP も渡さない)。
        assert "/RU" not in plan.arguments
        assert "/RP" not in plan.arguments


def test_plan_never_creates_or_runs_a_task() -> None:
    for plan in _plans():
        assert plan.arguments[0] == "schtasks"
        assert "/Create" in plan.arguments
        assert "/Delete" not in plan.arguments
        assert "/Run" not in plan.arguments
        assert "/Change" not in plan.arguments


def test_executable_resolution_never_raises() -> None:
    assert resolve_executable("C:/x/uv.exe") == "C:/x/uv.exe"
    assert resolve_executable(None)


# ==================== secrets =================================================
def test_no_secret_is_embedded() -> None:
    for plan in _plans():
        assert contains_secret(plan, secrets=["super-secret-token"]) is False


def test_a_secret_would_be_detected_if_present() -> None:
    plan = _plans()[0]
    assert contains_secret(plan, secrets=[plan.working_directory]) is True


def test_task_definition_holds_no_env_values() -> None:
    blob = " ".join(_plans()[0].arguments)
    for marker in ("MAKE_API_TOKEN", "WORDPRESS_APP_PASSWORD", "WEBHOOK", ".env"):
        assert marker not in blob


# ==================== launcher contract =======================================
def _launcher_text() -> str:
    return (_ROOT / LAUNCHER_RELATIVE_PATH).read_text(encoding="utf-8")


def test_launcher_is_ascii_only() -> None:
    """cmd.exe は OEM コードページで読むため、非 ASCII は解析を壊す。"""

    text = _launcher_text()
    assert text.isascii(), "the launcher must stay ASCII-only for cmd.exe"


def test_launcher_sets_the_project_directory() -> None:
    assert 'cd /d "%~dp0.."' in _launcher_text()


def test_launcher_appends_stderr_to_stdout_log() -> None:
    text = _launcher_text()
    assert ">>" in text
    assert "2>&1" in text


def test_launcher_propagates_the_child_exit_code() -> None:
    assert "exit /b %ERRORLEVEL%" in _launcher_text()


def test_launcher_rejects_an_unknown_profile() -> None:
    text = _launcher_text()
    assert 'if /I not "%PROFILE%"=="daily"' in text
    assert "exit /b 64" in text


def test_launcher_contains_no_secret_and_no_business_logic() -> None:
    """secret の「代入」も URL も持たないこと (散文中の語は対象外)。"""

    import re

    text = _launcher_text()
    # set "SOMETHING_TOKEN=..." のような代入が無いこと。
    assert not re.search(
        r'set\s+"?[A-Za-z_]*(PASSWORD|TOKEN|SECRET|WEBHOOK)[A-Za-z_]*=[^"\r\n]+',
        text,
        re.IGNORECASE,
    )
    # 外部エンドポイントを直接叩かない (URL も /go/ も持たない)。
    assert "http://" not in text
    assert "https://" not in text
    assert "/go/" not in text
    # 実処理は run_operations.py に委譲するだけ。
    assert "scripts\\run_operations.py" in text


def test_launcher_invokes_only_the_operations_cli() -> None:
    text = _launcher_text()
    assert "--profile %PROFILE% --execute --trigger scheduler" in text
    assert "publish" not in text
