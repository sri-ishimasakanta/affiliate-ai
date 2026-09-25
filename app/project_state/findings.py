"""警告・既知の問題・次の行動 (状態からだけ導く。pure・決定論的)。

- 警告: ``id`` / ``severity`` / ``area`` / ``message`` / ``evidence`` / ``action_required`` /
  ``blocking``。同じ ``id`` は 1 つにまとめる。意図した設計 (在庫の保守を無効にしている、
  など) は誤りとして扱わず ``info`` にする。
- 次の行動: 優先度 → id の順に並べる。外部に取り返しのつかない決定を自動でしない
  (本番に書く行動・人の確認が要る行動はそう明示する)。
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
                "medium" if ahead >= 10 else "low",
                "git",
                f"local branch is {ahead} commit(s) ahead of {git.get('remote')} (not pushed)",
                evidence="git rev-list --left-right --count",
                action_required="human decides when to push",
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
    db = state.get("database") or {}
    if db.get("pending_migrations"):
        out.append(
            warning(
                "db-pending-migrations",
                "high",
                "database",
                f"database is behind the code head: {db['pending_migrations']}",
                evidence="alembic",
                action_required="a human applies migrations explicitly",
                blocking=True,
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
    if fi.get("declared_vs_live_disagreements"):
        out.append(
            warning(
                "wp-featured-image-drift",
                "high",
                "featured_images",
                "live featured_media differs from the W1 manifests",
                evidence=str(fi["declared_vs_live_disagreements"]),
                action_required="investigate",
                blocking=True,
            )
        )
    media_99 = fi.get("media_99") or {}
    if media_99.get("exists"):
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
    taxonomy = state.get("taxonomy") or {}
    if taxonomy.get("matches_plan") is False:
        out.append(
            warning(
                "wp-taxonomy-drift",
                "high",
                "taxonomy",
                f"live categories differ from the W2 plan: {taxonomy.get('assignment_mismatches')}",
                evidence="live WordPress",
                action_required="investigate before any taxonomy change",
                blocking=True,
            )
        )
    out.append(
        warning(
            "wp-api-user-author-role",
            "info",
            "wordpress",
            "the WordPress API user has the author role and cannot create categories (by design)",
            evidence="docs/operations/taxonomy-w2.md §9.1",
            action_required="create new categories manually in wp-admin; do not broaden the role",
        )
    ) if taxonomy else None
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
    perf = threads.get("performance") or {}
    if perf.get("rerun_recommended"):
        out.append(
            warning(
                "threads-performance-diagnostic-stale",
                "low",
                "threads",
                f"performance diagnostic is stale: {perf.get('rerun_reason')}",
                evidence="reports/threads_performance_diagnostic_latest.json",
                action_required="re-run the read-only diagnostic",
            )
        )
    daily = _get(state, "analytics", "latest_daily_run") or {}
    if daily and daily.get("status") not in ("succeeded", None):
        unclean = [
            f"{s['step_name']}={s['status']}"
            for s in daily.get("steps") or []
            if s.get("status") != "succeeded"
        ]
        active = _get(state, "analytics", "alerts", "active", default=0)
        resolved = _get(state, "analytics", "alerts", "resolved", default=0)
        out.append(
            warning(
                "ops-latest-daily-run-not-clean",
                "medium",
                "operations",
                f"latest daily run #{daily.get('id')} ({daily.get('effective_date')}) was "
                f"{daily.get('status')}: {', '.join(unclean) or 'no failed step recorded'}; "
                f"active alerts {active}, resolved {resolved}",
                evidence="operations_runs / operations_step_runs",
                action_required="confirm the next scheduled daily run succeeds",
            )
        )
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
    for name, task in (_get(state, "scheduler", "tasks", default={}) or {}).items():
        for text in task.get("warnings") or []:
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
    return dedupe(w for w in out if w)


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
    }


def build_next_actions(state: Mapping, warnings: list[dict]) -> list[dict]:
    ids = {w["id"] for w in warnings}
    out = []
    if any(w["blocking"] for w in warnings):
        out.append(
            action(
                "resolve-blocking-warnings",
                "P0",
                "project",
                "resolve the blocking warnings before starting any new production phase",
                why=", ".join(sorted(w["id"] for w in warnings if w["blocking"])),
                blocking=True,
                human_checkpoint_required=True,
            )
        )
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
    if "ops-latest-daily-run-not-clean" in ids:
        out.append(
            action(
                "confirm-next-daily-run",
                "P1",
                "operations",
                "after the next scheduled daily run (06:30 JST), confirm it succeeded; if not, "
                "read D:/Logs/affiliate-ai/operations-daily.log (do not change the task)",
                why="the latest daily run was not clean (see the operations warning)",
            )
        )
    out.append(
        action(
            "t7-validate-project-state",
            "P1",
            "project",
            "T7: review this report against reality and harden the generator (T7B)",
            why="T7 is the active phase; C10 depends on a trustworthy state report",
        )
    )
    if "threads-performance-diagnostic-stale" in ids:
        out.append(
            action(
                "rerun-threads-performance-diagnostic",
                "P2",
                "threads",
                "re-run scripts/analyze_threads_performance.py (read-only)",
                why="newer publications have matured past the diagnostic checkpoints",
            )
        )
    if "approvals-mobile-render-unobserved" in ids:
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
        out.append(
            action(
                "decide-proposal-stock-routine",
                "P3",
                "threads",
                (
                    "decide the proposal-stock operating routine (keep --maintain-proposal-stock "
                    "OFF until decided)"
                ),
                why="depends on the mobile approval observation",
                prerequisites=["observe-mobile-approval-render"],
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
