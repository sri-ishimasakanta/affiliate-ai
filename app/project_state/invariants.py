"""運用の不変条件 (機械で確かめる。pure)。

3 つの強さを分ける:

- ``hard``: 破れたら運用の約束が破れている (DB が head・公開の窓・承認メールの窓・間隔 120 分・
  1 回に 1 本・スケジュールの契約)。
- ``expected_state``: 今そうであるはずの状態 (T6 の本番の確認で決めた状態。自動公開 ON・
  在庫の保守 OFF・featured image 25/25・W2 のカテゴリ・中継の配備)。変わったら理由を確かめる。
- ``advisory``: 運用の目安 (1 日 3〜5 本・push 済み・診断が新しい)。外れても誤りではない。
  警告にも strict の失敗にもしない。

結果は ``pass`` / ``fail`` / ``unknown`` (読めなかった)。``severity`` は破れたときの重さ。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

LEVELS = ("hard", "expected_state", "advisory")
RESULTS = ("pass", "fail", "unknown")

PUBLICATION_WINDOW = {"start": "07:00", "end": "23:00"}
APPROVAL_WINDOW = {"start": "08:00", "end": "21:00"}
SOFT_MIN_GAP_MINUTES = 120
MAX_PUBLICATIONS_PER_CYCLE = 1
DAILY_ACTIVITY_TARGET = (3, 5)
# スケジュールの契約 (app/operations/windows_scheduler.py・threads_worker_task.py)。
# DaysOfWeek は日=1・月=2 … 土=64 のビットの和 (月〜土 = 126)。
SCHEDULER_CONTRACTS = {
    "affiliate-ai-operations-daily": {
        "action_endswith": "run_operations_task.cmd",
        "arguments": "daily",
        "start_time": "06:30",
        "days_of_week": 126,
    },
    "affiliate-ai-operations-weekly": {
        "action_endswith": "run_operations_task.cmd",
        "arguments": "weekly",
        "start_time": "07:30",
        "days_of_week": 1,
    },
    "affiliate-ai-threads-worker": {
        "action_endswith": "run_threads_worker_task.cmd",
        "arguments": "publish",
        "repetition": "PT15M",
    },
}


def _get(state: Mapping, *path):
    node = state
    for key in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def _inv(iid, level, description, *, expected, observed, check: Callable, severity, source):
    if observed is None:
        result = "unknown"
    else:
        result = "pass" if check(observed) else "fail"
    return {
        "id": iid,
        "level": level,
        "description": description,
        "expected": expected,
        "observed": observed,
        "result": result,
        "severity": "info" if level == "advisory" else severity,
        "enforced": level != "advisory",
        "source": source,
    }


def scheduler_contract_problems(name: str, task: Mapping | None) -> list[str] | None:
    """契約との違い (読めなければ None)。"""

    if task is None:
        return None
    contract = SCHEDULER_CONTRACTS[name]
    if task.get("exists") is False:
        return ["task does not exist"]
    problems = []
    if not task.get("enabled"):
        problems.append("disabled")
    if task.get("multiple_instances") not in (None, "IgnoreNew"):
        problems.append(f"multiple instances {task.get('multiple_instances')} (want IgnoreNew)")
    if not str(task.get("action") or "").endswith(contract["action_endswith"]):
        problems.append(f"action {task.get('action')}")
    if (task.get("arguments") or "").strip() != contract["arguments"]:
        problems.append(f"arguments {task.get('arguments')!r}")
    details = task.get("trigger_details") or []
    if "start_time" in contract and not any(
        d.get("start_time") == contract["start_time"] for d in details
    ):
        problems.append(f"no trigger at {contract['start_time']}")
    if "days_of_week" in contract:
        days = {d.get("days_of_week") for d in details if d.get("days_of_week") is not None}
        if days and contract["days_of_week"] not in days:
            problems.append(f"days of week {sorted(days)} (want {contract['days_of_week']})")
    if "repetition" in contract and not any(
        d.get("repetition") == contract["repetition"] for d in details
    ):
        problems.append(f"no {contract['repetition']} repetition")
    return problems


def evaluate(state: Mapping) -> list[dict]:
    policy = _get(state, "threads", "policy") or None
    worker = _get(state, "threads", "worker") or None
    db = state.get("database") or {}
    out = [
        _inv(
            "db-at-code-head",
            "hard",
            "the application database is at the Alembic head",
            expected=True,
            observed=db.get("db_at_code_head"),
            check=lambda v: v is True,
            severity="critical",
            source="alembic_version (read-only) + alembic.ini",
        ),
        _inv(
            "threads-publication-window",
            "hard",
            "publication may happen only inside 07:00–23:00",
            expected=PUBLICATION_WINDOW,
            observed=(policy or {}).get("publication_window"),
            check=lambda v: v == PUBLICATION_WINDOW,
            severity="high",
            source="threads_operations_policy.json",
        ),
        _inv(
            "threads-approval-window",
            "hard",
            "approval emails go out only inside 08:00–21:00",
            expected=APPROVAL_WINDOW,
            observed=(policy or {}).get("approval_notification_window"),
            check=lambda v: v == APPROVAL_WINDOW,
            severity="high",
            source="threads_operations_policy.json",
        ),
        _inv(
            "threads-soft-min-gap",
            "hard",
            "at least 120 minutes between publications",
            expected=SOFT_MIN_GAP_MINUTES,
            observed=(policy or {}).get("soft_min_gap_minutes"),
            check=lambda v: v == SOFT_MIN_GAP_MINUTES,
            severity="high",
            source="threads_operations_policy.json",
        ),
        _inv(
            "threads-one-publication-per-cycle",
            "hard",
            "at most one publication per worker cycle (no catch-up burst)",
            expected=MAX_PUBLICATIONS_PER_CYCLE,
            observed=(policy or {}).get("max_publications_per_cycle"),
            check=lambda v: v == MAX_PUBLICATIONS_PER_CYCLE,
            severity="critical",
            source="app/social/threads/queue.py MAX_PUBLICATIONS_PER_CYCLE",
        ),
    ]
    tasks = _get(state, "scheduler", "tasks")
    for name in SCHEDULER_CONTRACTS:
        problems = scheduler_contract_problems(name, (tasks or {}).get(name)) if tasks else None
        out.append(
            _inv(
                f"scheduler-contract-{name.removeprefix('affiliate-ai-')}",
                "hard",
                f"{name} matches its registered contract",
                expected=SCHEDULER_CONTRACTS[name],
                observed=None if problems is None else (problems or "matches"),
                check=lambda v: v == "matches",
                severity="high",
                source="Get-ScheduledTask (read-only)",
            )
        )
    fi = state.get("featured_images") or {}
    tax = state.get("taxonomy") or {}
    out += [
        _inv(
            "threads-automatic-publication-on",
            "expected_state",
            "automatic publication is ON (T6 production checkpoint)",
            expected=True,
            observed=(policy or {}).get("automatic_publication_enabled"),
            check=lambda v: v is True,
            severity="medium",
            source="threads_operations_policy.json",
        ),
        _inv(
            "threads-stock-maintenance-off",
            "expected_state",
            "resident proposal-stock maintenance stays OFF until the routine is decided",
            expected=False,
            observed=(worker or {}).get("stock_maintenance_enabled"),
            check=lambda v: v is False,
            severity="high",
            source="scripts/run_threads_worker_task.cmd",
        ),
        _inv(
            "threads-worker-running",
            "expected_state",
            "the resident worker holds the lock with a fresh heartbeat",
            expected=True,
            observed=(worker or {}).get("running"),
            check=lambda v: v is True,
            severity="medium",
            source="operations_locks heartbeat",
        ),
        _inv(
            "wordpress-featured-images-complete",
            "expected_state",
            "every published post has a featured image (W1)",
            expected="all published",
            observed=None
            if fi.get("with_featured_image") is None
            else f"{fi['with_featured_image']}/{fi.get('published')}",
            check=lambda v: fi.get("with_featured_image") == fi.get("published"),
            severity="medium",
            source="WordPress REST (read-only)",
        ),
        _inv(
            "wordpress-taxonomy-matches-plan",
            "expected_state",
            "live categories match the W2 plan",
            expected=True,
            observed=tax.get("matches_plan"),
            check=lambda v: v is True,
            severity="high",
            source="WordPress REST (read-only) + taxonomy_plan.py",
        ),
        _inv(
            "approvals-relay-deployed",
            "expected_state",
            "the T6.1 approval relay deployment is recorded",
            expected=True,
            observed=_get(state, "approvals", "relay_t6_1_deployment_recorded"),
            check=lambda v: v is True,
            severity="medium",
            source="relay README deployment record",
        ),
    ]
    low, high = DAILY_ACTIVITY_TARGET
    git = state.get("git") or {}
    diag = _get(state, "timing", "diagnostic", "state")
    out += [
        _inv(
            "threads-daily-activity-target",
            "advisory",
            "about 3–5 publications per day (advisory; not a quota and not a cap)",
            expected=f"{low}–{high} per day",
            observed=_get(state, "threads", "publications", "published_last_24h"),
            check=lambda v: low <= v <= high,
            severity="info",
            source="threads_publications (last 24h)",
        ),
        _inv(
            "git-pushed",
            "advisory",
            "local commits are pushed (a human decides when)",
            expected=0,
            observed=git.get("ahead"),
            check=lambda v: v == 0,
            severity="info",
            source="git rev-list",
        ),
        _inv(
            "threads-performance-diagnostic-fresh",
            "advisory",
            "the read-only performance diagnostic is not due",
            expected="not_due",
            observed=diag,
            check=lambda v: v == "not_due",
            severity="info",
            source="reports/threads_performance_diagnostic_latest.json",
        ),
    ]
    return out


def summary(results: list[dict]) -> dict:
    counts = {level: {r: 0 for r in RESULTS} for level in LEVELS}
    for item in results:
        counts[item["level"]][item["result"]] += 1
    return counts
