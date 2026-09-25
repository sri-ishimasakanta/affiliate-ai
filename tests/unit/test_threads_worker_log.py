"""常駐 Threads worker のログ行 (T4.3、観測できるようにする)。

pin する契約:

- 起動したら 1 行目にすぐ出す (JST・常駐・ポリシー・有効な機能・自動公開の状態・pid)。
- 仕事の実行ごとに 1 行。health と queue の観測は状態が変わったときだけ。
- 承認の取り込み: 「確認、決定なし」「N 件反映」「失敗」を区別する。
- 指標: 「取り込んだ」「期限前」「失敗 + 分類」を区別する。
- 停止・ロックの喪失・二重起動をはっきり出す。
- token・capability・承認 URL・パスワード・追跡 URL・/go/ の token を出さない。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.services.threads_worker_log import WorkerLogFormatter, sanitize

_JST = ZoneInfo("Asia/Tokyo")
_NOW = datetime(2026, 9, 25, 1, 0, tzinfo=UTC)  # 10:00 JST


def _formatter() -> WorkerLogFormatter:
    return WorkerLogFormatter(timezone=_JST)


def _event(name: str, summary: dict, **extra) -> dict:
    return {
        "event": "subsystem",
        "at": _NOW,
        "name": name,
        "summary": summary,
        "next_run_at": _NOW + timedelta(minutes=5),
        **extra,
    }


# == startup ===================================================================
def test_the_startup_line_says_what_is_running() -> None:
    line = _formatter().startup(
        now=_NOW,
        policy_version="t4.3",
        capabilities={
            "collect_insights": True,
            "sync_approvals": True,
            "send_approval_digests": False,
            "auto_publish_flag": False,
            "auto_publish_policy": False,
            "publish": False,
        },
    )
    assert line.startswith("2026-09-25T10:00:00+0900 threads-worker INFO event=started")
    assert "mode=resident" in line
    assert "policy=t4.3" in line
    assert "capabilities=collect_insights,sync_approvals" in line
    assert "auto_publish_policy=disabled" in line
    assert "can_publish=False" in line
    assert "pid=" in line


# == subsystems ================================================================
@pytest.mark.parametrize(
    ("summary", "expected", "level"),
    [
        ({"fetched": 0, "applied": 0, "skipped": 0, "failed": 0}, "checked, no decisions", "INFO"),
        ({"fetched": 2, "applied": 2, "skipped": 0, "failed": 0}, "applied 2 decision(s)", "INFO"),
        ({"fetched": 0, "applied": 0, "skipped": 0, "failed": 1}, "result=failed", "WARN"),
    ],
)
def test_approval_sync_outcomes_are_distinguished(summary, expected, level) -> None:
    line = _formatter().format(_event("approval_sync", summary))
    assert "event=approval_sync" in line
    assert expected in line
    assert f"threads-worker {level}" in line


def test_insights_imported_not_due_and_failed_are_distinguished() -> None:
    formatter = _formatter()
    imported = formatter.format(
        _event(
            "insights_refresh",
            {
                "refreshed": [{"publication_id": 2, "result": "imported"}],
                "network_calls": 1,
            },
        )
    )
    assert 'result="#2:imported"' in imported or "result=#2:imported" in imported

    not_due = formatter.format(_event("insights_refresh", {"refreshed": [], "would_refresh": []}))
    assert 'result="not due"' in not_due

    failed = formatter.format(
        _event(
            "insights_refresh",
            {
                "refreshed": [
                    {
                        "publication_id": 2,
                        "result": "failed",
                        "category": "threads_permission",
                        "reason": "/18095684012104774/insights failed: API access blocked.",
                    }
                ],
                "network_calls": 1,
            },
        )
    )
    assert "threads-worker WARN" in failed
    assert "failed[threads_permission]" in failed
    assert "API access blocked." in failed


def test_health_and_queue_are_logged_only_when_they_change() -> None:
    formatter = _formatter()
    ready = {"threads_state": "ready", "config_issues": []}
    assert formatter.format(_event("health", ready)) is not None
    assert formatter.format(_event("health", ready)) is None
    assert formatter.format(_event("health", {"threads_state": "misconfigured"})) is not None

    queue = {"approved": 2, "approved_unpublished": 0, "awaiting_approval": 1}
    assert formatter.format(_event("queue_observation", queue)) is not None
    assert formatter.format(_event("queue_observation", queue)) is None
    assert formatter.format(_event("queue_observation", {**queue, "awaiting_approval": 2}))


def test_every_approval_sync_run_is_logged() -> None:
    """承認の取り込みは「確認した」ことそのものが生存の証拠なので、毎回出す。"""

    formatter = _formatter()
    summary = {"fetched": 0, "applied": 0, "skipped": 0, "failed": 0}
    assert formatter.format(_event("approval_sync", summary)) is not None
    assert formatter.format(_event("approval_sync", summary)) is not None


def test_a_problem_in_automatic_publication_is_logged_as_an_error() -> None:
    line = _formatter().format(
        _event(
            "publication_evaluation",
            {
                "blockers": [],
                "next_candidate_id": 5,
                "auto_publish": {"outcome": "uncertain", "publication_id": 3, "threads_writes": 2},
            },
        )
    )
    assert "threads-worker ERROR" in line
    assert "auto_publish=uncertain" in line


# == lifecycle =================================================================
def test_lock_and_shutdown_outcomes_are_explicit() -> None:
    formatter = _formatter()
    assert "event=already_running" in formatter.format({"event": "already_running", "at": _NOW})
    assert "ERROR event=lock_lost" in formatter.format({"event": "lock_lost", "at": _NOW})
    stopped = formatter.format(
        {"event": "stopped", "at": _NOW, "reason": "lost_lock", "exit_code": 5, "cycles": 3}
    )
    assert "ERROR event=stopped" in stopped
    assert "reason=lost_lock" in stopped
    assert "exit_code=5" in stopped
    failed = formatter.format(
        {"event": "subsystem_failed", "at": _NOW, "name": "insights_refresh", "error": "boom"}
    )
    assert "WARN event=subsystem_failed" in failed


# == secrets ===================================================================
@pytest.mark.parametrize(
    "secret",
    [
        "https://bizfluxlab.com/bfl-approval/abc#Zx8qP2vN5tR7yK1mW3eH9jL4uB6oC0dF",
        "https://bizfluxlab.com/go/aff_9f8e7d6c5b4a",
        "access_token=THAAAsecretvalue123",
        "password=hunter2",
        "/go/aff_token123",
        "Zx8qP2vN5tR7yK1mW3eH9jL4uB6oC0dFgH2k",
    ],
)
def test_secrets_never_reach_a_log_line(secret: str) -> None:
    line = _formatter().format(
        {"event": "subsystem_failed", "at": _NOW, "name": "approval_sync", "error": secret}
    )
    assert secret not in line
    for fragment in ("THAAA", "hunter2", "aff_", "Zx8qP2vN5tR7"):
        if fragment in secret:
            assert fragment not in line


def test_numeric_ids_stay_readable() -> None:
    assert sanitize("media 18095684012104774 publication 2") == (
        "media 18095684012104774 publication 2"
    )
