"""プロジェクトの状態の報告を組み立てる (読むだけ。外部には書かない)。

入口: ``scripts/generate_project_state.py``。外部への読み取り (WordPress の REST・スケジューラ・
DB) は ``StateContext`` から差し込む。offline では WordPress を読まない (呼ばない)。

報告の形 (``generator_version = project-state/2``。キーは ``strict.TOP_KEYS``):

``generated_at`` / ``generator_version`` / ``mode`` / ``project`` / ``git`` / ``quality`` /
``database`` / ``wordpress`` / ``featured_images`` / ``taxonomy`` / ``monetization`` / ``threads`` /
``approvals`` / ``analytics`` / ``scheduler`` / ``facts`` / ``drift`` / ``invariants`` /
``timing`` / ``documentation_health`` / ``warnings`` / ``decisions`` / ``known_issues`` /
``next_actions`` / ``source_freshness``。主要なセクションは ``provenance`` を持つ。
T7B: ``facts`` は出どころの強さ付きの事実、``drift`` は食い違い (``precedence.finding``)、
``invariants`` は不変条件、``timing`` は時間で決まる状態、``documentation_health`` は
ドキュメントの健康。Markdown は同じ dict から作る (JSON と食い違わない)。
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.project_state import (
    db_state,
    docs_health,
    invariants,
    local_state,
    runtime_records,
    scheduler_state,
    wordpress_state,
)
from app.project_state.decisions import check_support
from app.project_state.drift import detect_drift
from app.project_state.facts import build_facts
from app.project_state.findings import build_known_issues, build_next_actions, build_warnings
from app.project_state.precedence import AUTHORITY_LEVELS
from app.project_state.provenance import provenance
from app.project_state.redaction import redact
from app.project_state.roadmap import load_roadmap, verify_phases
from app.project_state.timing import build_timing

GENERATOR_VERSION = "project-state/2"
SECTIONS = (
    "project",
    "git",
    "quality",
    "database",
    "wordpress",
    "featured_images",
    "taxonomy",
    "monetization",
    "threads",
    "approvals",
    "analytics",
    "scheduler",
)
REPORT_JSON = Path("reports/project_state_latest.json")
REPORT_MD = Path("reports/project_state_latest.md")
# 報告を作る時機 (T7C の決定。警告ではない)。
GENERATION_POLICY = {
    "mode": "on_demand",
    "scheduled": False,
    "reason": (
        "the generator is reliable on demand; the C8 / scheduler topology is not changed just to "
        "refresh a derived report; scheduling is reconsidered only for a concrete operational need"
    ),
    "decision": "project-state-on-demand",
}


@dataclass
class StateContext:
    root: Path
    now: datetime
    database_url: str
    offline: bool = False
    with_tests: bool = False
    runner: Callable | None = None
    wordpress_client_factory: Callable | None = None
    scheduler_reader: Callable | None = None
    db_engine: object | None = None
    commit_exists: Callable[[str], bool] | None = None
    worker_log_path: Path | None = None
    extra: dict = field(default_factory=dict)


def _commit_exists_factory(root: Path) -> Callable[[str], bool]:
    def exists(sha: str) -> bool:
        done = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=root, capture_output=True
        )
        return done.returncode == 0

    return exists


def _articles(ctx: StateContext) -> list[dict]:
    try:
        engine = ctx.db_engine or db_state.readonly_engine(ctx.database_url)
        with engine.connect() as conn:
            return db_state._rows(
                conn, "select id, title, slug, wordpress_post_id from articles order by id"
            )
    except Exception:
        return []


def _read_live(ctx: StateContext):
    if ctx.offline:
        return None, None
    if ctx.wordpress_client_factory is None:
        return None, "no WordPress client configured"
    try:
        return wordpress_state.read_wordpress(ctx.wordpress_client_factory), None
    except Exception as exc:  # 読めなければ「読めなかった」
        return None, f"WordPress not readable ({type(exc).__name__})"


def _worker_start_record(ctx: StateContext, threads: dict) -> dict:
    """ロックを持つ pid の最後の起動の記録 (worker のログの末尾を読むだけ)。"""

    path = ctx.worker_log_path or runtime_records.DEFAULT_WORKER_LOG
    pid = (threads.get("worker") or {}).get("lock_pid")
    base = {"source": path.name, "lock_pid": pid, "freshness": "recorded"}
    try:
        text = runtime_records.read_tail(path)
    except OSError as exc:
        return {**base, "found": False, "reason": f"not readable ({type(exc).__name__})"}
    if text is None:
        return {**base, "found": False, "reason": "log file not found"}
    event = runtime_records.last_started(text, pid=pid) if pid else None
    if event is None:
        return {**base, "found": False, "reason": "no start-up event for the lock pid"}
    return {**base, "found": True, **event}


def _discrepancies(drift: list[dict]) -> list[str]:
    return [
        f"{f['classification']}: {f['conflicting_source']} says {f['conflicting_value']}; "
        f"{f['authoritative_source']} says {f['authoritative_value']}"
        for f in drift
        if f["classification"] != "expected_difference"
    ]


def build_report(ctx: StateContext) -> dict:
    root, now = ctx.root, ctx.now
    runner = ctx.runner or local_state.default_runner(root)
    commit_exists = ctx.commit_exists or _commit_exists_factory(root)

    roadmap = verify_phases(root, load_roadmap(root), commit_exists=commit_exists)
    git = local_state.collect_git(runner, now=now)
    quality = local_state.collect_quality(runner, root, now=now, with_tests=ctx.with_tests)
    engine_factory = (lambda: ctx.db_engine) if ctx.db_engine else None
    database = local_state.collect_database(
        root, database_url=ctx.database_url, now=now, engine_factory=engine_factory
    )
    articles = _articles(ctx)
    live, error = _read_live(ctx)
    wordpress = wordpress_state.summarize_wordpress(live, articles=articles, now=now, error=error)
    featured = wordpress_state.summarize_featured_images(root, live, now=now, error=error)
    taxonomy = wordpress_state.summarize_taxonomy(
        root, live, articles=articles, now=now, error=error
    )
    db_sections = db_state.collect_db_sections(
        root, ctx.database_url, now=now, engine=ctx.db_engine
    )
    scheduler = scheduler_state.collect_scheduler(ctx.scheduler_reader, now=now)
    threads = db_sections.get("threads") or {}
    if isinstance(threads.get("worker"), dict):
        threads["worker"]["runtime_start"] = _worker_start_record(ctx, threads)

    project = {
        "provenance": provenance(
            "repository",
            kind="declared",
            status="ok" if not roadmap["problems"] else "degraded",
            observed_at=now,
            freshness="fresh",
            detail="docs/project-roadmap.json + evidence check",
        ),
        "repository_path": str(root),
        "current_phase": roadmap["declared_current_phase"],
        "next_phase": roadmap["next_phase"],
        "last_completed_phase": roadmap["last_completed_phase"],
        "completed_phases": roadmap["completed"],
        "active_phases": roadmap["active"],
        "upcoming_phases": roadmap["upcoming"],
        "deferred_phases": roadmap["deferred"],
        "phases": roadmap["phases"],
        "roadmap_problems": roadmap["problems"],
        "source_precedence": list(AUTHORITY_LEVELS),
        "generation_policy": GENERATION_POLICY,
    }
    report = {
        "generated_at": now.isoformat(timespec="seconds"),
        "generator_version": GENERATOR_VERSION,
        "mode": "offline" if ctx.offline else "live",
        "project": project,
        "git": git,
        "quality": quality,
        "database": database,
        "wordpress": wordpress,
        "featured_images": featured,
        "taxonomy": taxonomy,
        **db_sections,
        "scheduler": scheduler,
    }
    report["timing"] = build_timing(report, now=now)
    doc_findings = docs_health.check_claims(root, report)
    drift = detect_drift(report, doc_findings=doc_findings)
    report["drift"] = drift
    project["source_discrepancies"] = _discrepancies(drift)
    results = invariants.evaluate(report)
    report["invariants"] = {"results": results, "summary": invariants.summary(results)}
    report["facts"] = build_facts(report, drift)
    report["documentation_health"] = docs_health.documentation_health(root, report, doc_findings)
    report["decisions"] = check_support(root, commit_exists=commit_exists)
    report["source_freshness"] = {
        name: report[name]["provenance"]
        for name in SECTIONS
        if isinstance(report.get(name), dict) and report[name].get("provenance")
    }
    warnings = build_warnings(report)
    report["warnings"] = warnings
    report["known_issues"] = build_known_issues(report, warnings)
    report["next_actions"] = build_next_actions(report, warnings)
    return redact(report)


# == Markdown =====================================================================
def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(_fmt(v) for v in value) or "—"
    return str(value)


def _prov(section: dict) -> str:
    p = section.get("provenance") or {}
    extra = f" — {p['reason']}" if p.get("reason") else ""
    return (
        f"_source: {p.get('source')} · {p.get('kind')} · {p.get('status')} · "
        f"{p.get('freshness')} · {_fmt(p.get('observed_at'))}{extra}_"
    )


def _md_summary(report: dict) -> list[str]:
    project, git, quality = report["project"], report["git"], report["quality"]
    wp, fi, tax = report["wordpress"], report["featured_images"], report["taxonomy"]
    warnings, actions = report["warnings"], report["next_actions"]
    blocking = [w for w in warnings if w["blocking"]]
    pytest = quality.get("pytest") or {}
    push = "push pending" if git.get("push_pending") else "nothing to push"
    return [
        "## Executive Summary",
        "",
        f"- Phase: {_phase_line(project)}; "
        f"{len(project['completed_phases'])} phases complete; "
        f"upcoming: {_fmt(project['upcoming_phases'])}",
        f"- Git: `{(git.get('head') or '')[:7]}` on {git.get('branch')}, "
        f"{_fmt(git.get('ahead'))} ahead / {_fmt(git.get('behind'))} behind "
        f"{git.get('remote')} ({push})",
        f"- Quality: ruff {_fmt((quality.get('ruff') or {}).get('ok'))}, "
        f"alembic {_fmt((quality.get('alembic_check') or {}).get('ok'))}, "
        f"pytest {_fmt(pytest.get('summary'))} ({pytest.get('freshness')})",
        f"- WordPress: {_fmt(wp.get('published_count'))} published; featured images "
        f"{_fmt(fi.get('with_featured_image'))}/{_fmt(fi.get('published'))}; "
        f"taxonomy matches the W2 plan: {_fmt(tax.get('matches_plan'))}",
        f"- Drift: {len(report['drift'])} finding(s) "
        f"({sum(1 for f in report['drift'] if f['blocking'])} blocking); invariants "
        + ", ".join(
            f"{level} {c['pass']} pass / {c['fail']} fail / {c['unknown']} unknown"
            for level, c in report["invariants"]["summary"].items()
        ),
        f"- Daily operations: {report['timing']['daily'].get('state')} (follow-up "
        f"{report['timing']['daily'].get('follow_up')}); weekly "
        f"{report['timing']['weekly'].get('state')}; diagnostic "
        f"{report['timing']['diagnostic'].get('state')}",
        f"- Warnings: {len(warnings)} ({len(blocking)} blocking); first next action: "
        f"{actions[0]['action'] if actions else '—'}",
        "",
    ]


def _phase_line(project: dict) -> str:
    if project["current_phase"]:
        return f"**{project['current_phase']}** active"
    return (
        f"no phase in progress (last complete: **{project['last_completed_phase']}**); "
        f"next: **{project['next_phase']}** (not started)"
    )


def _md_phase(project: dict) -> list[str]:
    policy = project["generation_policy"]
    lines = [
        "## Current Phase",
        "",
        f"- Current: {_phase_line(project)}",
        f"- Next phase: {_fmt(project['next_phase'])}; last completed: "
        f"{_fmt(project['last_completed_phase'])}",
        f"- Project-state generation: {policy['mode']} (scheduled: {_fmt(policy['scheduled'])})",
        f"- Completed: {_fmt(project['completed_phases'])}",
        f"- Upcoming: {_fmt(project['upcoming_phases'])}",
        f"- Deferred: {_fmt(project['deferred_phases'])}",
    ]
    for p in project["phases"]:
        if not p["verified"]:
            issues = f": {', '.join(p['issues'])}" if p["issues"] else ""
            lines.append(f"- {p['id']} ({p['status']}) — {p['evidence_kind']}{issues}")
    if project["source_discrepancies"]:
        lines += ["", "Source discrepancies (reported, not resolved):", ""]
        lines += [f"- {d}" for d in project["source_discrepancies"]]
    return [*lines, "", _prov(project), ""]


def _md_git_quality(git: dict, quality: dict) -> list[str]:
    lines = [
        "## Git / Quality",
        "",
        f"- HEAD `{git.get('head')}` — {git.get('head_subject')}",
        f"- clean: {_fmt(git.get('clean'))}; staged {len(git.get('staged') or [])}, "
        f"unstaged {len(git.get('unstaged') or [])}, untracked {_fmt(git.get('untracked'))}",
        f"- ahead {_fmt(git.get('ahead'))} / behind {_fmt(git.get('behind'))} "
        f"({git.get('remote')}, as last fetched)",
    ]
    for name in ("ruff", "alembic_check", "git_diff_check", "pytest"):
        c = quality.get(name) or {}
        lines.append(
            f"- {name}: {_fmt(c.get('ok'))} — {c.get('summary')} "
            f"({c.get('freshness')}, {_fmt(c.get('observed_at'))})"
        )
    return [*lines, "", _prov(quality), ""]


def _md_database(db: dict) -> list[str]:
    latest = db.get("latest_migration") or {}
    return [
        "## Database",
        "",
        f"- target: {db.get('target')}; code head {_fmt(db.get('code_heads'))}; "
        f"DB {_fmt(db.get('db_revisions'))}",
        f"- at head: {_fmt(db.get('db_at_code_head'))}; "
        f"pending: {_fmt(db.get('pending_migrations'))}",
        f"- latest migration: {_fmt(latest.get('revision'))} — {_fmt(latest.get('message'))}",
        "",
        _prov(db),
        "",
    ]


def _md_wordpress(wp: dict) -> list[str]:
    lines = ["## WordPress", ""]
    if wp.get("published_count") is None:
        lines.append("- live state not read")
    else:
        lines += [
            f"- posts by status: {wp.get('post_status_counts')}; published {wp['published_count']}",
            f"- categories {wp.get('category_count')}; tags {wp.get('tag_count')}; "
            f"media {wp.get('media_count')}",
            "- published posts without an application article: "
            f"{_fmt(wp.get('published_posts_without_application_article'))}",
        ]
    return [*lines, "", _prov(wp), ""]


def _md_featured(fi: dict) -> list[str]:
    lines = [
        "## Featured Images",
        "",
        f"- W1.4 pilot: {fi.get('w1_4_pilot')}",
        f"- W1.5 rollout: {fi.get('w1_5_rollout')}",
        f"- recorded in manifests: {fi.get('declared_count')} articles",
    ]
    if fi.get("with_featured_image") is not None:
        m99 = fi.get("media_99") or {}
        lines += [
            f"- live: {fi['with_featured_image']}/{fi['published']} published posts; "
            f"missing: {_fmt(fi.get('without_featured_image'))}",
            f"- manifest vs live disagreements: {_fmt(fi.get('declared_vs_live_disagreements'))}",
            f"- media 99: exists {_fmt(m99.get('exists'))}, "
            f"attached {_fmt(m99.get('attached_post'))}, "
            f"featured by {_fmt(m99.get('featured_by'))} — {m99.get('note')}",
        ]
    return [*lines, "", _prov(fi), ""]


def _md_taxonomy(tax: dict) -> list[str]:
    lines = [
        "## Taxonomy",
        "",
        f"- W2: {tax.get('w2_status_declared')}; model: {tax['plan']['model']}",
    ]
    if tax.get("children") is not None:
        parent = tax.get("parent") or {}
        lines += [
            f"- parent: {parent.get('name')} (id {parent.get('id')}, "
            f"`{parent.get('slug')}`, count {parent.get('count')})",
            "",
            "| child | id | slug | count | expected | exact |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for c in tax["children"]:
            lines.append(
                f"| {c['name']} | {_fmt(c['id'])} | `{c['slug']}` | {_fmt(c['count'])} | "
                f"{c['expected_count']} | {_fmt(c['exact'])} |"
            )
        lines += [
            "",
            f"- article 1 parent-only: {_fmt(tax.get('article_1_parent_only'))}; "
            f"parent + child posts: {tax.get('parent_plus_child_posts')}/24",
            f"- categories {tax.get('categories_total')}, tags {tax.get('tags_total')}; "
            f"mismatches: {_fmt(tax.get('assignment_mismatches'))}",
            f"- rendering: {tax.get('expected_rendering')}",
        ]
    lines += [f"- lesson: {lesson}" for lesson in tax.get("lessons", [])]
    return [*lines, "", _prov(tax), ""]


def _md_money(money: dict) -> list[str]:
    lines = ["## Monetization", ""]
    if money.get("live_programs") is not None:
        monetized = ", ".join(
            f"{m['article_id']} ({'/'.join(m['programs'])}, {m['active_placements']} placement)"
            for m in money["monetized_articles"]
        )
        missing = "; ".join(f"{m['program']} {m['article_ids']}" for m in money["missing_tracking"])
        lines += [
            f"- live programs: {_fmt(money['live_programs'])}",
            f"- monetized articles: {monetized or '—'}",
            f"- missing tracking: {missing or '—'}",
            f"- commission facts recorded: {money['commission_facts']['count']} "
            "(no commission data is invented)",
            f"- outbound clicks recorded: {money['outbound_clicks']['count']}",
            f"- latest revenue candidate run: {_fmt(money.get('latest_revenue_run'))}",
        ]
    return [*lines, "", _prov(money), ""]


def _md_threads(threads: dict) -> list[str]:
    lines = ["## Threads", ""]
    if threads.get("policy"):
        pol, worker, pubs = threads["policy"], threads["worker"], threads["publications"]
        perf = threads.get("performance") or {}
        latest = pubs.get("latest") or {}
        start = worker.get("runtime_start") or {}
        stock = "ON" if worker.get("stock_maintenance_enabled") else "OFF"
        rerun = (
            f" — {perf['rerun_reason']}"
            if perf.get("rerun_reason")
            else f" — newly past 6h: {_fmt(perf.get('newly_past_6h'))} ({perf.get('rerun_rule')})"
        )
        lines += [
            f"- account: {_fmt(threads.get('account'))}; policy {pol.get('policy_version')}",
            f"- publish window {pol.get('publication_window')}, approval email "
            f"{pol.get('approval_notification_window')}, gap {pol.get('soft_min_gap_minutes')} min",
            "- one publication per cycle, no fixed posting times, no catch-up bursts; "
            f"automatic publication enabled: {_fmt(pol.get('automatic_publication_enabled'))}",
            f"- worker running: {_fmt(worker.get('running'))} (heartbeat "
            f"{_fmt(worker.get('heartbeat_age_seconds'))} s ago, max "
            f"{worker.get('heartbeat_max_seconds')} s); stock maintenance: {stock}",
            f"- publish flags: {_fmt(worker.get('publish_profile_flags'))}",
            f"- start-up record for lock pid {_fmt(worker.get('lock_pid'))}: "
            + (
                f"can_publish {_fmt(start.get('can_publish'))}, capabilities "
                f"{_fmt(start.get('capabilities'))} (at {start.get('at')})"
                if start.get("found")
                else f"not found ({start.get('reason')})"
            ),
            f"- proposals: {threads.get('proposals')}; awaiting approval "
            f"{threads.get('awaiting_approval')}; approved not published: "
            f"{_fmt(threads.get('approved_not_published'))}",
            f"- publications: {pubs.get('total')} {pubs.get('by_status')} "
            f"{pubs.get('by_trigger')}; latest #{_fmt(latest.get('id'))} at "
            f"{_fmt(latest.get('published_at'))}",
            f"- insights: {threads['insights']['snapshots']} snapshots, latest "
            f"{_fmt(threads['insights']['latest_observed_at'])}; learning guidance on "
            f"{threads['learning_guidance']['proposals_with_guidance']} proposal(s)",
            f"- stock: {_fmt(threads.get('stock'))}",
            f"- performance diagnostic: {perf.get('status')} "
            f"(generated {_fmt(perf.get('generated_at'))}); "
            f"rerun recommended: {_fmt(perf.get('rerun_recommended'))}{rerun}",
        ]
    return [*lines, "", _prov(threads), ""]


def _md_approvals(approvals: dict) -> list[str]:
    lines = ["## Approval Gateway", ""]
    if approvals.get("sessions_by_state") is not None:
        lines += [
            f"- C8.8: {approvals.get('c8_8_gateway')}; sessions "
            f"{approvals.get('sessions_by_state')}; latest sync "
            f"{_fmt(approvals.get('latest_sync_at'))}",
            "- T6.1 relay deployment recorded: "
            f"{_fmt(approvals.get('relay_t6_1_deployment_recorded'))}",
            "- genuine live mobile rendering observed: "
            f"{_fmt(approvals.get('genuine_mobile_render_observed'))} "
            f"({approvals.get('evidence')})",
        ]
    return [*lines, "", _prov(approvals), ""]


def _md_analytics(analytics: dict) -> list[str]:
    lines = ["## Analytics / C8", ""]
    daily = analytics.get("latest_daily_run")
    if daily:
        steps = ", ".join(f"{s['step_name']}={s['status']}" for s in daily.get("steps") or [])
        alerts = analytics["alerts"]
        lines += [
            f"- latest daily run #{daily['id']} ({daily['effective_date']}): **{daily['status']}**",
            f"- steps: {steps}",
            f"- alerts: {alerts['active']} active, {alerts['resolved']} resolved",
        ]
        weekly = analytics.get("latest_weekly_run")
        lines.append(f"- latest weekly run: {_fmt(weekly)}")
        for name, src in (analytics.get("sources") or {}).items():
            lines.append(f"- {name}: {_fmt(src)}")
    return [*lines, "", _prov(analytics), ""]


def _md_scheduler(scheduler: dict) -> list[str]:
    lines = ["## Scheduler", ""]
    for name, task in (scheduler.get("tasks") or {}).items():
        lines += [
            f"- `{name}`: {task.get('state')} (enabled {_fmt(task.get('enabled'))}), "
            f"{_fmt(task.get('triggers'))}",
            f"  - last run {_fmt(task.get('last_run'))} → {_fmt(task.get('last_result'))} "
            f"({_fmt(task.get('last_result_meaning'))}); next {_fmt(task.get('next_run'))}",
            f"  - action `{task.get('action')} {task.get('arguments')}`",
        ]
    return [*lines, "", _prov(scheduler), ""]


def _md_quality_of_state(report: dict) -> list[str]:
    lines = [
        "## Facts (source precedence)",
        "",
        f"Precedence: {' > '.join(report['project']['source_precedence'])}",
        "",
        "| fact | value | authority | confidence | conflicts |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, f in report["facts"].items():
        conflicts = ", ".join(c["finding"] for c in f["conflicts"]) or "—"
        value = f["value"]
        if isinstance(value, dict):
            value = "; ".join(f"{k}={_fmt(v)}" for k, v in value.items())
        lines.append(
            f"| {name} | {_fmt(value)} | {f['authority']} | {f['confidence']} | {conflicts} |"
        )
    lines += ["", "## Drift", ""]
    for f in report["drift"]:
        lines.append(
            f"- **{f['severity']}** `{f['classification']}` {f['field']}: "
            f"{f['conflicting_source']} vs {f['authoritative_source']} → "
            f"{f['recommended_resolution']}" + (" (blocking)" if f["blocking"] else "")
        )
    if not report["drift"]:
        lines.append("- none")
    lines += ["", "## Invariants", "", "| id | level | result | severity |"]
    lines.append("| --- | --- | --- | --- |")
    for i in report["invariants"]["results"]:
        lines.append(f"| {i['id']} | {i['level']} | {i['result']} | {i['severity']} |")
    timing = report["timing"]
    daily, weekly, diag = timing["daily"], timing["weekly"], timing["diagnostic"]
    lines += [
        "",
        "## Timing",
        "",
        f"- daily: {daily.get('state')}; follow-up {daily.get('follow_up')}; check after "
        f"{_fmt(daily.get('check_after'))}; superseded runs "
        f"{_fmt(daily.get('superseded_non_success_run_ids'))}",
        f"- weekly: {weekly.get('state')}; next {_fmt(weekly.get('next_expected_run'))}",
        f"- diagnostic: {diag.get('state')}; newly past 6h {_fmt(diag.get('newly_past_6h'))} "
        f"(due at {diag.get('needed_for_due')})",
    ]
    health = report["documentation_health"]
    lines += ["", "## Documentation Health", ""]
    lines += [f"- stale: {d['path']} — {d['claims']}" for d in health["stale_documents"]]
    lines += [f"- corrected: {d['path']} — {d['note']}" for d in health["corrected_documents"]]
    lines += [
        f"- unresolved: {d['where']} — {d['resolution']}" for d in health["unresolved_mismatches"]
    ]
    return [*lines, ""]


def _md_lists(report: dict) -> list[str]:
    lines = ["## Warnings", ""]
    lines += [
        f"- **{w['severity']}** [{w['area']}] {w['message']} — {w['action_required']}"
        + (" (blocking)" if w["blocking"] else "")
        for w in report["warnings"]
    ] or ["- none"]
    lines += ["", "## Decisions", ""]
    lines += [
        f"- {'supported' if d['supported'] else 'NOT supported'}: {d['decision']} (`{d['id']}`)"
        for d in report["decisions"]
    ]
    lines += ["", "## Known Issues", ""]
    lines += [
        f"- [{i['area']}] {i['summary']} — {i['action_required']}" for i in report["known_issues"]
    ] or ["- none"]
    lines += ["", "## Next Actions", ""]
    for a in report["next_actions"]:
        flags = [
            name
            for name, on in (
                ("blocking", a["blocking"]),
                ("production write", a["production_write_required"]),
                ("human checkpoint", a["human_checkpoint_required"]),
            )
            if on
        ]
        prereq = f" (after {', '.join(a['prerequisites'])})" if a["prerequisites"] else ""
        tail = f" _[{', '.join(flags)}]_" if flags else ""
        lines.append(
            f"1. **{a['priority']}** [{a['area']}] {a['action']}{prereq} — {a['why']}{tail}"
        )
    lines += [
        "",
        "## Source Freshness",
        "",
        "| section | source | kind | status | freshness | observed |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, p in report["source_freshness"].items():
        lines.append(
            f"| {name} | {p.get('source')} | {p.get('kind')} | {p.get('status')} | "
            f"{p.get('freshness')} | {_fmt(p.get('observed_at'))} |"
        )
    return lines


def render_markdown(report: dict) -> str:
    lines = [
        "# Project State",
        "",
        f"Generated {report['generated_at']} ({report['mode']}) by "
        f"`{report['generator_version']}`. Read-only: nothing was written to WordPress, "
        "Threads, the scheduler or the database.",
        "",
        *_md_summary(report),
        *_md_phase(report["project"]),
        *_md_git_quality(report["git"], report["quality"]),
        *_md_database(report["database"]),
        *_md_wordpress(report["wordpress"]),
        *_md_featured(report["featured_images"]),
        *_md_taxonomy(report["taxonomy"]),
        *_md_money(report["monetization"]),
        *_md_threads(report["threads"]),
        *_md_approvals(report["approvals"]),
        *_md_analytics(report["analytics"]),
        *_md_scheduler(report["scheduler"]),
        *_md_quality_of_state(report),
        *_md_lists(report),
    ]
    return "\n".join(lines) + "\n"


def write_reports(root: Path, report: dict, *, json_out=True, markdown_out=True) -> list[Path]:
    written = []
    if json_out:
        path = root / REPORT_JSON
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(path)
    if markdown_out:
        path = root / REPORT_MD
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_markdown(report), encoding="utf-8")
        written.append(path)
    return written
