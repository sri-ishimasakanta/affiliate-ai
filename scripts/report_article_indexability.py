"""管理用 CLI: 公開記事のインデックス可能性ベースライン (C5.1)。

    # live + robots + sitemap + GSC 実績のみ (URL Inspection を呼ばない)
    uv run python scripts/report_article_indexability.py --no-inspect

    # URL Inspection も含めた完全な棚卸し (プロパティあたりのクォータを消費する)
    uv run python scripts/report_article_indexability.py

    # 機械可読な出力
    uv run python scripts/report_article_indexability.py --json report.json

**read-only** -- DB にも WordPress にも Google にも一切書き込まない。Search
Console のデータ取り込みは ``scripts/import_search_console.py`` (C1.1) の責務で、
このスクリプトは既に取り込まれた行を読むだけ。

secret (credential path / private key / access token) は出力しない。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.database import SessionLocal  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.seo.indexability import (  # noqa: E402
    ACTION_MANUAL_INDEX_REQUEST,
    ACTION_TECHNICAL_FIX,
    LIVE_HEALTHY,
    SITEMAP_PRESENT,
)
from app.services.article_indexability_report_service import (  # noqa: E402
    ArticleIndexabilityReportService,
)

EXIT_OK = 0
EXIT_DEFECTS = 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-inspect",
        action="store_true",
        help="URL Inspection API を呼ばない (live/sitemap/実績のみ)",
    )
    parser.add_argument("--json", dest="json_path", help="結果を JSON で書き出すパス")
    args = parser.parse_args(argv)

    settings = get_settings()
    with SessionLocal() as session:
        report = ArticleIndexabilityReportService(session, settings=settings).build(
            inspect=not args.no_inspect
        )

    print("=== article indexability baseline ===")
    print(f"generated_at           = {report.generated_at.isoformat()}")
    print(f"site                   = {report.site_base_url}")
    print(f"property               = {report.property_uri}")
    print(f"published articles     = {report.article_count}")
    print(f"robots.txt             = HTTP {report.robots_txt_status}")
    print(
        f"sitemap entry point    = {report.sitemap_entry_point} "
        f"({report.sitemap_entry_point_source})"
    )
    for document in report.sitemap_documents:
        print(
            f"  - {document['url']} HTTP {document['status']} "
            f"index={document['is_index']} locs={document['loc_count']}"
        )
    print(f"sitemap url count      = {report.sitemap_url_count}")
    print(
        f"url inspection         = "
        f"{'available' if report.inspection_available else 'unavailable'}"
        f"{'' if report.inspection_available else f' ({report.inspection_limitation})'}"
    )
    print(f"performance data through = {report.performance_data_through}")

    print(
        f"\n{'id':>3} {'live':22} {'sitemap':16} {'index state':28} "
        f"{'impr':>5} {'clicks':>6} {'action':28} url"
    )
    for row in report.articles:
        print(
            f"{row.article_id:>3} {row.live_state:22} {row.sitemap_state:16} "
            f"{row.google_index_state:28} {row.impressions:>5} {row.clicks:>6} "
            f"{row.action:28} {row.url}"
        )

    healthy = sum(
        1
        for r in report.articles
        if r.live_state == LIVE_HEALTHY and r.sitemap_state == SITEMAP_PRESENT
    )
    fixes = [r for r in report.articles if r.action == ACTION_TECHNICAL_FIX]
    candidates = [r for r in report.articles if r.action == ACTION_MANUAL_INDEX_REQUEST]
    print(f"\nhealthy + in sitemap   = {healthy}/{report.article_count}")
    print(f"technical fix required = {len(fixes)}")
    print(f"manual index request   = {len(candidates)}")
    for warning in report.warnings:
        print(f"warning: {warning}")

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(report.as_dict(), ensure_ascii=False, indent=1, default=str),
            encoding="utf-8",
        )
        print(f"\nwrote {args.json_path}")

    return EXIT_DEFECTS if fixes else EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
