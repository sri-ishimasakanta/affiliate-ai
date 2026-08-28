"""AffiliateProgram カタログと代表キーワードの match 分析 CLI (採点はしない)。

現在 DB に投入済みの **active** AffiliateProgram の ``match_terms`` と keyword を
照合し、affiliate_opportunity V1 formula を設計するための「採点前の生データ」を
表形式 / CSV で出力する。

    uv run python scripts/analyze_affiliate_opportunities.py \
        --keyword "AI 議事録 おすすめ" --keyword "ChatGPT 料金"
    uv run python scripts/analyze_affiliate_opportunities.py \
        --input keywords.csv --output affiliate_opportunity_analysis.csv --show-programs

DB は read-only。KeywordSignal を作らない / DB を変更しない / commit しない。
**tracking_url / landing_page_url は出力に一切含めない。**
affiliate_opportunity normalizer / Signal は実装しない (今回は分析のみ)。
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# matching semantics は production normalizer / service と共有 (乖離防止)。
from app.config.database import SessionLocal  # noqa: E402
from app.keyword.affiliate_matching import (  # noqa: E402
    MatchedProgram,
    ProgramFacts,
    match_programs,
)
from app.models.enums import AffiliateProgramStatus  # noqa: E402
from app.repositories.affiliate_program_repository import (  # noqa: E402
    AffiliateProgramRepository,
)

EXIT_OK = 0
EXIT_BAD_INPUT = 2

_FIXED = "fixed"
_PERCENTAGE = "percentage"
_ACTIVE_LIMIT = 10_000
_UNKNOWN_CURRENCY = "UNKNOWN"


@dataclass
class KeywordAnalysis:
    keyword: str
    matched: list[MatchedProgram] = field(default_factory=list)

    @property
    def matched_program_count(self) -> int:
        return len(self.matched)

    @property
    def matched_program_ids(self) -> list[int]:
        return [m.program_id for m in self.matched]

    @property
    def matched_program_names(self) -> list[str]:
        return [m.name for m in self.matched]

    @property
    def matched_terms(self) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for program in self.matched:
            for term in program.matched_terms:
                if term not in seen:
                    seen.add(term)
                    out.append(term)
        return sorted(out)

    @property
    def active_providers(self) -> list[str]:
        return sorted({m.provider for m in self.matched if m.provider})

    @property
    def distinct_provider_count(self) -> int:
        # provider が同一 (例: "direct") の複数案件は 1 とカウントする。
        return len({m.provider for m in self.matched})

    @property
    def _with_commission(self) -> list[MatchedProgram]:
        return [
            m
            for m in self.matched
            if m.commission_type and m.commission_value is not None
        ]

    @property
    def commission_data_count(self) -> int:
        return len(self._with_commission)

    @property
    def fixed(self) -> list[MatchedProgram]:
        return [
            m
            for m in self.matched
            if (m.commission_type or "").lower() == _FIXED
            and m.commission_value is not None
        ]

    @property
    def percentage(self) -> list[MatchedProgram]:
        return [
            m
            for m in self.matched
            if (m.commission_type or "").lower() == _PERCENTAGE
            and m.commission_value is not None
        ]

    @property
    def fixed_commission_count(self) -> int:
        return len(self.fixed)

    @property
    def percentage_commission_count(self) -> int:
        return len(self.percentage)

    @property
    def best_fixed_by_currency(self) -> dict[str, float]:
        # currency ごとに最大値を保持。currency 横断の統合・FX 換算はしない。
        out: dict[str, float] = {}
        for m in self.fixed:
            currency = m.currency or _UNKNOWN_CURRENCY
            value = float(m.commission_value or 0.0)
            if currency not in out or value > out[currency]:
                out[currency] = value
        return out

    @property
    def best_fixed_commission(self) -> tuple[float, str] | None:
        """最も高い fixed 額とその currency。**currency 横断では比較不能** な点に注意。"""

        best: tuple[float, str] | None = None
        for currency, value in self.best_fixed_by_currency.items():
            if best is None or value > best[0]:
                best = (value, currency)
        return best

    @property
    def best_percentage_commission_value(self) -> float | None:
        values = [float(m.commission_value or 0.0) for m in self.percentage]
        return max(values) if values else None


# --- core ---------------------------------------------------------------
def analyze_keyword(
    keyword: str, programs: Sequence[ProgramFacts]
) -> KeywordAnalysis:
    # 照合ルールは app.keyword.affiliate_matching に集約 (production と同一)。
    return KeywordAnalysis(
        keyword=keyword, matched=match_programs(keyword, list(programs))
    )


def load_active_program_facts(session) -> list[ProgramFacts]:
    rows = AffiliateProgramRepository(session).list(
        status=AffiliateProgramStatus.ACTIVE, limit=_ACTIVE_LIMIT
    )
    return [
        ProgramFacts(
            program_id=row.id,
            name=row.name,
            provider=row.provider,
            category=row.category,
            commission_type=row.commission_type,
            commission_value=row.commission_value,
            currency=row.currency,
            match_terms=tuple(row.match_terms or ()),
        )
        for row in rows
    ]


def load_keywords(
    cli_keywords: Iterable[str] | None, input_path: Path | None
) -> list[str]:
    raw: list[str] = list(cli_keywords or [])
    if input_path is not None:
        with input_path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None or "keyword" not in reader.fieldnames:
                raise ValueError("input CSV must have a 'keyword' column")
            raw.extend(row.get("keyword", "") or "" for row in reader)

    seen: set[str] = set()
    cleaned: list[str] = []
    for item in raw:
        keyword = (item or "").strip()
        if not keyword or keyword in seen:
            continue
        seen.add(keyword)
        cleaned.append(keyword)
    return cleaned


# --- rendering ----------------------------------------------------------
def _display_width(text: str) -> int:
    return sum(
        2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text
    )


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def render_table(analyses: Sequence[KeywordAnalysis]) -> str:
    headers = ["keyword", "programs", "providers", "commission data"]
    matrix: list[list[str]] = [headers]
    for analysis in analyses:
        matrix.append(
            [
                analysis.keyword,
                str(analysis.matched_program_count),
                str(analysis.distinct_provider_count),
                str(analysis.commission_data_count),
            ]
        )
    widths = [
        max(_display_width(matrix[r][c]) for r in range(len(matrix)))
        for c in range(len(headers))
    ]
    lines: list[str] = []
    for index, record in enumerate(matrix):
        lines.append(
            "  ".join(_pad(record[c], widths[c]) for c in range(len(headers))).rstrip()
        )
        if index == 0:
            lines.append("  ".join("-" * widths[c] for c in range(len(headers))))
    return "\n".join(lines)


def _format_commission(program: MatchedProgram) -> str:
    if not program.commission_type or program.commission_value is None:
        return "-"
    currency = f" {program.currency}" if program.currency else ""
    return f"{program.commission_type} {program.commission_value}{currency}"


def _print_program_details(analyses: Sequence[KeywordAnalysis]) -> None:
    print("\n=== matched programs (URL は表示しない) ===")
    for analysis in analyses:
        print(
            f"\n● {analysis.keyword}  "
            f"({analysis.matched_program_count} programs, "
            f"{analysis.distinct_provider_count} providers)"
        )
        for program in analysis.matched:
            print(
                f"    [id {program.program_id}] {program.name} | "
                f"provider={program.provider} | category={program.category} | "
                f"commission={_format_commission(program)} | "
                f"terms={' , '.join(program.matched_terms)}"
            )


def _bucket_counts(values: Sequence[int]) -> tuple[int, int, int, int]:
    return (
        sum(1 for v in values if v == 0),
        sum(1 for v in values if v == 1),
        sum(1 for v in values if v == 2),
        sum(1 for v in values if v >= 3),
    )


def _print_summary(analyses: Sequence[KeywordAnalysis]) -> None:
    total = len(analyses)
    counts = [a.matched_program_count for a in analyses]
    provider_counts = [a.distinct_provider_count for a in analyses]
    with_matches = sum(1 for c in counts if c > 0)
    commission_available = sum(1 for a in analyses if a.commission_data_count >= 1)

    print("\n=== coverage ===")
    print(f"  total_keywords          : {total}")
    print(f"  keywords_with_matches   : {with_matches}")
    print(f"  keywords_without_matches: {total - with_matches}")
    rate = (with_matches / total) if total else 0.0
    print(f"  match_coverage_rate     : {rate:.2%}")

    def _dist(label: str, values: Sequence[int]) -> None:
        b0, b1, b2, b3 = _bucket_counts(values)
        print(f"\n=== {label} distribution ===")
        print(f"  min / max     : {min(values)} / {max(values)}")
        print(f"  mean / median : {statistics.mean(values):.2f} / {statistics.median(values)}")
        print(f"  0 / 1 / 2 / 3+: {b0} / {b1} / {b2} / {b3}")

    _dist("matched_program_count", counts)
    _dist("distinct_provider_count", provider_counts)

    print("\n=== commission ===")
    print(f"  commission_data_available (>=1 program): {commission_available}")


# --- CSV --------------------------------------------------------------
def _fixed_currencies(analyses: Sequence[KeywordAnalysis]) -> list[str]:
    return sorted({cur for a in analyses for cur in a.best_fixed_by_currency})


def csv_fieldnames(analyses: Sequence[KeywordAnalysis]) -> list[str]:
    fields = [
        "keyword",
        "matched_program_count",
        "distinct_provider_count",
        "commission_data_count",
        "fixed_commission_count",
        "percentage_commission_count",
        "best_percentage_commission_value",
        "best_fixed_by_currency",
    ]
    fields += [f"best_fixed_{currency}" for currency in _fixed_currencies(analyses)]
    fields += [
        "matched_program_ids",
        "matched_program_names",
        "active_providers",
        "matched_terms",
    ]
    return fields


def _write_csv(path: Path, analyses: Sequence[KeywordAnalysis]) -> None:
    fieldnames = csv_fieldnames(analyses)
    currencies = _fixed_currencies(analyses)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for analysis in analyses:
            by_currency = analysis.best_fixed_by_currency
            best_pct = analysis.best_percentage_commission_value
            row: dict[str, object] = {
                "keyword": analysis.keyword,
                "matched_program_count": analysis.matched_program_count,
                "distinct_provider_count": analysis.distinct_provider_count,
                "commission_data_count": analysis.commission_data_count,
                "fixed_commission_count": analysis.fixed_commission_count,
                "percentage_commission_count": analysis.percentage_commission_count,
                "best_percentage_commission_value": "" if best_pct is None else best_pct,
                "best_fixed_by_currency": json.dumps(
                    by_currency, ensure_ascii=False, sort_keys=True
                ),
                "matched_program_ids": "|".join(
                    str(i) for i in analysis.matched_program_ids
                ),
                "matched_program_names": " | ".join(analysis.matched_program_names),
                "active_providers": " | ".join(analysis.active_providers),
                "matched_terms": " | ".join(analysis.matched_terms),
            }
            for currency in currencies:
                row[f"best_fixed_{currency}"] = by_currency.get(currency, "")
            writer.writerow(row)


# --- orchestration --------------------------------------------------
def run_analysis(
    keywords: Sequence[str],
    *,
    session_factory=SessionLocal,
    output: str | Path | None = None,
    show_programs: bool = False,
) -> int:
    with session_factory() as session:
        programs = load_active_program_facts(session)

    analyses = [analyze_keyword(keyword, programs) for keyword in keywords]

    print(f"active affiliate programs in catalog: {len(programs)}")
    print(f"keywords analyzed                   : {len(analyses)}\n")
    print(render_table(analyses))

    if show_programs:
        _print_program_details(analyses)

    _print_summary(analyses)

    if output is not None:
        path = Path(output)
        _write_csv(path, analyses)
        print(f"\nwrote {len(analyses)} row(s) to {path}")

    return EXIT_OK


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="analyze_affiliate_opportunities",
        description=(
            "keyword と active AffiliateProgram.match_terms を照合し、"
            "affiliate_opportunity 採点前の生データを出力する (DB read-only)"
        ),
    )
    parser.add_argument(
        "--keyword",
        dest="keywords",
        action="append",
        metavar="KEYWORD",
        help="分析するキーワード。複数回指定可",
    )
    parser.add_argument(
        "--input", metavar="PATH", help="'keyword' 列を持つ CSV から一括読込"
    )
    parser.add_argument(
        "--output", metavar="PATH", help="結果を CSV へ書き出す"
    )
    parser.add_argument(
        "--show-programs",
        action="store_true",
        help="keyword ごとの matched program 詳細も表示する (URL は出さない)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    input_path = Path(args.input) if args.input else None
    if input_path is not None and not input_path.is_file():
        print(f"input file not found: {input_path}", file=sys.stderr)
        return EXIT_BAD_INPUT

    try:
        keywords = load_keywords(args.keywords, input_path)
    except ValueError as exc:
        print(f"cannot read keywords: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    if not keywords:
        print(
            "no keywords given (use --keyword and/or --input)", file=sys.stderr
        )
        return EXIT_BAD_INPUT

    return run_analysis(keywords, output=args.output, show_programs=args.show_programs)


if __name__ == "__main__":
    raise SystemExit(main())
