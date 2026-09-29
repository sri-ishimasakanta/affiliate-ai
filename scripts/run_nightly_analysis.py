"""夜の分析 (C10-2 / C10-C)。**既定は PLAN (書かない・外に問い合わせない)。**

    uv run python scripts/run_nightly_analysis.py                          # PLAN の要約
    uv run python scripts/run_nightly_analysis.py --section next|clusters|gaps|discovery
    uv run python scripts/run_nightly_analysis.py --section facts|refresh|schedule|history
    uv run python scripts/run_nightly_analysis.py --format json
    uv run python scripts/run_nightly_analysis.py --execute [--trigger scheduler]

約 80 候補 (上限ではなく予算) を、手元と保存済みのデータだけで分析する。``--execute`` で書くのは
手元の 2 つの表 (``nightly_analysis_runs`` / ``content_discovery_candidates``) だけ。Keyword・
記事・計画の依頼・Growth Action は作らない。外の取り直し (Google Ads 等) は提供元ごとの計画に
並べるだけで呼ばない。定期の実行 (Windows のタスク) の登録は人の判断 (``--section schedule``)。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
SECTIONS = ("summary", "next", "clusters", "gaps", "discovery", "facts", "refresh", "schedule",
            "history")


def main(argv=None, *, session_factory=None, settings=None, default_section="summary") -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--section", choices=SECTIONS, default=default_section)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--trigger", default="manual", choices=("manual", "scheduler"))
    parser.add_argument("--budget", type=int, default=None)
    parser.add_argument("--as-of", dest="as_of")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    args = parser.parse_args(argv)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass
    from app.services.nightly_analysis_service import (
        NightlyAnalysisError,
        NightlyAnalysisService,
        load_policy,
        schedule_plan,
    )

    if args.section == "schedule":
        plan = schedule_plan(load_policy(), project_root=ROOT)
        print(json.dumps(plan, ensure_ascii=False, indent=2) if args.format == "json"
              else render_schedule(plan))
        return 0
    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    if settings is None:
        from app.config.settings import get_settings

        settings = get_settings()
    now = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    with session_factory() as session:
        service = NightlyAnalysisService(session, settings=settings)
        if args.section == "history":
            rows = service.history()
            print(json.dumps(rows, ensure_ascii=False, indent=2, default=str)
                  if args.format == "json" else render_history(rows))
            return 0
        if args.execute:
            try:
                result = service.execute(now=now, trigger=args.trigger, budget=args.budget)
            except NightlyAnalysisError as exc:
                print(f"refused: {exc.reason}")
                return 2
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            print("side effects: local tables only (nightly_analysis_runs, "
                  "content_discovery_candidates); external calls = 0")
            return 0
        plan = service.plan(now=now, budget=args.budget)
        session.rollback()
    if args.format == "json":
        key = {"summary": None, "next": "next_articles", "clusters": "clusters", "gaps": "gaps",
               "discovery": "discovery", "facts": "facts", "refresh": "refresh_plan"}[
            args.section]
        print(json.dumps(plan if key is None else plan[key], ensure_ascii=False, indent=2,
                         default=str))
    else:
        print(render(plan, args.section))
    print("PLAN: database writes = 0, external calls = 0, articles / keywords created = 0")
    return 0


def render(plan: dict, section: str) -> str:
    c = plan["counts"]
    lines = [f"nightly analysis PLAN — as of {plan['as_of']}",
             f"universe {c['universe']} → prefilter skipped {c['prefilter_skipped']} → eligible "
             f"{c['eligible']} → analyzed {c['analyzed']} (budget {c['budget']}; "
             f"{plan['budget_semantics']}); deferred by budget {c['deferred_by_budget']}",
             "by tier: " + ", ".join(f"{k}={v}" for k, v in c["by_tier"].items()),
             "handoff: " + ", ".join(f"{k}={v}" for k, v in c["handoff"].items()),
             f"clusters {c['clusters']}, gaps {c['gaps']}, new discovery terms "
             f"{c['discovery_new']}, refresh required {c['refresh_required']}"]
    if section in ("summary", "refresh"):
        ads = plan["refresh_plan"]["google_ads"]
        lines += ["", f"external refresh plan (not called): google_ads {ads['terms']} term(s) "
                  f"in {ads['calls_if_run']} batched call(s) — {ads['status']}"]
    if section in ("summary", "next"):
        lines += ["", "next article candidates (no article is created):"]
        shown = plan["next_articles"] if section == "next" else plan["next_articles"][:10]
        for n in shown:
            lines.append(f"- [{n['handoff']['readiness']}] {n['topic']} ({n['cluster_id']}; "
                         f"{n['content_type']['role']}; {n['monetization_role']}; "
                         f"{n['cluster_role']}) — {n['selection']['reason']}")
            if n["blockers"]:
                lines.append(f"    blockers: {'; '.join(n['blockers'])}")
            lines.append(f"    next: {n['handoff']['next_step']}")
    if section == "clusters":
        for cl in plan["clusters"]:
            lines.append(f"- {cl['cluster_id']} {cl['canonical_topic']} [{cl['config_status']}] "
                         f"{cl['coverage_state']}: {len(cl['articles'])} article(s), roles "
                         f"{', '.join(cl['roles_present']) or '—'}, pillar "
                         f"{cl['pillar']['keyword']} covered={cl['pillar']['covered']}")
    if section == "gaps":
        for g in plan["gaps"]:
            lines.append(f"- {g['gap_key']}: {g['reason']}"
                         + (f" (blockers: {', '.join(g['blockers'])})" if g["blockers"] else ""))
    if section == "discovery":
        for d in plan["discovery"]:
            if d["is_new"]:
                lines.append(f"- {d['phrase']} [{d['cluster_key'] or '—'}] sources "
                             + ", ".join(sorted({s['source'] for s in d['sources']}))
                             + f"; impressions {d['evidence']['gsc_impressions']}")
    if section == "facts":
        for f in plan["facts"]:
            lines.append(f"- {f['subject_ref']}: {f['readiness']} (stale "
                         f"{', '.join(f['stale_required']) or '—'}; missing "
                         f"{', '.join(f['missing_required']) or '—'})")
    return "\n".join(lines)


def render_schedule(plan: dict) -> str:
    return "\n".join([f"nightly schedule plan: {plan['task_name']} at {plan['start_time']} "
                      f"{plan['timezone']} ({plan['days']})", f"registered: {plan['registered']}",
                      "command (not run): " + " ".join(plan["schtasks_arguments"]),
                      f"rationale: {plan['rationale']}", plan["note"]])


def render_history(rows: list[dict]) -> str:
    if not rows:
        return "no nightly analysis run recorded (tables missing or never executed)"
    return "\n".join(f"- {r['run_key']} {r['status']} (attempt {r['attempt_count']}): analyzed "
                     f"{r['analyzed']}/{r['eligible']}, refresh {r['refresh_required']}"
                     + (f", failure: {r['failure_reason']}" if r["failure_reason"] else "")
                     for r in rows)


if __name__ == "__main__":
    raise SystemExit(main())
