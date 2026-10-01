"""管理用 CLI: N7 の試しの利用者の記録。**既定は PLAN (書かない)。外に送らない。数を作らない。**

    # 本物の試しの利用者を登録する (人が合意をリポジトリの外で持っていることを確かめてから)
    uv run python scripts/manage_pilots.py register pilot-01 --started-at 2026-10-10T10:00:00+09:00
        --use-case "記事と投稿の承認の流れ" --acquisition own_network --agreement-confirmed
        --source "pilot agreement (kept by the human)" --by human [--execute]

    # 観測・声・状態 (どれも追記だけ。--evidence-kind で証拠の種類を書く)
    uv run python scripts/manage_pilots.py onboarding pilot-01 --state completed ... [--execute]
    uv run python scripts/manage_pilots.py usage|outcome|feedback pilot-01 --note "..." ...
    uv run python scripts/manage_pilots.py blocker pilot-01 --category setup --note "..." ...
    uv run python scripts/manage_pilots.py close pilot-01 --note "..." ...
    uv run python scripts/manage_pilots.py withdraw pilot-01 --reason no_time ...
    uv run python scripts/manage_pilots.py correct pilot-01 --supersedes 3 --reason "..." ...

    # 読むだけ
    uv run python scripts/manage_pilots.py show pilot-01
    uv run python scripts/manage_pilots.py list
    uv run python scripts/manage_pilots.py summary     # 数・無いもの・N8 に進めるか

数 (activated・active_days・value_rating・willingness_to_pay_jpy・payment_received_jpy など) は
今までどおり ``record_manual_metric.py record --kind pilot --ref pilot-01 ...`` で記録する。
共通の引数: ``--observed-at`` (人が見た・聞いた時刻、タイムゾーンつき)・``--evidence-kind``
(observed_fact / human_reported / measured_metric / inference / hypothesis)・``--source``・
``--by``。
出来事は ``data/n7/pilot_events.jsonl`` (git 管理外) に足す。名前・メール・電話・URL は拒む。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

EVENT_COMMANDS = ("register", "onboarding", "usage", "outcome", "feedback", "blocker", "close",
                  "withdraw", "correct")  # fmt: skip


def _parser() -> argparse.ArgumentParser:
    from app.n_track import pilot_registry as reg

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in EVENT_COMMANDS:
        cmd = sub.add_parser(name)
        cmd.add_argument("pilot")
        cmd.add_argument("--observed-at", help="when the human saw / heard it (default: now)")
        cmd.add_argument("--evidence-kind", choices=reg.EVIDENCE_KINDS, default="human_reported")
        cmd.add_argument("--source", required=True)
        cmd.add_argument("--by", required=True)
        cmd.add_argument("--note")
        cmd.add_argument("--execute", action="store_true")
        if name == "register":
            cmd.add_argument("--started-at", required=True)
            cmd.add_argument("--use-case", required=True)
            cmd.add_argument("--acquisition", choices=reg.ACQUISITION, required=True)
            cmd.add_argument("--agreement-confirmed", action="store_true",
                             help="the human holds the pilot's agreement outside this repo")
        elif name == "onboarding":
            cmd.add_argument("--state", choices=reg.ONBOARDING_STATES, required=True)
        elif name == "blocker":
            cmd.add_argument("--category", choices=reg.BLOCKERS, required=True)
        elif name == "withdraw":
            cmd.add_argument("--reason", choices=reg.WITHDRAW_REASONS, required=True)
        elif name == "correct":
            cmd.add_argument("--supersedes", type=int, required=True)
            cmd.add_argument("--reason", required=True)
    show = sub.add_parser("show")
    show.add_argument("pilot")
    sub.add_parser("list")
    sub.add_parser("summary")
    return parser


def _data(args) -> dict:
    if args.command == "register":
        return {"started_at": args.started_at, "use_case": args.use_case,
                "acquisition": args.acquisition, "agreement_confirmed": args.agreement_confirmed}
    if args.command == "onboarding":
        return {"state": args.state, "note": args.note}
    if args.command == "blocker":
        return {"category": args.category, "note": args.note}
    if args.command == "withdraw":
        return {"reason": args.reason, "note": args.note}
    if args.command == "correct":
        return {"supersedes": args.supersedes, "reason": args.reason}
    return {"note": args.note}


def _metric_rows(session_factory) -> list[dict]:
    from app.services.manual_metrics_service import ManualMetricsService

    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    with session_factory() as session:
        return ManualMetricsService(session).active_rows()


def main(argv=None, *, path: Path | None = None, session_factory=None,
         now: datetime | None = None) -> int:  # fmt: skip
    from app.n_track import pilot_registry as reg
    from app.services.note_ledger_service import NoteLedgerError
    from app.services.pilot_registry_service import DEFAULT_PATH, PilotRegistryService

    args = _parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    now = now or datetime.now(UTC)
    service = PilotRegistryService(path or DEFAULT_PATH)
    try:
        if args.command in EVENT_COMMANDS:
            type_ = "correction" if args.command == "correct" else args.command
            result = service.add(pilot=args.pilot, type_=type_,
                                 observed_at=args.observed_at or now.isoformat(),
                                 evidence_kind=args.evidence_kind, source=args.source,
                                 entered_by=args.by, data=_data(args), execute=args.execute,
                                 now=now)  # fmt: skip
        elif args.command == "show":
            result = service.show(args.pilot)
        elif args.command == "list":
            result = service.list()
        else:
            result = service.report(_metric_rows(session_factory))
    except (reg.PilotError, NoteLedgerError, ValueError) as exc:
        print(f"refused: {exc}")
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if args.command in EVENT_COMMANDS:
        print("local only: nothing was sent to anyone")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
