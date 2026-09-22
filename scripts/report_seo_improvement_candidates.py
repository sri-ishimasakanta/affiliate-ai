"""管理用 CLI: SEO 改善候補の評価 (C6)。

    # 評価して表示するだけ (既定。DB にも外部にも書き込まない)
    uv run python scripts/report_seo_improvement_candidates.py --days 30

    # live / sitemap / URL Inspection も見る (サイトと Google への GET が発生する)
    uv run python scripts/report_seo_improvement_candidates.py --days 30 \
        --with-indexability --inspect

    # 評価結果を append-only な履歴として残す
    uv run python scripts/report_seo_improvement_candidates.py --days 30 --execute

    # 機械可読な出力
    uv run python scripts/report_seo_improvement_candidates.py --json candidates.json

**記事本文は一切変更しない。** ``--execute`` が書くのは
``seo_improvement_runs`` / ``seo_improvement_candidates`` だけで、記事・analytics の
ソース表・WordPress には触れない。

閾値は ``app/config/seo_policy.json`` にあり、運用上のヒューリスティックである
(Google のランキングアルゴリズムに関する主張ではない)。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.database import SessionLocal  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.services.article_indexability_report_service import (  # noqa: E402
    ArticleIndexabilityReportService,
)
from app.services.seo_improvement_candidate_service import (  # noqa: E402
    SeoImprovementCandidateService,
)

EXIT_OK = 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="集計する日数 (既定 30)")
    parser.add_argument(
        "--with-indexability",
        action="store_true",
        help="live / sitemap の状態も評価に使う (サイトへの GET が発生する)",
    )
    parser.add_argument(
        "--inspect", action="store_true", help="--with-indexability と併用し URL Inspection も呼ぶ"
    )
    parser.add_argument("--execute", action="store_true", help="評価結果を履歴として永続化する")
    parser.add_argument("--idempotency-key", help="同じ評価を二重に保存しないためのキー")
    parser.add_argument("--json", dest="json_path", help="結果を JSON で書き出すパス")
    args = parser.parse_args(argv)

    settings = get_settings()
    with SessionLocal() as session:
        indexability = None
        if args.with_indexability:
            indexability = ArticleIndexabilityReportService(session, settings=settings).build(
                inspect=args.inspect
            )
        service = SeoImprovementCandidateService(session, settings=settings)
        report = service.evaluate(days=args.days, indexability=indexability)

        print("=== seo improvement candidates ===")
        print(f"generated_at        = {report.generated_at.isoformat()}")
        print(f"policy_version      = {report.policy_version}")
        print(f"window              = {report.window_start} .. {report.window_end}")
        print(f"gsc data-through    = {report.gsc_data_through}")
        print(
            f"ga4 data-through    = {report.ga4_data_through} (configured={report.ga4_configured})"
        )
        print(f"index observed_at   = {report.index_observed_at}")
        print(f"articles evaluated  = {report.article_count}")

        print("\n--- maturity distribution (search) ---")
        for state, count in sorted(report.maturity_counts.items()):
            print(f"  {state:32} {count}")

        print("\n--- candidates by type ---")
        if report.candidate_counts:
            for candidate_type, count in sorted(report.candidate_counts.items()):
                print(f"  {candidate_type:28} {count}")
        else:
            print("  (none)")
        print(f"  priority: {report.priority_counts}")
        print(
            f"  actionable articles       = {len(report.actionable_article_ids)} "
            f"{report.actionable_article_ids}"
        )
        print(f"  insufficient-data articles = {len(report.insufficient_data_article_ids)}")

        print("\n--- per article ---")
        for evaluation in report.articles:
            print(
                f"  #{evaluation.article_id:<3} {evaluation.maturity_search:28} "
                f"{evaluation.maturity_engagement:30} "
                f"impr={evaluation.gsc['impressions']:<5} candidates={len(evaluation.candidates)}"
            )
            for candidate in evaluation.candidates:
                print(
                    f"        [{candidate['priority']:6}] {candidate['candidate_type']:26} "
                    f"{candidate['reason_code']} ({candidate['evidence_strength']})"
                )
            if evaluation.no_action_reason:
                print(f"        no action: {evaluation.no_action_reason}")

        satisfied = [d for d in report.internal_link_debt if d["status"] == "already_satisfied"]
        pending = [d for d in report.internal_link_debt if d["status"] == "candidate"]
        print(f"\n--- internal link relations ---\n  already satisfied = {len(satisfied)}")
        print(f"  still candidates  = {len(pending)}")

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
                f"articles={run.evaluated_article_count} candidates={run.candidate_count}"
            )
        else:
            print("\nreport only: no evaluation history was written. pass --execute to persist.")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
