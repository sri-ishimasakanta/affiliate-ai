"""発見の候補の管理 (C10-3)。**既定は読むだけ。Keyword にするのは人が 1 件ずつ。**

    uv run python scripts/manage_discovery_candidates.py plan               # 3〜5 件 (理由つき)
    uv run python scripts/manage_discovery_candidates.py list [--status tracked]
    uv run python scripts/manage_discovery_candidates.py promote <id> --fingerprint <sha> --execute
    uv run python scripts/manage_discovery_candidates.py dismiss <id> --reason "..." --execute

``promote`` は既存の Keyword の作り方で Keyword を 1 つ作るだけ (保存済みの Google Ads の値が
あれば signal も。呼び直さない)。記事・計画の依頼・Growth の承認は作らない。まとめて全部を
承認する入口は無い。``dismiss`` した候補は夜の分析でも戻らない。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main(argv=None, *, session_factory=None, settings=None, intelligence=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--limit", type=int, default=5)
    lst = sub.add_parser("list")
    lst.add_argument("--status", default=None)
    pro = sub.add_parser("promote")
    pro.add_argument("candidate_id", type=int)
    pro.add_argument("--fingerprint", required=True)
    pro.add_argument("--execute", action="store_true")
    dis = sub.add_parser("dismiss")
    dis.add_argument("candidate_id", type=int)
    dis.add_argument("--reason", required=True)
    dis.add_argument("--execute", action="store_true")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    args = parser.parse_args(argv)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass
    from sqlalchemy import select

    from app.models.content_discovery import CDC_DISMISSED, ContentDiscoveryCandidate
    from app.services.discovery_promotion_service import (
        DiscoveryPromotionError,
        DiscoveryPromotionService,
    )
    from app.services.nightly_analysis_service import tables_ready

    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    with session_factory() as session:
        if not tables_ready(session):
            print("refused: discovery tables are missing (migration c1d0e233e180)")
            return 2
        service = DiscoveryPromotionService(session, settings=settings,
                                            intelligence=intelligence)
        if args.command == "plan":
            result = service.plan(limit=args.limit)
            if args.format == "json":
                print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            else:
                print(f"discovery promotion plan: showing {len(result['shown'])} of "
                      f"{result['eligible']} eligible ({result['tracked']} tracked, "
                      f"{result['blocked_by_cannibalization']} blocked by overlap)")
                for v in result["shown"]:
                    print(f"- [{v['candidate_id']}] {v['phrase']} ({v['cluster_id'] or '—'}; "
                          f"{v['content_type']}) — {v['why']}")
                    print(f"    {v['command']}")
            print("read-only: database writes = 0, external calls = 0")
            return 0
        if args.command == "list":
            q = select(ContentDiscoveryCandidate).order_by(ContentDiscoveryCandidate.id)
            if args.status:
                q = q.where(ContentDiscoveryCandidate.status == args.status)
            for r in session.scalars(q):
                print(f"- [{r.id}] {r.phrase} {r.status} ({r.duplicate_state}; cluster "
                      f"{r.cluster_key or '—'}; seen {r.seen_count})")
            return 0
        if not args.execute:
            print(f"PLAN: {args.command} would write one local row; re-run with --execute")
            return 0
        if args.command == "promote":
            try:
                result = service.promote(args.candidate_id,
                                         expected_fingerprint=args.fingerprint)
            except DiscoveryPromotionError as exc:
                print(f"refused: {exc.reason}")
                return 2
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        row = session.get(ContentDiscoveryCandidate, args.candidate_id)
        if row is None:
            print(f"refused: candidate {args.candidate_id} does not exist")
            return 2
        row.status, row.updated_at = CDC_DISMISSED, datetime.now(UTC)
        row.evidence_json = {**(row.evidence_json or {}), "dismissed": {
            "reason": args.reason, "at": datetime.now(UTC).isoformat()}}
        session.commit()
        print(f"candidate {row.id} dismissed (it will not come back)")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
