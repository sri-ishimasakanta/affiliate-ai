"""管理用 CLI: 収益最適化候補の評価 (C7)。

    # 評価して表示するだけ (既定。DB にも WordPress にも Google にも書き込まない)
    uv run python scripts/report_revenue_optimization_candidates.py --days 30

    # 評価結果を append-only な履歴として残す
    uv run python scripts/report_revenue_optimization_candidates.py --days 30 \
        --execute --idempotency-key c7-baseline

    # 機械可読な出力
    uv run python scripts/report_revenue_optimization_candidates.py --json candidates.json

**記事本文・WordPress は一切変更しない。** ``--execute`` が書くのは
``revenue_optimization_runs`` / ``revenue_optimization_candidates`` だけ。

**live の ``/go/<token>`` は叩かない** -- そのリクエスト自体がアウトバウンド
クリック行を作り、計測を汚すため。リンクの健全性は DB の target / mapping の
状態から判定する。

閾値は ``app/config/revenue_policy.json`` にあり、運用上のヒューリスティックで
ある (普遍的な転換率のベンチマークではない)。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.database import SessionLocal  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.services.revenue_optimization_candidate_service import (  # noqa: E402
    RevenueOptimizationCandidateService,
)

EXIT_OK = 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="集計する日数 (既定 30)")
    parser.add_argument("--execute", action="store_true", help="評価結果を履歴として永続化する")
    parser.add_argument("--idempotency-key", help="同じ評価を二重に保存しないためのキー")
    parser.add_argument("--json", dest="json_path", help="結果を JSON で書き出すパス")
    args = parser.parse_args(argv)

    settings = get_settings()
    with SessionLocal() as session:
        service = RevenueOptimizationCandidateService(session, settings=settings)
        report = service.evaluate(days=args.days)

        print("=== revenue optimization candidates ===")
        print(f"generated_at           = {report.generated_at.isoformat()}")
        print(f"policy_version         = {report.policy_version}")
        print(f"window                 = {report.window_start} .. {report.window_end}")
        print(f"trusted click start    = {report.trusted_measurement_start_at}")
        print(
            f"ga4 data-through       = {report.ga4_data_through} "
            f"(configured={report.ga4_configured})"
        )
        print(
            f"click data-through     = {report.affiliate_click_data_through} "
            f"(clean: {report.clean_click_data_through})"
        )
        print(f"commission data-through= {report.commission_data_through}")
        print(f"windows overlap        = {report.windows_overlap}")
        print(
            f"articles               = {report.article_count} "
            f"(monetized {report.monetized_article_count})"
        )

        print("\n--- outbound clicks ---")
        print(f"  raw      = {report.raw_clicks}")
        print(f"  excluded = {report.excluded_clicks}  (instrumentation, retained for audit)")
        print(f"  clean    = {report.clean_clicks}")

        print("\n--- revenue maturity distribution ---")
        for state, count in sorted(report.maturity_counts.items()):
            print(f"  {state:32} {count}")

        print("\n--- candidates by type ---")
        if report.candidate_counts:
            for candidate_type, count in sorted(report.candidate_counts.items()):
                print(f"  {candidate_type:32} {count}")
        else:
            print("  (none)")
        print(f"  priority: {report.priority_counts}")

        print("\n--- per article ---")
        for evaluation in report.articles:
            sessions = "-" if evaluation.sessions is None else evaluation.sessions
            print(
                f"  #{evaluation.article_id:<3} {str(evaluation.monetization_mode):10} "
                f"{evaluation.maturity_state:30} monetized={str(evaluation.monetized):5} "
                f"sessions={sessions!s:>5} clean_clicks={evaluation.clean_clicks:<3} "
                f"candidates={len(evaluation.candidates)}"
            )
            for candidate in evaluation.candidates:
                print(
                    f"        [{candidate['priority']:6}] {candidate['candidate_type']:30} "
                    f"{candidate['reason_code']} ({candidate['evidence_basis']})"
                )
            if evaluation.no_action_reason:
                print(f"        no action: {evaluation.no_action_reason}")

        if report.program_commissions:
            print("\n--- commissions (program level only) ---")
            for bucket in report.program_commissions:
                print(
                    f"  {bucket['program_name']}: conversions={bucket['conversions']} "
                    f"amount={bucket['commission_amount']} {bucket['currency'] or ''} "
                    f"attribution={bucket['attribution']} "
                    f"article_level_revenue={bucket['article_level_revenue']}"
                )
        else:
            print("\ncommissions            = none in window")

        for candidate in report.program_candidates + report.data_quality_candidates:
            scope = (
                f"program {candidate['affiliate_program_id']}"
                if candidate["affiliate_program_id"]
                else "measurement"
            )
            print(
                f"\n[{candidate['priority']}] {candidate['candidate_type']} ({scope}) "
                f"{candidate['reason_code']} ({candidate['evidence_basis']})"
            )
            print(f"    {candidate['suggested_action']}")

        for note in report.notes:
            print(f"\nnote: {note}")

        if args.json_path:
            Path(args.json_path).write_text(
                json.dumps(report.as_dict(), ensure_ascii=False, indent=1, default=str),
                encoding="utf-8",
            )
            print(f"\nwrote {args.json_path}")

        if args.execute:
            run = service.persist(report, idempotency_key=args.idempotency_key)
            print(
                f"\npersisted run {run.id}: policy={run.policy_version} "
                f"articles={run.evaluated_article_count} candidates={run.candidate_count} "
                f"clean_clicks={run.clean_clicks}"
            )
        else:
            print("\nreport only: no evaluation history was written. pass --execute to persist.")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
