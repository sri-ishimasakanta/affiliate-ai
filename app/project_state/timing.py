"""時間で決まる状態 (pure。時刻は呼び出し側が渡す。テストでは固定する)。

あいまいな言い方 (「そのうち」) で決めない。システムの記録の時刻と、スケジューラが示す実行の
時刻から決める。待たない (次の実行の時刻まで眠らない): 報告を作った時点で一番新しい記録を使う。

- 毎日の運用: ``latest_success`` / ``latest_partial`` / ``latest_failed`` / ``running``。
  最後の実行がうまくいかなかったとき、その次の予定の実行 (+ 猶予 45 分) までは ``not_due``、
  過ぎても新しい記録が無ければ ``due``、6 時間を過ぎれば ``overdue``。スケジューラが最後の記録の
  後に既に動いたのに新しい記録が無ければ、その時刻から数える。新しい成功の実行があれば、前の
  うまくいかなかった実行は ``superseded`` (警告にしない)。
- 週の報告: ``not_scheduled`` (タスクが無い) / ``not_yet_due`` (まだ 1 度も予定の時刻に
  なっていない) / ``ok`` / ``missing`` (予定の時刻に動いたのに記録が無い)。
- Threads の成績の診断: ``not_due`` / ``due`` / ``overdue`` (新しく 6h を過ぎた公開が 3 本
  以上で due、6 本以上か、報告が 7 日より古く新しい公開もあれば overdue)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

DAILY_GRACE = timedelta(minutes=45)
DAILY_OVERDUE = timedelta(hours=6)
DIAGNOSTIC_DUE = 3
DIAGNOSTIC_OVERDUE = 6
DIAGNOSTIC_MAX_AGE = timedelta(days=7)
_RESULT_NEVER_RAN = 267011  # 0x41303 (Get-ScheduledTaskInfo: has not run yet)
_STATE_BY_STATUS = {
    "succeeded": "latest_success",
    "partial": "latest_partial",
    "running": "running",
}


def _parse(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).replace("Z", "+00:00")
    if "." in text:  # .NET の 7 桁の小数を 6 桁に
        head, _, tail = text.partition(".")
        digits = tail[: len(tail) - len(tail.lstrip("0123456789"))]
        rest = tail[len(digits) :]
        text = f"{head}.{digits[:6]}{rest}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def daily_state(runs: Sequence[Mapping], task: Mapping | None, *, now: datetime) -> dict:
    """``runs`` は新しい順の daily の実行 (id・status・effective_date・started_at・finished_at)。"""

    task = task or {}
    next_run = _parse(task.get("next_run"))
    if not runs:
        return {"state": "no_runs", "follow_up": "not_due", "next_expected_run": _iso(next_run)}
    latest = runs[0]
    state = _STATE_BY_STATUS.get(latest.get("status"), "latest_failed")
    success = next((r for r in runs if r.get("status") == "succeeded"), None)
    superseded = (
        [r["id"] for r in runs[1:] if r.get("status") != "succeeded"]
        if state == "latest_success"
        else []
    )
    out = {
        "state": state,
        "latest_run": {k: latest.get(k) for k in ("id", "status", "effective_date", "finished_at")},
        "latest_success_run_id": success["id"] if success else None,
        "superseded_non_success_run_ids": superseded,
        "next_expected_run": _iso(next_run),
    }
    if state in ("latest_success", "running"):
        return {**out, "follow_up": "none", "check_after": None}
    # 最後の記録の後に予定の実行が既にあったなら、その時刻から数える。
    started = _parse(latest.get("started_at"))
    last_scheduled = _parse(task.get("last_run"))
    if last_scheduled and started and last_scheduled > started + DAILY_GRACE:
        expected = last_scheduled
    else:
        expected = next_run
    if expected is None:
        follow = "unknown"
    elif now < expected + DAILY_GRACE:
        follow = "not_due"
    elif now < expected + DAILY_OVERDUE:
        follow = "due"
    else:
        follow = "overdue"
    return {
        **out,
        "follow_up": follow,
        "expected_superseding_run": _iso(expected),
        "check_after": _iso(expected + DAILY_GRACE) if expected else None,
    }


def weekly_state(runs: Sequence[Mapping], task: Mapping | None, *, now: datetime) -> dict:
    if not task or task.get("exists") is False:
        return {"state": "not_scheduled", "reason": "no affiliate-ai-operations-weekly task"}
    last_scheduled = _parse(task.get("last_run"))
    never_ran = task.get("last_result") == _RESULT_NEVER_RAN or (
        last_scheduled is not None and last_scheduled.year < 2000
    )
    latest = runs[0] if runs else None
    out = {"next_expected_run": _iso(_parse(task.get("next_run"))), "latest_run": latest}
    if never_ran or last_scheduled is None:
        return {**out, "state": "not_yet_due"}
    started = _parse((latest or {}).get("started_at"))
    if started is None or started + DAILY_GRACE < last_scheduled:
        if now < last_scheduled + DAILY_GRACE:
            return {**out, "state": "not_yet_due"}
        return {
            **out,
            "state": "missing",
            "reason": "the task ran but no weekly run is recorded for that run",
        }
    return {**out, "state": "ok"}


def diagnostic_state(performance: Mapping, *, now: datetime) -> dict:
    newly = list(performance.get("newly_past_6h") or [])
    generated = _parse(performance.get("generated_at"))
    old = bool(generated and now - generated > DIAGNOSTIC_MAX_AGE)
    if performance.get("status") == "unavailable" and not performance.get("generated_at"):
        state = "due"
    elif len(newly) >= DIAGNOSTIC_OVERDUE or (old and newly):
        state = "overdue"
    elif len(newly) >= DIAGNOSTIC_DUE or old:
        state = "due"
    else:
        state = "not_due"
    return {
        "state": state,
        "newly_past_6h": newly,
        "needed_for_due": DIAGNOSTIC_DUE,
        "needed_for_overdue": DIAGNOSTIC_OVERDUE,
        "report_older_than_max_age": old,
    }


def build_timing(state: Mapping, *, now: datetime) -> dict:
    analytics = state.get("analytics") or {}
    tasks = (state.get("scheduler") or {}).get("tasks") or {}
    performance = (state.get("threads") or {}).get("performance") or {}
    return {
        "as_of": now.isoformat(timespec="seconds"),
        "daily": daily_state(
            analytics.get("recent_daily_runs") or [],
            tasks.get("affiliate-ai-operations-daily"),
            now=now,
        ),
        "weekly": weekly_state(
            analytics.get("recent_weekly_runs") or [],
            tasks.get("affiliate-ai-operations-weekly"),
            now=now,
        ),
        "diagnostic": diagnostic_state(performance, now=now),
        "rules": {
            "daily_grace_minutes": int(DAILY_GRACE.total_seconds() // 60),
            "daily_overdue_hours": int(DAILY_OVERDUE.total_seconds() // 3600),
            "diagnostic_due_newly_past_6h": DIAGNOSTIC_DUE,
            "diagnostic_overdue_newly_past_6h": DIAGNOSTIC_OVERDUE,
            "diagnostic_max_age_days": DIAGNOSTIC_MAX_AGE.days,
        },
    }
