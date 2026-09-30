"""note の記録 (N2)。**書くのは手元の DB の ``note_pieces`` だけ (``sync --execute``)。**

    uv run python scripts/manage_note_ledger.py sync                 # PLAN (何を同期するか)
    uv run python scripts/manage_note_ledger.py sync --execute       # reports/note/drafts → DB
    uv run python scripts/manage_note_ledger.py status               # 流れと人の次の一手
    uv run python scripts/manage_note_ledger.py cadence              # 週ごとの公開と目安
    uv run python scripts/manage_note_ledger.py links <draft_id>     # 関係する公開済みの記事

note・WordPress・Threads には問い合わせない。リンクは示すだけ (入れるのは人が本文を直すとき)。
表が無ければ止まる (migration a4a74a5bcb8b の本番への適用は人が決める)。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main(argv=None, *, root: Path = ROOT, session_factory=None, now=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sync").add_argument("--execute", action="store_true")
    sub.add_parser("status")
    sub.add_parser("cadence")
    sub.add_parser("links").add_argument("draft_id")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    from app.services.note_ledger_service import NoteLedgerError, NoteLedgerService
    from app.social.note import review

    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    with session_factory() as session:
        try:
            service = NoteLedgerService(session)
        except NoteLedgerError as exc:
            print(f"refused: {exc}")
            return 2
        if args.command == "sync":
            result = service.sync_from_drafts(root, execute=args.execute, now=now)
        elif args.command == "status":
            result = service.loop_status()
        elif args.command == "cadence":
            result = service.cadence_plan(now=now)
        else:
            path = root / "reports/note/drafts" / f"{args.draft_id}.json"
            if not path.exists():
                print(f"refused: no local draft {args.draft_id}")
                return 2
            draft = review.draft_from_dict(json.loads(path.read_text(encoding="utf-8")))
            result = {"draft_id": draft.id, "related": service.related_links(draft),
                      "note": "suggestions only; a link enters the body only through a human "
                              "edit and the link approval"}  # fmt: skip
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if args.command == "sync" and not args.execute:
        print("PLAN: nothing was written (re-run with --execute)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
