"""Keyword Analysis Workflow CLI (追加実費ゼロ)。

    keywords
      → auto 6 signals (Google Ads bulk 1 fetch + local)
      → competition_ease Difficulty template 出力
      → (ユーザーが値を入力し scripts/import_competition_ease.py で投入)
      → 7/7 揃った keyword を Opportunity Score 化
      → ranking CSV 出力

business logic は ``app/services/keyword_analysis_service.py`` に置き、この CLI は
orchestration の入口に留める。DataForSEO / 有料 SEO API / SERP API / LLM /
embedding / scraper は一切呼ばない。``--dry-run`` は完全な no-side-effect preview
(Google Ads API も呼ばない)。
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.database import SessionLocal  # noqa: E402
from app.services.keyword_analysis_service import (  # noqa: E402
    KeywordAnalysisService,
    normalize_keyword_inputs,
)

EXIT_OK = 0
EXIT_HAD_FAILURES = 1
EXIT_BAD_INPUT = 2

# import_competition_ease.py が読む CSV ヘッダと同一形式。
_TEMPLATE_HEADER = (
    "keyword",
    "keyword_difficulty",
    "source_name",
    "source_reference",
    "observed_at",
)

# ranking CSV の列 (component は scoring.py の並びを尊重しつつ spec の順序に合わせる)。
_RANKING_COMPONENT_ORDER = (
    "search_demand",
    "commercial_intent",
    "affiliate_opportunity",
    "competition_ease",
    "trend",
    "originality",
    "site_relevance",
)


@dataclass
class WorkflowSummary:
    total_keywords: int = 0
    resolved: int = 0
    unresolved: int = 0
    complete: int = 0
    incomplete: int = 0
    auto_signals_created: int = 0
    auto_signals_reused: int = 0
    auto_signals_failed: int = 0
    competition_ease_missing: int = 0
    scores_created: int = 0
    scores_reused: int = 0
    provider_error: str | None = None
    dry_run: bool = False
    messages: list[str] = field(default_factory=list)


def _load_csv_keywords(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "keyword" not in reader.fieldnames:
            raise ValueError("input CSV must have a 'keyword' column")
        return [row.get("keyword", "") or "" for row in reader]


def _write_competition_template(
    path: Path, keywords: list[str], *, source_name: str | None
) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(_TEMPLATE_HEADER)
        for keyword in keywords:
            writer.writerow([keyword, "", source_name or "", "", ""])


def _write_ranking(path: Path, rows) -> None:
    fieldnames = [
        "keyword",
        "keyword_id",
        *_RANKING_COMPONENT_ORDER,
        "opportunity_score",
        "analysis_status",
        "missing_components",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            record: dict[str, object] = {
                "keyword": row.keyword,
                "keyword_id": row.keyword_id,
                "opportunity_score": (
                    "" if row.opportunity_score is None else row.opportunity_score
                ),
                "analysis_status": row.analysis_status,
                "missing_components": "|".join(row.missing_components),
            }
            for component in _RANKING_COMPONENT_ORDER:
                value = row.component_values.get(component)
                record[component] = "" if value is None else value
            writer.writerow(record)


def run_workflow(
    args: argparse.Namespace,
    *,
    session_factory=SessionLocal,
    build_service=None,
) -> WorkflowSummary:
    summary = WorkflowSummary(dry_run=args.dry_run)

    csv_keywords: list[str] = []
    if args.input:
        csv_keywords = _load_csv_keywords(Path(args.input))
    keywords = normalize_keyword_inputs(args.keyword, csv_keywords)
    summary.total_keywords = len(keywords)
    if not keywords:
        raise ValueError("no keywords given (use --keyword and/or --input)")

    with session_factory() as session:
        service = (
            build_service(session)
            if build_service is not None
            else KeywordAnalysisService(session)
        )

        resolved = service.resolve_keywords(
            keywords, create_missing=args.create_missing and not args.dry_run
        )
        summary.resolved = len(resolved.resolved)
        summary.unresolved = len(resolved.unresolved)
        for text in resolved.unresolved:
            summary.messages.append(f"unresolved keyword (not in DB): {text}")
        if args.dry_run and args.create_missing:
            would_create = [
                t
                for t in keywords
                if t not in {k.keyword for k in resolved.resolved}
            ]
            for text in would_create:
                summary.messages.append(f"would create keyword: {text}")

        keyword_ids = [k.id for k in resolved.resolved]

        # --- Phase A: auto signals -----------------------------------
        if args.collect_auto_signals and keyword_ids:
            if args.dry_run:
                for kid in keyword_ids:
                    need = service.components_to_generate(kid, refresh=args.refresh)
                    if need:
                        summary.messages.append(
                            f"would collect auto signals kw={kid}: "
                            f"{', '.join(sorted(need))}"
                        )
            else:
                report = service.collect_auto_signals(
                    keyword_ids, refresh=args.refresh
                )
                summary.auto_signals_created = sum(report.created.values())
                summary.auto_signals_reused = sum(report.reused.values())
                summary.auto_signals_failed = sum(report.failed.values())
                summary.provider_error = report.provider_error
                if report.provider_error:
                    summary.messages.append(
                        f"Google Ads bulk fetch failed: {report.provider_error}"
                    )

        # --- readiness ---------------------------------------------
        readiness = [service.readiness(kid) for kid in keyword_ids]
        summary.complete = sum(1 for r in readiness if r.complete)
        summary.incomplete = sum(1 for r in readiness if not r.complete)

        # --- Phase B: competition template ------------------------
        missing_ce = service.competition_ease_missing(keyword_ids)
        summary.competition_ease_missing = len(missing_ce)
        if args.export_competition_template:
            template_path = Path(args.export_competition_template)
            texts = [k.keyword for k in missing_ce]
            if args.dry_run:
                summary.messages.append(
                    f"would export competition template ({len(texts)} rows) "
                    f"to {template_path}"
                )
            else:
                _write_competition_template(
                    template_path, texts, source_name=args.competition_source
                )
                summary.messages.append(
                    f"wrote competition template ({len(texts)} rows) to {template_path}"
                )

        # --- Phase D: final scoring -------------------------------
        if args.score_ready and keyword_ids:
            if args.dry_run:
                complete_ids = [r.keyword_id for r in readiness if r.complete]
                summary.messages.append(
                    f"would score {len(complete_ids)} complete keyword(s)"
                )
            else:
                outcomes = service.score_ready(keyword_ids, refresh=args.refresh)
                summary.scores_created = sum(
                    1 for o in outcomes if o.status == "scored"
                )
                summary.scores_reused = sum(
                    1 for o in outcomes if o.status == "reused"
                )

        # --- Phase E: ranking export -----------------------------
        if args.output:
            output_path = Path(args.output)
            rows = service.ranking_rows(keyword_ids)
            if args.dry_run:
                summary.messages.append(
                    f"would write ranking ({len(rows)} rows) to {output_path}"
                )
            else:
                _write_ranking(output_path, rows)
                summary.messages.append(
                    f"wrote ranking ({len(rows)} rows) to {output_path}"
                )

    return summary


def _print_summary(summary: WorkflowSummary) -> None:
    for message in summary.messages:
        print(message)
    print()
    if summary.dry_run:
        print("=== DRY RUN (no DB writes, no Google Ads API call) ===")
    print(f"  total_keywords          : {summary.total_keywords}")
    print(f"  resolved / unresolved   : {summary.resolved} / {summary.unresolved}")
    print(f"  complete / incomplete   : {summary.complete} / {summary.incomplete}")
    print(f"  auto_signals_created    : {summary.auto_signals_created}")
    print(f"  auto_signals_reused     : {summary.auto_signals_reused}")
    print(f"  auto_signals_failed     : {summary.auto_signals_failed}")
    print(f"  competition_ease_missing: {summary.competition_ease_missing}")
    print(f"  scores_created          : {summary.scores_created}")
    print(f"  scores_reused           : {summary.scores_reused}")
    if summary.provider_error:
        print(f"  provider_error          : {summary.provider_error}")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_keyword_analysis",
        description=(
            "Keyword Analysis Workflow: auto 6 signals → competition template → "
            "7/7 → Opportunity Score → ranking (追加実費ゼロ)"
        ),
    )
    parser.add_argument("--keyword", action="append", metavar="KEYWORD")
    parser.add_argument("--input", metavar="PATH", help="'keyword' 列を持つ CSV")
    parser.add_argument(
        "--create-missing",
        action="store_true",
        help="DB に無い keyword を KeywordService 経由で作成する (default: 作らない)",
    )
    parser.add_argument(
        "--collect-auto-signals",
        action="store_true",
        help=(
            "6 auto component を生成 (Google Ads bulk 1 fetch + local。"
            "competition_ease は生成しない)"
        ),
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="最新 Signal / Score が存在しても再生成する (default: 再利用)",
    )
    parser.add_argument(
        "--export-competition-template",
        metavar="PATH",
        help="competition_ease が無い keyword の Difficulty 入力 template を CSV 出力",
    )
    parser.add_argument(
        "--competition-source",
        metavar="NAME",
        help="template の source_name 共通値",
    )
    parser.add_argument(
        "--score-ready",
        action="store_true",
        help="7/7 揃った keyword を score_keyword_from_latest_signals で採点",
    )
    parser.add_argument("--output", metavar="PATH", help="ranking CSV の出力先")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="DB write / Signal / Score / 外部 API を一切行わず予定だけ表示",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        summary = run_workflow(args)
    except ValueError as exc:
        print(f"cannot run workflow: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    _print_summary(summary)
    if summary.provider_error or summary.auto_signals_failed or summary.unresolved:
        return EXIT_HAD_FAILURES
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
