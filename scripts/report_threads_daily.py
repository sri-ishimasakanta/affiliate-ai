"""管理用 CLI: 今日の Threads 運用のまとめを日本語で表示する (T6.4、**読むだけ**)。

    uv run python scripts/report_threads_daily.py
    uv run python scripts/report_threads_daily.py --date 2026-09-28
    uv run python scripts/report_threads_daily.py --json

DB と Growth Post の記録を読むだけ。Threads にも OpenAI にも問い合わせず、メールも送らない
(毎日メールで送るかどうかは、別に人が決める)。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.operations.report_format import render_threads_daily_summary  # noqa: E402
from app.services.threads_report_service import ThreadsReportService  # noqa: E402

EXIT_OK = 0


def main(argv: list[str] | None = None, *, session_factory=None, now: datetime | None = None,
         growth_directory=None) -> int:  # fmt: skip
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="対象の日 (JST、YYYY-MM-DD。既定は今日)")
    parser.add_argument("--json", action="store_true", help="素材を JSON で出す")
    args = parser.parse_args(argv)

    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    from app.operations.policy import get_policy

    tz = get_policy().timezone
    now = now or datetime.now(UTC)
    day = date.fromisoformat(args.date) if args.date else now.astimezone(tz).date()
    options = {"growth_directory": growth_directory} if growth_directory else {}
    with session_factory() as session:
        summary = ThreadsReportService(session, timezone=tz, **options).summary(
            start=day, end=day, now=now
        )
        session.rollback()
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_threads_daily_summary(summary))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
