"""常駐 Threads worker のランチャとタスク定義 (T4.3、**登録はしない**)。

pin する契約:

- ランチャは ASCII だけ (cmd.exe は OEM コードページで読む)。秘密を含まない。
- プロファイルは observe / operate / publish の 3 つ。observe は投稿もメールもしない。
- publish プロファイルだけが --auto-publish を渡す (それでもポリシーが無効なら投稿しない)。
- ランチャは常駐モードの worker だけを起動する。ログは C8 と分ける。
- タスク定義は「起動と復帰」の 15 分おき。/TR はランチャ + プロファイルだけ。
- 計画の CLI はタスクを登録しない (subprocess を使わない)。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.operations.threads_worker_task import (
    PROFILES,
    TASK_NAME,
    build_threads_worker_task_plan,
)
from app.operations.windows_scheduler import contains_secret, task_action_of

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "scripts" / "run_threads_worker_task.cmd"


def _launcher() -> str:
    return LAUNCHER.read_text(encoding="ascii")


def _profile_flags(profile: str) -> str:
    match = re.search(rf'"%PROFILE%"=="{profile}" set "FLAGS=([^"]*)"', _launcher())
    assert match, profile
    return match.group(1)


def test_the_launcher_is_ascii_only() -> None:
    raw = LAUNCHER.read_bytes()
    assert all(byte < 128 for byte in raw)


def test_the_launcher_runs_only_the_resident_worker() -> None:
    text = _launcher()
    assert "scripts\\run_threads_worker.py --resident %FLAGS%" in text
    assert "threads-worker.log" in text
    assert "operations-" not in text.split("rem ====")[-1]  # C8 のログに混ぜない


def test_observe_neither_posts_nor_emails() -> None:
    flags = _profile_flags("observe")
    assert "--collect-insights" in flags and "--sync-approvals" in flags
    assert "--auto-publish" not in flags
    assert "--send-approval-digests" not in flags


def test_operate_adds_digests_but_never_posts() -> None:
    flags = _profile_flags("operate")
    assert "--send-approval-digests" in flags
    assert "--auto-publish" not in flags


def test_only_the_publish_profile_passes_auto_publish() -> None:
    assert "--auto-publish" in _profile_flags("publish")


def test_the_launcher_carries_no_secret_material() -> None:
    lowered = _launcher().lower()
    for marker in ("access_token", "password", "secret=", "thaa", "bearer"):
        assert marker not in lowered


@pytest.mark.parametrize("profile", PROFILES)
def test_the_task_plan_is_launcher_plus_profile_only(profile: str) -> None:
    plan = build_threads_worker_task_plan(project_root=ROOT, profile=profile, executable="uv")
    action = task_action_of(plan.arguments)
    assert action == f"{plan.launcher} {profile}"
    assert '"' not in action
    assert plan.task_name == TASK_NAME
    assert plan.arguments[plan.arguments.index("/SC") + 1] == "MINUTE"
    assert plan.arguments[plan.arguments.index("/MO") + 1] == "15"
    assert "/RU" not in plan.arguments  # 資格情報を埋め込まない
    assert plan.log_path.endswith("threads-worker.log")


def test_an_unknown_profile_is_refused() -> None:
    with pytest.raises(ValueError):
        build_threads_worker_task_plan(project_root=ROOT, profile="burst")


def test_a_secret_would_be_detected_in_the_plan() -> None:
    plan = build_threads_worker_task_plan(project_root=ROOT, profile="observe", executable="uv")
    assert contains_secret(plan, secrets=["TOPSECRET"]) is False
    assert contains_secret(plan, secrets=["affiliate-ai-threads"]) is True


def test_the_schedule_cli_registers_nothing(monkeypatch, capsys) -> None:
    import subprocess

    from scripts.plan_threads_worker_schedule import main

    def _refuse(*_args, **_kwargs):
        raise AssertionError("planning must never run schtasks")

    monkeypatch.setattr(subprocess, "run", _refuse)
    monkeypatch.setattr(subprocess, "Popen", _refuse)

    class _Settings:
        threads_access_token = "THAAAsecret-value"
        approval_relay_shared_secret = "relay-secret-value"
        affiliate_runtime_shared_secret = "runtime-secret-value"
        operations_email_password = "smtp-password-value"

    assert main(["--profile", "publish"], settings=_Settings()) == 0
    out = capsys.readouterr().out
    assert "PLAN ONLY" in out
    assert "posts nothing while the policy is disabled" in out
    for secret in ("THAAAsecret-value", "relay-secret-value", "smtp-password-value"):
        assert secret not in out


def test_the_launcher_runs_python_unbuffered() -> None:
    """ログの行が溜まらずに出るように (常駐モードは長く動き続ける)。"""

    expected = "run python -u scripts" + chr(92) + "run_threads_worker.py --resident"
    assert expected in _launcher()
