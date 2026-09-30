"""SaaS の検証の証拠 (N7)。**読むだけ。決めない。数を作らない。**

    uv run python scripts/pilot_evidence.py summary

記録 (``record_manual_metric.py record --kind pilot --ref pilot-01 ...``) から、go / no-go の
条件ごとの状態と、SaaS / Managed / Hybrid の兆しを出す。試しの利用者がまだいなければ
``insufficient_evidence``。条件の値は ``app/config/pilot_policy.json`` (人が決める案)。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main(argv=None, *, session_factory=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("summary",))
    parser.parse_args(argv)
    from app.n_track import pilot
    from app.services.manual_metrics_service import ManualMetricsService
    from app.services.note_ledger_service import NoteLedgerError

    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    with session_factory() as session:
        try:
            rows = ManualMetricsService(session).active_rows()
        except NoteLedgerError as exc:
            print(f"refused: {exc}")
            return 2
    print(json.dumps(pilot.evaluate(rows, policy=pilot.load_policy()), ensure_ascii=False,
                     indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
