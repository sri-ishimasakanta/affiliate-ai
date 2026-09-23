"""管理用 CLI: 週次運用レポートのプレビュー (C8.7)。

    # 表示するだけ (既定。DB にも外部にも書かない)
    uv run python scripts/report_weekly_operations.py

    # 機械可読な出力
    uv run python scripts/report_weekly_operations.py --json weekly.json

    # 実際にメールで送る (メールが設定されているときのみ)
    uv run python scripts/report_weekly_operations.py --send

**既定では決して送らない。** ここで表示される本文は、週次メールが送るものと
同一である (同じ renderer を使う)。

レポートは既存の永続化済みの事実を読むだけで、新しい分析は行わない。
欠測は ``unavailable`` と書き、0 で埋めない。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.database import SessionLocal  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.operations.report_format import render_weekly_report, weekly_subject  # noqa: E402
from app.services.operations_weekly_report_service import (  # noqa: E402
    DEFAULT_PERIOD_DAYS,
    OperationsWeeklyReportService,
)

EXIT_OK = 0
EXIT_NOT_SENT = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period-days", type=int, default=DEFAULT_PERIOD_DAYS)
    parser.add_argument("--operations-run-id", type=int, help="対象の週次 run (既定は最新)")
    parser.add_argument("--json", dest="json_path", help="結果を JSON で書き出すパス")
    parser.add_argument("--send", action="store_true", help="メールで送る (既定は送らない)")
    args = parser.parse_args(argv)

    settings = get_settings()
    with SessionLocal() as session:
        service = OperationsWeeklyReportService(session, settings=settings)
        report = service.build(
            operations_run_id=args.operations_run_id, period_days=args.period_days
        )
        body = render_weekly_report(report)
        print(f"subject: {weekly_subject(report)}")
        print()
        print(body)

        if args.json_path:
            Path(args.json_path).write_text(
                json.dumps(report.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"\nJSON written to {args.json_path}")

        if not args.send:
            print("\nプレビューのみ。送るには --send を付ける (メール設定が必要)。")
            return EXIT_OK

        from app.services.operations_notification_service import (
            OperationsNotificationService,
        )

        outcome = OperationsNotificationService(session, settings=settings).send_weekly_report(
            operations_run_id=args.operations_run_id, report=report
        )
        print(f"\nsent      = {outcome.sent}")
        if outcome.reason:
            print(f"reason    = {outcome.reason}")
        if outcome.delivery_id:
            print(f"delivery  = #{outcome.delivery_id}")
        return EXIT_OK if outcome.sent else EXIT_NOT_SENT


if __name__ == "__main__":
    raise SystemExit(main())
