"""Windows のスケジュールされたタスクの状態 (読むだけ)。

``Get-ScheduledTask`` / ``Get-ScheduledTaskInfo`` を PowerShell で呼ぶだけ。タスクを作る・
変える・止める・動かすことは一切しない (worker の再起動もしない)。
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import datetime

from app.project_state.provenance import provenance, unavailable

TASKS = ("affiliate-ai-operations-daily", "affiliate-ai-threads-worker")
# 読むだけの PowerShell (Get-* だけ)。タスク名は固定の値だけを埋め込む。
_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$out = @()
foreach ($n in @(%NAMES%)) {
  $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
  if ($null -eq $t) { $out += [pscustomobject]@{ name = $n; exists = $false }; continue }
  $i = Get-ScheduledTaskInfo -TaskName $n
  $out += [pscustomobject]@{
    name = $n; exists = $true; state = [string]$t.State; enabled = [bool]$t.Settings.Enabled
    multiple_instances = [string]$t.Settings.MultipleInstances
    triggers = @($t.Triggers | ForEach-Object { [pscustomobject]@{
      kind = $_.CimClass.CimClassName; start = [string]$_.StartBoundary
      repetition = [string]$_.Repetition.Interval } })
    action = [string]$t.Actions[0].Execute; arguments = [string]$t.Actions[0].Arguments
    last_run = $i.LastRunTime.ToString('o'); last_result = [int64]$i.LastTaskResult
    next_run = $(if ($i.NextRunTime) { $i.NextRunTime.ToString('o') } else { $null })
  }
}
$out | ConvertTo-Json -Depth 5 -Compress
"""
# LastTaskResult の意味 (よく出るものだけ)。
RESULT_MEANINGS = {
    0: "success",
    1: "the run exited with code 1 (for operations: a partial or failed run)",
    267009: "running now (0x41301)",
    267011: "has not run yet (0x41303)",
    2147946720: "an instance was skipped because one is already running (0x800710E0, IgnoreNew)",
}

Reader = Callable[[], str]


def default_reader() -> str:
    names = ",".join(f"'{n}'" for n in TASKS)
    done = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            _SCRIPT.replace("%NAMES%", names),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if done.returncode != 0:
        raise RuntimeError("Get-ScheduledTask failed")
    return done.stdout


def summarize(raw: str, *, now: datetime) -> dict:
    data = json.loads(raw)
    rows = data if isinstance(data, list) else [data]
    tasks = {}
    for row in rows:
        warnings = []
        code = row.get("last_result")
        if row.get("exists") is False:
            warnings.append("task does not exist")
        elif not row.get("enabled"):
            warnings.append("task is disabled")
        if row.get("name") == "affiliate-ai-operations-daily" and code not in (0, None, 267009):
            warnings.append(f"last run result {code}: {RESULT_MEANINGS.get(code, 'non-zero')}")
        if row.get("name") == "affiliate-ai-threads-worker" and row.get("state") != "Running":
            warnings.append(f"worker task is {row.get('state')} (resident worker expected Running)")
        triggers = row.get("triggers") or []
        if isinstance(triggers, dict):
            triggers = [triggers]
        tasks[row["name"]] = {
            **{
                k: row.get(k)
                for k in (
                    "exists",
                    "state",
                    "enabled",
                    "multiple_instances",
                    "action",
                    "arguments",
                    "last_run",
                    "last_result",
                    "next_run",
                )
            },
            "last_result_meaning": RESULT_MEANINGS.get(code),
            "triggers": [
                f"{t.get('kind', '').replace('MSFT_Task', '')} from {t.get('start')}"
                + (f" every {t['repetition']}" if t.get("repetition") else "")
                for t in triggers
            ],
            "warnings": warnings,
        }
    return {
        "provenance": provenance(
            "local_system",
            kind="observed",
            status="ok",
            observed_at=now,
            freshness="fresh",
            detail="Get-ScheduledTask / Get-ScheduledTaskInfo (read-only)",
        ),
        "tasks": tasks,
        "changes_made": False,
    }


def collect_scheduler(reader: Reader | None, *, now: datetime) -> dict:
    try:
        raw = (reader or default_reader)()
        return summarize(raw, now=now)
    except Exception as exc:
        return {
            "provenance": unavailable("local_system", type(exc).__name__),
            "changes_made": False,
        }
