"""管理用 CLI: Growth Post の生成の確かさと書き方の偏り (T6.3.3c、**読むだけ**)。

    uv run python scripts/report_threads_growth_reliability.py
    uv run python scripts/report_threads_growth_reliability.py --days 30 --json

DB とその日ごとの記録 (``data/threads-growth``) を読むだけ。OpenAI にも Threads にも触れない。
結果は **記述** (フォロワー・表示回数の原因は言わない)。生成には戻さない。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.threads_growth_service import (  # noqa: E402
    DEFAULT_DIRECTORY,
    ThreadsGrowthService,
)

EXIT_OK = 0


def render(report: dict) -> str:
    r, d = report["reliability"], report["diversity"]
    lines = [
        f"Growth Post の生成 (読むだけ・直近 {report['window_days']} 日・"
        f"{report['strategy_policy_version']}・1 日の呼び出しの上限 "
        f"{report['max_model_calls_per_jst_day']})",
        f"  生成した日 {r['eligible_growth_days']} / 提案ができた日 {r['valid_proposal_days']} / "
        f"提案なしで終わった日 {r['generation_exhausted_days']} "
        f"(成功の割合 {r['proposal_success_rate']})",
        f"  呼び出し {r['total_model_calls']} 回 (候補 {r['candidates_generated']} 本・"
        f"似すぎ {r['similarity_rejections']}・書き直し {r['format_repairs']}・"
        f"別の書き方 {r['strategy_retries']})・提案 1 本あたり "
        f"{r['average_calls_per_valid_proposal']} 回",
        f"  書き方 (最近 {d['recent_proposals']} 本、書き方の記録あり {d['with_strategy']} 本): "
        f"family {d['family_including_legacy_angle']}",
        f"  hook {d['hook']} / CTA {d['cta']} / structure {d['structure']}",
        f"  同じ書き方の繰り返し {d['repeated_signatures'] or 'なし'} / "
        f"最近の最大の類似 {d['recent_max_similarity']}",
    ]
    for row in report["days"]:
        lines.append(f"  {row['date_jst']}: {row['outcome']} 呼び出し {row['model_calls']} "
                     f"提案 {row['proposal_id'] or '-'} {row['strategies']}")  # fmt: skip
    lines += [f"  ※ {note}" for note in report["notes"]]
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, session_factory=None, now: datetime | None = None,
         directory: Path | None = None) -> int:  # fmt: skip
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=14, help="見る日数 (既定 14)")
    parser.add_argument("--json", action="store_true", help="JSON で出す")
    args = parser.parse_args(argv)
    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    from zoneinfo import ZoneInfo

    with session_factory() as session:
        service = ThreadsGrowthService(session, timezone=ZoneInfo("Asia/Tokyo"), client=None,
                                       directory=directory or DEFAULT_DIRECTORY)  # fmt: skip
        report = service.reliability(now=now or datetime.now(UTC), days=args.days)
        session.rollback()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str) if args.json
          else render(report))  # fmt: skip
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
