"""Growth Action の受け箱 (C9 Batch 2)。**既定は読むだけ。**

    uv run python scripts/manage_growth_actions.py list
    uv run python scripts/manage_growth_actions.py list --all --action-type review_internal_links
    uv run python scripts/manage_growth_actions.py show <id|opportunity_key>
    uv run python scripts/manage_growth_actions.py explain <id|opportunity_key>
    uv run python scripts/manage_growth_actions.py history <id>
    uv run python scripts/manage_growth_actions.py refresh            # PLAN (書かない)
    uv run python scripts/manage_growth_actions.py refresh --execute  # C9 の履歴の表だけに書く
    uv run python scripts/manage_growth_actions.py review <id> --execute
    uv run python scripts/manage_growth_actions.py approve <review_id> --fingerprint <sha> --execute
    uv run python scripts/manage_growth_actions.py reject <review_id> --fingerprint <sha> \\
        --reason "..." --execute
    uv run python scripts/manage_growth_actions.py dismiss <id> --reason "..." --execute

書くのは ``--execute`` のときの C9 の 3 つの表 (``growth_action_*``) だけ。**承認は次の段階へ
進めてよいという許可だけ** で、WordPress・Threads・公開・アフィリエイトの設定・メール・外の API に
触れない。履歴の表が無い DB (migration 前) では、``list`` / ``show`` / ``explain`` / ``refresh``
(PLAN) だけが動く。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.growth.analysis import ACTION_TYPES, EVIDENCE_STATES  # noqa: E402
from app.growth.conversion import plan_conversion  # noqa: E402
from app.growth.inbox import group_by_action  # noqa: E402
from app.models.growth_action import GA_STATUSES  # noqa: E402
from app.services.growth_action_service import (  # noqa: E402
    GrowthActionError,
    GrowthActionHistory,
    GrowthActionReviewService,
    build_inbox,
    filter_entries,
    inbox_counts,
)

SIDE_EFFECTS = ("WordPress writes = 0, Threads writes = 0, publications = 0, emails = 0, "
                "external calls = 0")


def main(argv=None, *, session_factory=None, settings=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--as-of", dest="as_of")
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--format", choices=("table", "json"), default="table")
    sub = parser.add_subparsers(dest="command", required=True)
    lst = sub.add_parser("list")
    lst.add_argument("--all", action="store_true", help="重複・覆われたもの・情報も出す")
    lst.add_argument("--status", choices=GA_STATUSES)
    lst.add_argument("--action-type", choices=ACTION_TYPES, dest="action_type")
    lst.add_argument("--article-id", type=int, dest="article_id")
    lst.add_argument("--keyword-id", type=int, dest="keyword_id")
    lst.add_argument("--evidence-state", choices=EVIDENCE_STATES, dest="evidence_state")
    lst.add_argument("--actionable-only", action="store_true", dest="actionable_only")
    lst.add_argument("--requires-review", action="store_true", dest="requires_review")
    lst.add_argument("--per-type", type=int, default=3, dest="per_type",
                     help="表で 1 つの行動の種類に出す件数 (残りは件数だけ)")
    for name in ("show", "explain", "history"):
        sub.add_parser(name).add_argument("target")
    ref = sub.add_parser("refresh")
    ref.add_argument("--execute", action="store_true")
    rev = sub.add_parser("review")
    rev.add_argument("candidate_id", type=int)
    rev.add_argument("--execute", action="store_true")
    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("review_id", type=int)
        p.add_argument("--fingerprint", required=True, help="レビューに見せた版の指紋")
        p.add_argument("--reason", default=None)
        p.add_argument("--execute", action="store_true")
    dis = sub.add_parser("dismiss")
    dis.add_argument("candidate_id", type=int)
    dis.add_argument("--reason", required=True)
    dis.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)

    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    if settings is None:
        from app.config.settings import get_settings

        settings = get_settings()
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
    with session_factory() as session:
        try:
            code = _run(args, session, settings, as_of)
        except GrowthActionError as exc:
            session.rollback()
            print(f"refused: {exc.reason}")
            code = 2
    print(f"side effects: {SIDE_EFFECTS}")
    return code


def _emit(args, payload, table: str) -> None:
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(table)


def _find(entries: list[dict], target: str) -> dict:
    for e in entries:
        if str(e.get("id")) == target or e["opportunity_key"] == target:
            return e
    raise GrowthActionError(f"no current growth action matches {target!r}")


def _run(args, session, settings, as_of) -> int:
    command = args.command
    if command in ("list", "show", "explain", "refresh"):
        box = build_inbox(session, settings=settings, now=as_of, days=args.days)
        entries = box["entries"]
        if command == "list":
            shown = filter_entries(
                entries, status=args.status, action_type=args.action_type,
                article_id=args.article_id, keyword_id=args.keyword_id,
                evidence_state=args.evidence_state, actionable_only=args.actionable_only,
                requires_review=args.requires_review, include_all=args.all)  # fmt: skip
            _emit(args, {"as_of": box["as_of"], "history_source": box["history_source"],
                         "counts": inbox_counts(entries), "entries": shown},
                  render_list(box, shown, per_type=args.per_type))  # fmt: skip
            return 0
        if command == "refresh":
            plan = box["plan"]
            payload = {"plan": plan.as_dict(), "executed": False, "written": None}
            if args.execute:
                payload["written"] = GrowthActionHistory(session).apply_refresh(
                    plan, now=datetime.fromisoformat(box["as_of"]))
                payload["executed"] = True
            _emit(args, payload, render_refresh(payload))
            return 0
        entry = _find(entries, args.target)
        if command == "show":
            _emit(args, entry, render_entry(entry))
        else:
            conversion = plan_conversion(entry).as_dict()
            _emit(args, {**entry, "conversion": conversion}, render_explain(entry, conversion))
        return 0
    history = GrowthActionHistory(session)
    reviews = GrowthActionReviewService(session)
    if command == "history":
        if not history.tables_ready():
            raise GrowthActionError("growth action history tables are missing (migration "
                                    "74bfaf6c9c9f is not applied)")
        payload = history.history(int(args.target))
        _emit(args, payload, json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0
    if not args.execute:
        print(f"PLAN: {command} would write only the growth_action_* tables; "
              "re-run with --execute")
        return 0
    if command == "review":
        review = reviews.request_review(args.candidate_id)
        print(f"review #{review.id} pending for growth action {review.candidate_id}; "
              f"fingerprint {review.candidate_fingerprint}")
    elif command == "approve":
        review = reviews.approve(args.review_id, expected_candidate_fingerprint=args.fingerprint,
                                 reason=args.reason)
        print(f"review #{review.id} approved (permission only; nothing was executed)")
    elif command == "reject":
        review = reviews.reject(args.review_id, expected_candidate_fingerprint=args.fingerprint,
                                reason=args.reason or "")
        print(f"review #{review.id} rejected")
    elif command == "dismiss":
        row = reviews.dismiss(args.candidate_id, reason=args.reason)
        print(f"growth action {row.id} dismissed")
    return 0


def _v(value) -> str:
    return "—" if value is None else str(value)


def _components(entry: dict) -> str:
    p = entry["priority"]
    return (f"evidence={p['evidence_strength']['level']} potential="
            f"{p['potential_opportunity']['level']} urgency={p['recency_urgency']['level']} "
            f"effort={p['effort']['level']} monetization={p['monetization_relevance']['level']}")


def render_list(box: dict, shown: list[dict], *, per_type: int) -> str:
    counts = inbox_counts(box["entries"])
    lines = [f"Growth Action inbox (history: {box['history_source']}) — as of {box['as_of']}",
             "counts: " + ", ".join(f"{k}={v}" for k, v in counts.items()), ""]
    for action, items in group_by_action(shown).items():
        lines.append(f"## {action} ({len(items)})")
        for e in items[:per_type]:
            variant = f" {e['variant']}" if e.get("variant") else ""
            lines.append(
                f"- [{_v(e.get('id'))}] {e['subject_id']}{variant}"
                f" | {e['status']}/{e['availability']} | rev {e['revision']} | "
                f"{e['evidence_state']} | {_components(e)}")  # fmt: skip
            if e.get("blockers"):
                lines.append(f"    blockers: {'; '.join(e['blockers'])}")
            if e.get("availability_reasons") and e["availability"] != "actionable_now":
                lines.append(f"    coverage: {'; '.join(e['availability_reasons'][:2])}")
        if len(items) > per_type:
            lines.append(f"  … +{len(items) - per_type} more (use --action-type {action})")
        lines.append("")
    if not shown:
        lines.append("(nothing to review)")
    return "\n".join(lines)


def render_entry(e: dict) -> str:
    lines = [f"growth action [{_v(e.get('id'))}] {e['opportunity_key']} (revision {e['revision']})",
             f"status {e['status']} / {e['availability']}; evidence {e['evidence_state']}",
             f"rationale: {e['rationale']}", f"priority: {_components(e)}",
             f"first seen {_v(e.get('first_seen_at'))}, last seen {_v(e.get('last_seen_at'))}, "
             f"seen {_v(e.get('seen_count'))}; review {_v(e.get('review'))}",
             f"candidate fingerprint {e['candidate_fingerprint']}",
             f"evidence fingerprint {e['evidence_fingerprint']}"]  # fmt: skip
    for ev in e["evidence"]:
        lines.append(f"- evidence ({ev['basis']}, {_v(ev['source_engine'])}): {ev['reason']}")
    for b in e.get("blockers") or ():
        lines.append(f"- blocker: {b}")
    for r in e.get("availability_reasons") or ():
        lines.append(f"- coverage: {r}")
    fresh = ", ".join(f"{k}={v['state']}" for k, v in (e.get("freshness") or {}).items())
    lines.append(f"freshness: {fresh}")
    return "\n".join(lines)


def render_explain(e: dict, conversion: dict) -> str:
    return "\n".join([
        render_entry(e), "",
        f"approval means: permission to proceed only (nothing is executed); "
        f"requires human approval: {e['requires_human_approval']}; external write needed "
        f"later: {_v(e['external_write_required'])}; reversible: {e['reversible']}",
        f"conversion: {conversion['support']} → {_v(conversion['target_workflow'])}",
        *[f"  - {s}" for s in conversion["steps"]],
        f"  note: {conversion['note']}",
    ])


def render_refresh(payload: dict) -> str:
    plan = payload["plan"]
    lines = [f"refresh {'EXECUTED' if payload['executed'] else 'PLAN'} — as of {plan['as_of']}; "
             f"history tables {'ready' if plan['tables_ready'] else 'missing (migration '
             + plan['required_revision'] + ' not applied)'}",
             "counts: " + json.dumps(plan["counts"], ensure_ascii=False)]  # fmt: skip
    if payload["written"]:
        w = payload["written"]
        lines.append(f"written: {len(w['created'])} new revision(s), {len(w['reobserved'])} "
                     f"re-observed, {len(w['not_observed'])} no longer observed")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
