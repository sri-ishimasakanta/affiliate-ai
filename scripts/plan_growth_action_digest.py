"""Growth Action のまとめ (digest) の PLAN (C9 Batch 2)。**送らない・書かない。**

人が見る少数の候補を選ぶだけ (いま動ける候補から、行動の種類ごとの上限つきで)。メールは送らない。
Threads の提案の承認のまとめとは別物 (同じ考え方: 少数をまとめて見る)。

    uv run python scripts/plan_growth_action_digest.py
    uv run python scripts/plan_growth_action_digest.py --limit 6 --per-type 2 --format json
    uv run python scripts/plan_growth_action_digest.py --include-unreviewed
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.growth.inbox import DIGEST_LIMIT, DIGEST_PER_ACTION, select_digest  # noqa: E402
from app.services.growth_action_service import (  # noqa: E402
    NEW,
    NEW_REVISION,
    build_inbox,
    filter_entries,
    inbox_counts,
)


def main(argv=None, *, session_factory=None, settings=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", dest="as_of")
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--limit", type=int, default=DIGEST_LIMIT)
    parser.add_argument("--per-type", type=int, default=DIGEST_PER_ACTION, dest="per_type")
    parser.add_argument("--include-unreviewed", action="store_true", dest="include_unreviewed",
                        help="前の評価から残っている (新しくない) 候補も入れる")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    args = parser.parse_args(argv)
    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    if settings is None:
        from app.config.settings import get_settings

        settings = get_settings()
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
    with session_factory() as session:
        box = build_inbox(session, settings=settings, now=as_of, days=args.days)
        session.rollback()
    plan = digest_plan(box, limit=args.limit, per_type=args.per_type,
                       include_unreviewed=args.include_unreviewed)  # fmt: skip
    if args.format == "json":
        print(json.dumps(plan, ensure_ascii=False, indent=2, default=str))
    else:
        print(render(plan))
    print("PLAN only: emails = 0, database writes = 0, WordPress writes = 0, Threads writes = 0")
    return 0


def digest_plan(box: dict, *, limit: int, per_type: int, include_unreviewed: bool) -> dict:
    entries = box["entries"]
    pool = [e for e in filter_entries(entries)
            if e["status"] == "active"
            and (include_unreviewed or e["decision"] in (NEW, NEW_REVISION))]  # fmt: skip
    selected, skipped = select_digest(pool, limit=limit, per_action=per_type)
    counts = inbox_counts(entries)
    return {
        "as_of": box["as_of"], "history_source": box["history_source"],
        "limit": limit, "per_type": per_type,
        "counts": {**counts, "eligible_for_digest": len(pool), "selected": len(selected),
                   "not_selected": len(skipped)},
        "selected": [_brief(e, e["selection_reason"]) for e in selected],
        "not_selected": [_brief(e, e["not_selected_reason"]) for e in skipped],
        "notes": ["no single score: the five priority components are shown",
                  "the digest is not sent in this batch"],
    }  # fmt: skip


def _brief(e: dict, reason: str) -> dict:
    return {"id": e.get("id"), "opportunity_key": e["opportunity_key"],
            "action_type": e["action_type"], "subject_id": e["subject_id"],
            "variant": e.get("variant"), "evidence_state": e["evidence_state"],
            "priority": {k: v["level"] for k, v in e["priority"].items()},
            "rationale": e["rationale"], "blockers": list(e.get("blockers") or ()),
            "evidence": [ev["reason"] for ev in e["evidence"]], "reason": reason,
            "candidate_fingerprint": e["candidate_fingerprint"]}  # fmt: skip


def render(plan: dict) -> str:
    c = plan["counts"]
    lines = [f"Growth Action digest PLAN (not sent) — as of {plan['as_of']} "
             f"(history: {plan['history_source']})",
             f"actionable {c['actionable']}, eligible {c['eligible_for_digest']}, "
             f"selected {c['selected']} (limit {plan['limit']}, per type {plan['per_type']}); "
             f"suppressed {c['suppressed']}, superseded {c['superseded_by_this_refresh']}, "
             f"informational {c['informational']}, covered {c['covered_by_existing_work']}, "
             f"blocked {c['blocked']}", ""]  # fmt: skip
    for i, e in enumerate(plan["selected"], 1):
        comps = ", ".join(f"{k}={v}" for k, v in e["priority"].items())
        lines += [f"{i}. {e['action_type']} {e['subject_id']}"
                  f"{' ' + e['variant'] if e['variant'] else ''} [{e['evidence_state']}]",
                  f"   why selected: {e['reason']}", f"   components: {comps}",
                  f"   evidence: {'; '.join(e['evidence'])}"]  # fmt: skip
        if e["blockers"]:
            lines.append(f"   blockers: {'; '.join(e['blockers'])}")
    if not plan["selected"]:
        lines.append("(nothing new to review)")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
