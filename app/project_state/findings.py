"""警告・既知の問題・次の行動 (状態からだけ導く。pure・決定論的)。

- 警告: ``id`` / ``severity`` / ``area`` / ``message`` / ``evidence`` / ``action_required`` /
  ``blocking``。同じ ``id`` は 1 つにまとめる。意図した設計 (在庫の保守を無効にしている、
  など) は誤りとして扱わず ``info`` にする。
- 警告の元: 食い違い (``drift``)・破れた不変条件 (``invariants``。advisory は警告にしない)・
  時間で決まる状態 (``timing``)・そのほかの観測。
- 次の行動: 止めるべき食い違い → 進行中の劣化 → 時刻の来た確認 → そのほか、の順 (優先度 →
  id)。時刻の来ていない確認は ``due: false`` と ``due_at`` を付ける。前提の済んでいない行動
  (C10・在庫の運用の決定) は出さないか、前提を付ける。解決済みの alert・意図した状態
  (在庫の保守 OFF・author の権限・media 99) を「直す」行動は出さない。外部に取り返しの
  つかない決定を自動でしない (本番に書く行動・人の確認が要る行動はそう明示する)。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def warning(wid, severity, area, message, *, evidence, action_required, blocking=False) -> dict:
    if severity not in SEVERITY_ORDER:
        raise ValueError(severity)
    return {
        "id": wid,
        "severity": severity,
        "area": area,
        "message": message,
        "evidence": evidence,
        "action_required": action_required,
        "blocking": blocking,
    }


def dedupe(items: Iterable[Mapping]) -> list[dict]:
    """同じ id は最初の 1 つだけ。重さ → id の順。"""

    seen: dict[str, dict] = {}
    for item in items:
        seen.setdefault(item["id"], dict(item))
    return sorted(seen.values(), key=lambda w: (SEVERITY_ORDER[w["severity"]], w["id"]))


def _get(state: Mapping, *path, default=None):
    node = state
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            return default
        node = node[key]
    return node


def build_warnings(state: Mapping) -> list[dict]:
    out = []
    git = state.get("git") or {}
    ahead = git.get("ahead")
    if isinstance(ahead, int) and ahead > 0:
        out.append(
            warning(
                "git-ahead-of-remote",
                "low",
                "git",
                f"local branch is {ahead} commit(s) ahead of {git.get('remote')} (not pushed)",
                evidence="git rev-list --left-right --count",
                action_required="none required (a human decides when to push)",
            )
        )
    if git.get("tracked_changes"):
        out.append(
            warning(
                "git-uncommitted-changes",
                "low",
                "git",
                "tracked files have uncommitted changes",
                evidence="git status --porcelain",
                action_required="review and commit or discard",
            )
        )
    quality = state.get("quality") or {}
    for name in ("ruff", "alembic_check", "git_diff_check", "pytest"):
        check = quality.get(name) or {}
        if check.get("ok") is False:
            out.append(
                warning(
                    f"quality-{name}-failing",
                    "high",
                    "quality",
                    f"{name} is failing: {check.get('summary')}",
                    evidence=name,
                    action_required="fix before any production phase",
                    blocking=True,
                )
            )
    if (quality.get("pytest") or {}).get("freshness") == "unverified":
        out.append(
            warning(
                "quality-tests-unverified",
                "info",
                "quality",
                "pytest was not run and has no record",
                evidence="reports/quality_latest.json",
                action_required="run with --with-tests",
            )
        )
    fi = state.get("featured_images") or {}
    if fi.get("without_featured_image"):
        out.append(
            warning(
                "wp-missing-featured-images",
                "medium",
                "featured_images",
                f"published posts without a featured image: {fi['without_featured_image']}",
                evidence="live WordPress",
                action_required="plan a featured-image batch",
            )
        )
    if (fi.get("media_99") or {}).get("exists"):
        out.append(
            warning(
                "wp-media-99-duplicate",
                "info",
                "featured_images",
                (
                    "media 99 is an unused W1.4 duplicate (optional cleanup; do not delete "
                    "automatically)"
                ),
                evidence="live WordPress media library",
                action_required="optional: a human deletes it in wp-admin",
            )
        )
    if state.get("taxonomy"):
        out.append(
            warning(
                "wp-api-user-author-role",
                "info",
                "wordpress",
                "the WordPress API user has the author role and cannot create categories "
                "(by design)",
                evidence="docs/operations/taxonomy-w2.md §9.1",
                action_required="create new categories manually in wp-admin; do not broaden the "
                "role",
            )
        )
    money = state.get("monetization") or {}
    missing = money.get("missing_tracking") or []
    if missing:
        out.append(
            warning(
                "monetization-missing-tracking",
                "medium",
                "monetization",
                "articles whose main program has no tracking link yet: "
                + ", ".join(f"{m['program']} {m['article_ids']}" for m in missing),
                evidence=money.get("provenance", {}).get("source", "repository"),
                action_required="a human joins/sets up the affiliate programs",
            )
        )
    threads = state.get("threads") or {}
    if _get(threads, "worker", "stock_maintenance_enabled") is False:
        out.append(
            warning(
                "threads-stock-maintenance-off",
                "info",
                "threads",
                "resident worker proposal-stock maintenance is intentionally OFF",
                evidence="docs/operations/threads-proposal-stock.md",
                action_required="none (design choice)",
            )
        )
    diagnostic = _get(state, "timing", "diagnostic") or {}
    if diagnostic.get("state") in ("due", "overdue"):
        out.append(
            warning(
                "threads-performance-diagnostic-stale",
                "medium" if diagnostic["state"] == "overdue" else "low",
                "threads",
                f"performance diagnostic is {diagnostic['state']}: "
                f"{_get(threads, 'performance', 'rerun_reason') or 'report too old'}",
                evidence="reports/threads_performance_diagnostic_latest.json",
                action_required="re-run the read-only diagnostic",
            )
        )
    out += _daily_warnings(state)
    for alert in _get(state, "analytics", "latest_daily_run", "alerts", default=[]) or []:
        out.append(
            warning(
                f"ops-alert-{alert['type'].lower()}",
                "high" if alert.get("severity") == "critical" else "medium",
                "operations",
                alert["message"],
                evidence=alert.get("evidence", "operations run"),
                action_required=alert.get("action", "a human checks"),
            )
        )
    weekly = _get(state, "timing", "weekly") or {}
    if weekly.get("state") == "missing":
        out.append(
            warning(
                "ops-weekly-run-missing",
                "medium",
                "operations",
                f"the weekly task ran but no weekly run is recorded ({weekly.get('reason')})",
                evidence="Get-ScheduledTaskInfo + operations_runs",
                action_required="read D:/Logs/affiliate-ai/operations-weekly.log (do not change "
                "the task)",
            )
        )
    explained = _get(state, "timing", "daily", "state") in ("latest_partial", "latest_failed")
    for name, task in (_get(state, "scheduler", "tasks", default={}) or {}).items():
        for text in task.get("warnings") or []:
            if (
                explained
                and name == "affiliate-ai-operations-daily"
                and text.startswith("last run result")
            ):
                continue  # 最後の daily の実行の状態 (ops の警告) で説明済み
            out.append(
                warning(
                    f"scheduler-{name}",
                    "medium",
                    "scheduler",
                    f"{name}: {text}",
                    evidence="Get-ScheduledTaskInfo",
                    action_required="read the task log; do not change the task",
                )
            )
    approvals = state.get("approvals") or {}
    if approvals.get("genuine_mobile_render_observed") is False:
        out.append(
            warning(
                "approvals-mobile-render-unobserved",
                "low",
                "approvals",
                "a genuine live mobile rendering of the T6.1 approval page has not been recorded",
                evidence=approvals.get("evidence", "docs"),
                action_required="observe on a phone and record it",
            )
        )
    for row in state.get("decisions") or []:
        if not row.get("supported"):
            out.append(
                warning(
                    f"decision-unsupported-{row['id']}",
                    "medium",
                    "decisions",
                    (
                        f"decision {row['id']} is no longer supported by its evidence: "
                        f"{row['problems']}"
                    ),
                    evidence=", ".join(row["evidence"]),
                    action_required="update the decision or the record",
                )
            )
    for section, prov in (state.get("source_freshness") or {}).items():
        if prov.get("status") in ("unavailable", "error"):
            out.append(
                warning(
                    f"source-unavailable-{section}",
                    "low",
                    "source_freshness",
                    f"{section} could not be read: {prov.get('reason')}",
                    evidence=prov.get("source"),
                    action_required="re-run when the source is reachable",
                )
            )
    out += _drift_warnings(state)
    out += _invariant_warnings(state)
    return _drop_covered(dedupe(out))


def _daily_warnings(state: Mapping) -> list[dict]:
    """最後の daily の実行がうまくいかなかったとき (新しい成功で置き換わっていなければ)。"""

    timing = _get(state, "timing", "daily") or {}
    daily = _get(state, "analytics", "latest_daily_run") or {}
    if timing.get("state") not in ("latest_partial", "latest_failed") or not daily:
        return []
    unclean = [
        f"{s['step_name']}={s['status']}"
        for s in daily.get("steps") or []
        if s.get("status") != "succeeded"
    ]
    active = _get(state, "analytics", "alerts", "active", default=0)
    resolved = _get(state, "analytics", "alerts", "resolved", default=0)
    follow = timing.get("follow_up")
    severity = {"not_due": "low", "due": "medium", "overdue": "high"}.get(follow, "medium")
    if active and severity == "low":
        severity = "medium"
    if follow == "not_due":
        when = f"awaiting the next scheduled run (check after {timing.get('check_after')})"
    else:
        when = (
            f"follow-up {follow}: no newer daily run recorded after "
            f"{timing.get('expected_superseding_run')}"
        )
    return [
        warning(
            "ops-latest-daily-run-not-clean",
            severity,
            "operations",
            f"latest daily run #{daily.get('id')} ({daily.get('effective_date')}) was "
            f"{daily.get('status')}: {', '.join(unclean) or 'no failed step recorded'}; "
            f"active alerts {active}, resolved {resolved}; {when}",
            evidence="operations_runs / operations_step_runs + scheduler next run",
            action_required="confirm the next scheduled daily run succeeds",
        )
    ]


_DRIFT_LABEL = {"expected_difference": "expected difference", "stale_doc": "stale documentation"}


def _drift_warnings(state: Mapping) -> list[dict]:
    out = []
    for f in state.get("drift") or []:
        label = _DRIFT_LABEL.get(f["classification"], f["classification"].replace("_", " "))
        out.append(
            warning(
                f"drift-{f['id']}",
                f["severity"],
                f["area"],
                f"{label}: {f['field']} — {f['conflicting_source']} says "
                f"{f['conflicting_value']}; {f['authoritative_source']} says "
                f"{f['authoritative_value']}",
                evidence=f"{f['authoritative_source']} vs {f['conflicting_source']}",
                action_required=f["recommended_resolution"],
                blocking=f["blocking"],
            )
        )
    return out


# 不変条件の失敗のうち、ほかの警告がもう同じことを言っているもの。
_COVERED_BY = {
    "wordpress-featured-images-complete": "wp-missing-featured-images",
    "wordpress-taxonomy-matches-plan": "drift-wp-taxonomy-drift",
    "threads-stock-maintenance-off": "drift-threads-stock-maintenance-on",
}


def _invariant_warnings(state: Mapping) -> list[dict]:
    out = []
    for inv in _get(state, "invariants", "results", default=[]) or []:
        if inv["level"] == "advisory" or inv["result"] != "fail":
            continue
        out.append(
            warning(
                f"invariant-{inv['id']}",
                inv["severity"],
                "invariants",
                f"{inv['level']} invariant failed: {inv['description']} (expected "
                f"{inv['expected']}, observed {inv['observed']})",
                evidence=inv["source"],
                action_required="a human investigates; T7 does not change production",
                blocking=inv["level"] == "hard" and inv["severity"] == "critical",
            )
        )
    return out


def _drop_covered(items: list[dict]) -> list[dict]:
    ids = {w["id"] for w in items}
    return [
        w
        for w in items
        if not (
            w["id"].startswith("invariant-")
            and _COVERED_BY.get(w["id"].removeprefix("invariant-")) in ids
        )
    ]


def build_known_issues(state: Mapping, warnings: list[dict]) -> list[dict]:
    """警告のうち、人の対応を待っている事実 (info 以外) を問題として並べる。"""

    return [
        {
            "id": w["id"],
            "area": w["area"],
            "summary": w["message"],
            "blocking": w["blocking"],
            "action_required": w["action_required"],
        }
        for w in warnings
        if w["severity"] in ("critical", "high", "medium")
    ]


PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def action(
    aid,
    priority,
    area,
    text,
    *,
    why,
    prerequisites=(),
    blocking=False,
    production_write_required=False,
    human_checkpoint_required=False,
    due=True,
    due_at=None,
) -> dict:
    return {
        "id": aid,
        "priority": priority,
        "area": area,
        "action": text,
        "why": why,
        "prerequisites": list(prerequisites),
        "blocking": blocking,
        "production_write_required": production_write_required,
        "human_checkpoint_required": human_checkpoint_required,
        "due": due,
        "due_at": due_at,
    }


def build_next_actions(state: Mapping, warnings: list[dict]) -> list[dict]:
    ids = {w["id"] for w in warnings}
    out = []
    blocking = sorted(w["id"] for w in warnings if w["blocking"])
    if blocking:
        out.append(
            action(
                "resolve-blocking-warnings",
                "P0",
                "project",
                "resolve the blocking drift / warnings before starting any new production phase "
                "(investigate; T7 never mutates production to make the report green)",
                why=", ".join(blocking),
                blocking=True,
                human_checkpoint_required=True,
            )
        )
    # 進行中の劣化
    active_alerts = sorted(i for i in ids if i.startswith("ops-alert-"))
    if active_alerts:
        out.append(
            action(
                "investigate-active-operations-alerts",
                "P1",
                "operations",
                "investigate the active operations alerts (read the alert and the daily log)",
                why=", ".join(active_alerts),
                human_checkpoint_required=True,
            )
        )
    if "invariant-threads-worker-running" in ids:
        out.append(
            action(
                "check-threads-worker",
                "P1",
                "threads",
                "read D:/Logs/affiliate-ai/threads-worker.log to see why the worker has no fresh "
                "heartbeat (the scheduled task recovers it; do not restart it by hand from T7)",
                why="the resident worker is not running",
                human_checkpoint_required=True,
            )
        )
    daily = _get(state, "timing", "daily") or {}
    if "ops-latest-daily-run-not-clean" in ids:
        follow = daily.get("follow_up")
        if follow in ("due", "overdue", "unknown"):
            out.append(
                action(
                    "check-daily-run",
                    "P1",
                    "operations",
                    "read D:/Logs/affiliate-ai/operations-daily.log: no newer daily run is "
                    "recorded after the scheduled time (do not change the task)",
                    why=f"daily follow-up is {follow}",
                    due_at=daily.get("check_after"),
                )
            )
        else:
            out.append(
                action(
                    "confirm-next-daily-run",
                    "P2",
                    "operations",
                    f"after {daily.get('check_after')}, confirm that the next scheduled daily run "
                    "succeeded; a newer success supersedes the partial run (do not wait or re-run "
                    "it by hand)",
                    why="the latest daily run was partial and has not been superseded yet",
                    due=False,
                    due_at=daily.get("check_after"),
                )
            )
    if any(i.startswith("drift-doc-") for i in ids):
        out.append(
            action(
                "correct-stale-docs",
                "P2",
                "documentation",
                "correct the stale documents listed in documentation_health (documentation only)",
                why="a document states a current state that the live sources contradict",
            )
        )
    if any(i.startswith("drift-config-note-") for i in ids):
        out.append(
            action(
                "review-policy-note-text",
                "P3",
                "documentation",
                "a human updates the stale explanatory note in threads_operations_policy.json "
                "(the value is correct; T7 does not edit runtime configuration)",
                why="the note inside the runtime configuration contradicts the value next to it",
                human_checkpoint_required=True,
            )
        )
    out.append(
        action(
            "t7-validate-project-state",
            "P2",
            "project",
            "T7B: review the drift report and documentation health against reality; close T7B",
            why="T7B is the active phase; C10 depends on a trustworthy state report",
        )
    )
    diagnostic = _get(state, "timing", "diagnostic") or {}
    if diagnostic.get("state") in ("due", "overdue"):
        out.append(
            action(
                "rerun-threads-performance-diagnostic",
                "P2",
                "threads",
                "re-run scripts/analyze_threads_performance.py (read-only)",
                why=f"diagnostic is {diagnostic['state']}: enough publications matured past 6h",
            )
        )
    approvals = state.get("approvals") or {}
    observed = approvals.get("genuine_mobile_render_observed")
    if observed is False:
        out.append(
            action(
                "observe-mobile-approval-render",
                "P2",
                "approvals",
                "open a real approval email on a phone and record how the T6.1 page renders",
                why="the T6.1 mobile rendering has not been observed live yet",
                human_checkpoint_required=True,
            )
        )
    stock_on = _get(state, "threads", "worker", "stock_maintenance_enabled")
    if observed is True and stock_on is False:
        out.append(
            action(
                "decide-proposal-stock-routine",
                "P3",
                "threads",
                (
                    "decide the proposal-stock operating routine (keep --maintain-proposal-stock "
                    "OFF until decided)"
                ),
                why="the genuine mobile approval rendering has been observed",
                human_checkpoint_required=True,
            )
        )
    if "monetization-missing-tracking" in ids:
        out.append(
            action(
                "set-up-missing-affiliate-programs",
                "P2",
                "monetization",
                "join/set up the affiliate programs for the articles without tracking",
                why="those articles cannot earn until tracking exists",
                production_write_required=True,
                human_checkpoint_required=True,
            )
        )
    out.append(
        action(
            "prepare-n0",
            "P3",
            "roadmap",
            "prepare N0 (after T7)",
            why="next roadmap phase",
            prerequisites=["t7-validate-project-state"],
        )
    )
    out.append(
        action(
            "note-n1-n3",
            "P3",
            "roadmap",
            "note work N1/N2/N3 (after N0)",
            why="roadmap",
            prerequisites=["prepare-n0"],
        )
    )
    out.append(
        action(
            "c10-after-maturity",
            "P3",
            "roadmap",
            "C10 only after T7 and N0 are done and the operations data has matured",
            why="C10 depends on stable operations and a trustworthy state report",
            prerequisites=["t7-validate-project-state", "prepare-n0"],
            blocking=True,
        )
    )
    seen, ordered = set(), []
    for item in sorted(out, key=lambda a: (PRIORITY_ORDER[a["priority"]], a["id"])):
        if item["id"] not in seen:
            seen.add(item["id"])
            ordered.append(item)
    return ordered
