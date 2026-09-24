"""管理用 CLI: Threads queue の人の操作 (T4.2)。

    uv run python scripts/manage_threads_queue.py show <id>
    uv run python scripts/manage_threads_queue.py hold <id> --reason "..."
    uv run python scripts/manage_threads_queue.py release <id> [--reason "..."]
    uv run python scripts/manage_threads_queue.py prefer <id> [--reason "..."]
    uv run python scripts/manage_threads_queue.py unprefer <id> [--reason "..."]
    uv run python scripts/manage_threads_queue.py timing <id> \\
        [--not-before 2026-10-01T09:00] [--expires-at 2026-10-03T21:00] [--clear] --reason "..."

時刻に タイムゾーンが無ければ Asia/Tokyo とみなす。保存は UTC。

**「次に優先」は並び順の希望にすぎない。** 却下・stale・期限切れ・not_before・
中身の不整合・T3 の不確定な公開・公開窓・間隔・公開済み のどれも飛ばさない。

どの操作も、誰が・いつ・なぜ行ったかを履歴に残す。提案の本文には触れない。
Threads にも WordPress にも書き込まない。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.article.fact_freshness import ensure_aware  # noqa: E402
from app.config.database import SessionLocal  # noqa: E402
from app.operations.local_time import to_local  # noqa: E402
from app.operations.policy import get_policy  # noqa: E402
from app.services.threads_queue_control_service import (  # noqa: E402
    ThreadsQueueControlError,
    ThreadsQueueControlService,
)

EXIT_OK = 0
EXIT_REFUSED = 2


def main(argv: list[str] | None = None, *, session_factory=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("show", "hold", "release", "prefer", "unprefer", "timing"):
        cmd = sub.add_parser(name)
        cmd.add_argument("proposal_id", type=int)
        cmd.add_argument("--reason", default=None)
        if name == "timing":
            cmd.add_argument("--not-before", default=None)
            cmd.add_argument("--expires-at", default=None)
            cmd.add_argument("--clear", action="store_true", help="両方の制約を外す (常緑)")
    args = parser.parse_args(argv)

    tz = get_policy().timezone
    with (session_factory or SessionLocal)() as session:
        service = ThreadsQueueControlService(session)
        try:
            if args.command == "hold":
                service.hold(args.proposal_id, reason=args.reason or "")
            elif args.command == "release":
                service.release(args.proposal_id, reason=args.reason)
            elif args.command == "prefer":
                service.prefer_next(args.proposal_id, reason=args.reason)
            elif args.command == "unprefer":
                service.clear_preference(args.proposal_id, reason=args.reason)
            elif args.command == "timing":
                not_before = None if args.clear else _parse(args.not_before, tz)
                expires_at = None if args.clear else _parse(args.expires_at, tz)
                service.set_timing(
                    args.proposal_id,
                    not_before=not_before,
                    expires_at=expires_at,
                    reason=args.reason,
                )
        except ThreadsQueueControlError as exc:
            print(f"refused: {exc.reason}")
            return EXIT_REFUSED

        proposal = service._require(args.proposal_id)
        print(f"=== threads proposal #{proposal.id} ===")
        print(f"status        = {proposal.status}")
        print(f"angle         = {proposal.angle}")
        print(f"held          = {_local(proposal.held_at, tz)} {proposal.hold_reason or ''}")
        print(f"preferred     = {_local(proposal.preferred_at, tz)}")
        print(f"not_before    = {_local(proposal.not_before, tz)}")
        print(f"expires_at    = {_local(proposal.expires_at, tz)}")
        print(f"approved_at   = {_local(proposal.approved_at, tz)}")
        print(f"request sent  = {_local(proposal.approval_request_sent_at, tz)}")
        print("\n--- history ---")
        for event in service.history(proposal.id):
            print(
                f"  {_local(event.created_at, tz)} {event.action:16} by {event.actor}"
                f" {event.reason or ''}".rstrip()
            )
    return EXIT_OK


def _parse(value: str | None, tz) -> datetime | None:
    if not value:
        return None
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=tz)


def _local(moment: datetime | None, tz) -> str:
    return to_local(ensure_aware(moment), tz).strftime("%Y-%m-%d %H:%M %Z") if moment else "-"


if __name__ == "__main__":
    raise SystemExit(main())
