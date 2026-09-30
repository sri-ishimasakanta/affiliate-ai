"""手で写した数を記録する (N3。N4〜N7 も使う)。**推定しない。provider に問い合わせない。**

    uv run python scripts/record_manual_metric.py catalog
    uv run python scripts/record_manual_metric.py record --kind note_piece --ref <draft_id>
        --metric views --value 120 --observed-at 2026-10-08T21:00:00+09:00
        --period 2026-10-01:2026-10-07 --source "note dashboard" --by <name> [--execute]
    uv run python scripts/record_manual_metric.py list [--kind note_piece]
    uv run python scripts/record_manual_metric.py summary --kind note_piece --metric views

``record`` は既定で PLAN (検査だけ)。0 も記録してよい (0 は結果であって成功ではない)。
表が無ければ止まる (migration a4a74a5bcb8b の本番への適用は人が決める)。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _period(text: str | None) -> tuple[date | None, date | None]:
    if not text:
        return None, None
    start, _, end = text.partition(":")
    return date.fromisoformat(start), date.fromisoformat(end or start)


def main(argv=None, *, session_factory=None, now=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("catalog")
    rec = sub.add_parser("record")
    for name in ("--kind", "--ref", "--metric", "--observed-at", "--source", "--by"):
        rec.add_argument(name, required=True)
    rec.add_argument("--value", required=True, type=float)
    rec.add_argument("--period")
    rec.add_argument("--note")
    rec.add_argument("--supersedes", type=int)
    rec.add_argument("--execute", action="store_true")
    lst = sub.add_parser("list")
    lst.add_argument("--kind")
    summ = sub.add_parser("summary")
    summ.add_argument("--kind", required=True)
    summ.add_argument("--metric", required=True)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    from app.n_track import metrics as mm

    if args.command == "catalog":
        print(json.dumps(mm.CATALOG, ensure_ascii=False, indent=2))
        return 0
    from app.services.manual_metrics_service import ManualMetricsService
    from app.services.note_ledger_service import NoteLedgerError

    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    with session_factory() as session:
        try:
            service = ManualMetricsService(session)
            if args.command == "record":
                start, end = _period(args.period)
                entry = mm.MetricInput(
                    subject_kind=args.kind, subject_ref=args.ref, metric=args.metric,
                    value=args.value, observed_at=datetime.fromisoformat(args.observed_at),
                    source_description=args.source, entered_by=args.by, period_start=start,
                    period_end=end, note=args.note)  # fmt: skip
                result = service.record(entry, execute=args.execute,
                                        supersedes=args.supersedes, now=now)  # fmt: skip
            elif args.command == "list":
                result = [r for r in service.active_rows()
                          if not args.kind or r["subject_kind"] == args.kind]  # fmt: skip
            else:
                result = service.summary(args.kind, args.metric)
        except (NoteLedgerError, mm.MetricError, ValueError) as exc:
            print(f"refused: {exc}")
            return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
