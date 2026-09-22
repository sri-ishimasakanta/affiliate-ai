"""管理用 CLI: 公開記事ごとの計測ベースライン (C5.2/C5.3)。

    # 取り込み済みデータだけで集計 (外部通信なし)
    uv run python scripts/report_article_measurement.py --days 30

    # インデックス状態 (live + sitemap) も併記する
    uv run python scripts/report_article_measurement.py --days 30 --with-indexability

    # URL Inspection まで含める (GSC のクォータを消費する)
    uv run python scripts/report_article_measurement.py --with-indexability --inspect

    # 機械可読な出力
    uv run python scripts/report_article_measurement.py --days 30 --json report.json

**read-only** -- DB にも WordPress にも Google にも一切書き込まない。取り込みは
それぞれの import CLI の責務:

- Search Console : ``scripts/import_search_console.py``
- GA4            : ``scripts/import_ga4.py``
- outbound click : ``scripts/import_affiliate_outbound_clicks.py``
- Make commission: ``scripts/import_make_affiliate_commissions.py``

secret は出力しない。
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
from app.services.article_measurement_report_service import (  # noqa: E402
    ArticleMeasurementReportService,
)

EXIT_OK = 0
EXIT_DATA_QUALITY = 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="集計する日数 (既定 30)")
    parser.add_argument(
        "--with-indexability",
        action="store_true",
        help="live / sitemap の状態も併記する (サイトへの GET が発生する)",
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="--with-indexability と併用し URL Inspection も呼ぶ",
    )
    parser.add_argument("--json", dest="json_path", help="結果を JSON で書き出すパス")
    args = parser.parse_args(argv)

    settings = get_settings()
    with SessionLocal() as session:
        indexability = None
        if args.with_indexability:
            indexability = ArticleIndexabilityReportService(session, settings=settings).build(
                inspect=args.inspect
            )
        report = ArticleMeasurementReportService(session, settings=settings).build(
            days=args.days, indexability=indexability
        )

    print("=== article measurement baseline ===")
    print(f"generated_at        = {report.generated_at.isoformat()}")
    print(f"window              = {report.window_start} .. {report.window_end}")
    print(f"published articles  = {report.article_count}")
    print(f"gsc property        = {report.gsc_property}")
    print(f"gsc data-through    = {report.gsc_data_through}")
    print(f"ga4 property        = {report.ga4_property or '(not configured)'}")
    print(f"ga4 data-through    = {report.ga4_data_through}")
    print(f"ga4 property tz     = {report.ga4_property_timezone or '(unknown)'}")
    print(
        f"affiliate clicks    = {report.affiliate_click_total} "
        f"(unattributed {report.affiliate_unattributed_clicks})"
    )

    print(
        f"\n{'id':>3} {'impr':>5} {'clk':>4} {'pos':>6} {'qry':>4} "
        f"{'sess':>5} {'org':>4} {'aff':>4} {'attr':13} states"
    )
    for row in report.articles:
        position = f"{row.average_position:.1f}" if row.average_position is not None else "-"
        print(
            f"{row.article_id:>3} {row.impressions:>5} {row.clicks:>4} {position:>6} "
            f"{row.query_count:>4} {row.sessions:>5} {row.organic_sessions:>4} "
            f"{row.affiliate_clicks:>4} {row.attribution_scope:13} {','.join(row.states)}"
        )

    if report.unattributed_commissions:
        print("\n=== commissions (UNATTRIBUTED_TO_ARTICLE) ===")
        for bucket in report.unattributed_commissions:
            print(
                f"  program={bucket['affiliate_program']} status={bucket['provider_status']} "
                f"conversions={bucket['conversions']} "
                f"amount={bucket['commission_amount']} {bucket['currency'] or ''}"
            )
    else:
        print("\ncommissions         = none in window")

    print("\n=== data quality ===")
    for finding in report.data_quality:
        print(f"  - {finding}")
    if not report.data_quality:
        print("  (no findings)")

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(report.as_dict(), ensure_ascii=False, indent=1, default=str),
            encoding="utf-8",
        )
        print(f"\nwrote {args.json_path}")

    return EXIT_DATA_QUALITY if report.data_quality else EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
