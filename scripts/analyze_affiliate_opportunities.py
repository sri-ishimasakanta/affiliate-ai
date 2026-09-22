"""AffiliateProgram カタログと代表キーワードの match 分析 CLI (Signal は作らない)。

現在 DB に投入済みの **active** AffiliateProgram の ``match_terms`` と keyword を
照合し、C2.5.7 の affiliate fit policy で分類して表形式 / CSV で出力する:

- brand tier: strong (自身の名前 / 明示 alias) | weak
- fit: core | loose | unreviewed (weak の term ごと。``app/config/affiliate_match_fit.json``)
- scoring_eligible / primary_eligible = strong、または weak かつ core
- keyword の分類: strong / core_weak (brand 無しで core の weak あり) / context_only
  (weak の loose / unreviewed だけ: score 0・primary 不可) / none

照合は scoring / plan / queue と同じ ``match_catalog`` (日本語の分かち書きも同じ扱い)。
``affiliate_opportunity`` 列は production と同じ式で eligible な program だけから計算した値。
legacy の「どれか 1 term でも match」(``matched_*`` 列) は CSV の後方互換のために残すが、
収益化の意味ではない (fit を見ない)。fit config に載っていない active program / term は
fail-closed で ``unreviewed`` になり、警告を出す (停止はしない)。

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
from app.keyword.affiliate_fit import FIT_CORE, FIT_LOOSE, FIT_UNREVIEWED  # noqa: E402
from app.keyword.affiliate_matching import (  # noqa: E402
    MatchedProgram,
    ProgramFacts,
    match_programs,
)
from app.keyword.affiliate_tiers import (  # noqa: E402
    TIER_STRONG,
    TIER_WEAK,
    TieredMatch,
    fit_config_gaps,
    match_catalog,
    scoring_programs,
    summarize_tiers,
    unreviewed_brand_tokens,
)
from app.keyword.normalizers.affiliate_opportunity import (  # noqa: E402
    calculate_affiliate_opportunity,
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

COVERAGE_STRONG = "strong"
COVERAGE_CORE_WEAK = "core_weak"
COVERAGE_CONTEXT_ONLY = "context_only"
COVERAGE_NONE = "none"


@dataclass
class KeywordAnalysis:
    keyword: str
    # legacy の「どれか 1 term でも match」(後方互換の列のためだけ。収益化の意味ではない)
    matched: list[MatchedProgram] = field(default_factory=list)
    # C2.5.7: scoring / plan / queue と同じ照合 (``match_catalog``) の tier + fit 付き match
    tiered: list[TieredMatch] = field(default_factory=list)

    # ---- C2.5.7: affiliate fit policy (strong、または weak かつ core が適格)
    @property
    def eligible(self) -> list[TieredMatch]:
        return [t for t in self.tiered if t.scoring_eligible]

    @property
    def scoring_eligible_program_names(self) -> list[str]:
        return [t.name for t in self.eligible]

    @property
    def primary_eligible_program_names(self) -> list[str]:
        return [t.name for t in self.tiered if t.primary_eligible]

    def _weak_names(self, fit: str) -> list[str]:
        return sorted(t.name for t in self.tiered if t.tier == TIER_WEAK and t.fit == fit)

    @property
    def core_weak_program_names(self) -> list[str]:
        return self._weak_names(FIT_CORE)

    @property
    def loose_weak_program_names(self) -> list[str]:
        return self._weak_names(FIT_LOOSE)

    @property
    def unreviewed_weak_program_names(self) -> list[str]:
        return sorted(
            t.name
            for t in self.tiered
            if t.tier == TIER_WEAK and t.fit not in (FIT_CORE, FIT_LOOSE)
        )

    @property
    def coverage_class(self) -> str:
        if any(t.tier == TIER_STRONG for t in self.tiered):
            return COVERAGE_STRONG
        if self.eligible:
            return COVERAGE_CORE_WEAK
        if self.tiered:
            return COVERAGE_CONTEXT_ONLY
        return COVERAGE_NONE

    @property
    def affiliate_opportunity(self) -> float:
        """production と同じ式・重みで eligible な program だけから計算した値。"""

        return calculate_affiliate_opportunity(scoring_programs(self.tiered)).normalized_value

    @property
    def matched_program_count(self) -> int:
        return len(self.matched)

    @property
    def strong_program_count(self) -> int:
        return summarize_tiers(self.tiered).strong_count

    @property
    def weak_program_count(self) -> int:
        return summarize_tiers(self.tiered).weak_count

    @property
    def strong_program_names(self) -> list[str]:
        return list(summarize_tiers(self.tiered).strong_names)

    @property
    def weak_program_names(self) -> list[str]:
        return list(summarize_tiers(self.tiered).weak_names)

    @property
    def no_strong_affiliate_match(self) -> bool:
        return summarize_tiers(self.tiered).no_strong_affiliate_match

    @property
    def alias_only_strong_program_names(self) -> list[str]:
        """明示 alias だけで strong になった program (legacy の matched には入らない)。"""

        return [t.name for t in self.tiered if not t.legacy_matched]

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
    # tiered は production (scoring / plan / queue) と同じ照合 + tier + fit (C2.5.7)。
    # matched は legacy の any-term match (CSV の後方互換列のためだけ)。
    return KeywordAnalysis(
        keyword=keyword,
        matched=match_programs(keyword, list(programs)),
        tiered=match_catalog(keyword, list(programs)),
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
    headers = [
        "keyword",
        "coverage",
        "aff_opp",
        "eligible",
        "strong",
        "core",
        "loose",
        "unreviewed",
        "legacy_any",
    ]
    matrix: list[list[str]] = [headers]
    for analysis in analyses:
        matrix.append(
            [
                analysis.keyword,
                analysis.coverage_class,
                f"{analysis.affiliate_opportunity:.2f}",
                str(len(analysis.eligible)),
                str(analysis.strong_program_count),
                str(len(analysis.core_weak_program_names)),
                str(len(analysis.loose_weak_program_names)),
                str(len(analysis.unreviewed_weak_program_names)),
                str(analysis.matched_program_count),
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


def _terms_text(tier: TieredMatch) -> str:
    parts = [f"strong={','.join(tier.strong_terms) or '-'}"]
    for fit in (FIT_CORE, FIT_LOOSE, FIT_UNREVIEWED):
        parts.append(f"{fit}={','.join(tier.terms_with_fit(fit)) or '-'}")
    return " ".join(parts)


def _print_program_details(analyses: Sequence[KeywordAnalysis]) -> None:
    print("\n=== matched programs: brand tier x fit (URL は表示しない) ===")
    for analysis in analyses:
        print(
            f"\n● {analysis.keyword}  (coverage={analysis.coverage_class}, "
            f"affiliate_opportunity={analysis.affiliate_opportunity:.2f}, "
            f"eligible={len(analysis.eligible)}/{len(analysis.tiered)})"
        )
        for tier in analysis.tiered:
            program = tier.program
            alias_note = (
                "" if tier.legacy_matched else " [alias-only strong, not in the legacy match]"
            )
            print(
                f"    [id {program.program_id}] {program.name}{alias_note} | "
                f"provider={program.provider} | category={program.category} | "
                f"commission={_format_commission(program)} | "
                f"tier={tier.tier} fit={tier.fit or '-'} | "
                f"scoring_eligible={tier.scoring_eligible} "
                f"primary_eligible={tier.primary_eligible} | "
                f"terms {_terms_text(tier)} | {tier.reason}"
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

    by_class = {
        c: sum(1 for a in analyses if a.coverage_class == c)
        for c in (COVERAGE_STRONG, COVERAGE_CORE_WEAK, COVERAGE_CONTEXT_ONLY, COVERAGE_NONE)
    }
    print("\n=== affiliate monetization (eligible = strong OR weak+core) ===")
    print(f"  total_keywords                 : {total}")
    monetizable = by_class[COVERAGE_STRONG] + by_class[COVERAGE_CORE_WEAK]
    print(f"  keywords_monetizable           : {monetizable}")
    print(f"  strong                         : {by_class[COVERAGE_STRONG]}")
    print(f"  core_weak (no brand, core fit) : {by_class[COVERAGE_CORE_WEAK]}")
    print(f"  context_only (loose/unreviewed): {by_class[COVERAGE_CONTEXT_ONLY]}")
    print(f"  no_match                       : {by_class[COVERAGE_NONE]}")
    print(
        f"  keywords_with_nonzero_affiliate_opportunity: "
        f"{sum(1 for a in analyses if a.affiliate_opportunity > 0)}"
    )
    alias_only = sum(1 for a in analyses if a.alias_only_strong_program_names)
    print(f"  keywords_with_alias_only_strong (not in the legacy match): {alias_only}")

    print("\n=== legacy any-term match (backward compatibility; NOT monetization) ===")
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
        # C2.5.4 (追加・末尾): 既存の列と順序は変えない
        "strong_program_count",
        "weak_program_count",
        "strong_program_names",
        "weak_program_names",
        "no_strong_affiliate_match",
        "alias_only_strong_program_names",
        # C2.5.7 (追加・末尾): 収益化の意味はこちら (strong OR weak+core)
        *FIT_CSV_COLUMNS,
    ]
    return fields


# C2.5.7: affiliate fit policy の列。matched_* (legacy any-term) より優先して読む
FIT_CSV_COLUMNS = (
    "coverage_class",
    "affiliate_opportunity",
    "scoring_eligible_program_count",
    "scoring_eligible_program_names",
    "primary_eligible_program_names",
    "core_weak_program_names",
    "loose_weak_program_names",
    "unreviewed_weak_program_names",
    "match_details",
)


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
                "strong_program_count": analysis.strong_program_count,
                "weak_program_count": analysis.weak_program_count,
                "strong_program_names": " | ".join(analysis.strong_program_names),
                "weak_program_names": " | ".join(analysis.weak_program_names),
                "no_strong_affiliate_match": analysis.no_strong_affiliate_match,
                "alias_only_strong_program_names": " | ".join(
                    analysis.alias_only_strong_program_names
                ),
                "coverage_class": analysis.coverage_class,
                "affiliate_opportunity": analysis.affiliate_opportunity,
                "scoring_eligible_program_count": len(analysis.eligible),
                "scoring_eligible_program_names": " | ".join(
                    analysis.scoring_eligible_program_names
                ),
                "primary_eligible_program_names": " | ".join(
                    analysis.primary_eligible_program_names
                ),
                "core_weak_program_names": " | ".join(analysis.core_weak_program_names),
                "loose_weak_program_names": " | ".join(analysis.loose_weak_program_names),
                "unreviewed_weak_program_names": " | ".join(
                    analysis.unreviewed_weak_program_names
                ),
                "match_details": json.dumps(
                    [
                        {
                            "program_id": t.program_id,
                            "name": t.name,
                            "brand_tier": t.tier,
                            "fit": t.fit,
                            "scoring_eligible": t.scoring_eligible,
                            "primary_eligible": t.primary_eligible,
                            "strong_terms": list(t.strong_terms),
                            "core_terms": list(t.terms_with_fit(FIT_CORE)),
                            "loose_terms": list(t.terms_with_fit(FIT_LOOSE)),
                            "unreviewed_terms": list(t.terms_with_fit(FIT_UNREVIEWED)),
                        }
                        for t in analysis.tiered
                    ],
                    ensure_ascii=False,
                ),
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
    unreviewed = unreviewed_brand_tokens(programs)
    if unreviewed:
        print(
            "tier config: own-name tokens not yet reviewed (add to affiliate_match_tiers.json): "
            + ", ".join(f"{name}={token}" for name, token in unreviewed)
        )
    for warning in fit_gap_warnings(programs):
        print(warning)
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


def fit_gap_warnings(programs: Sequence[ProgramFacts]) -> list[str]:
    """fit config が網羅していない active program / term の警告 (fail-closed の説明つき)。"""

    out: list[str] = []
    for gap in fit_config_gaps(programs):
        terms = ", ".join(gap.unclassified_terms) or "-"
        if gap.program_missing:
            out.append(
                f"WARNING fit config: active program {gap.program!r} is not in "
                "affiliate_match_fit.json; its generic terms resolve to 'unreviewed' "
                f"(score 0, never primary) until reviewed: {terms}"
            )
        else:
            out.append(
                f"WARNING fit config: {gap.program!r} has generic terms not in "
                "affiliate_match_fit.json; they resolve to 'unreviewed' "
                f"(score 0, never primary) until reviewed: {terms}"
            )
    return out


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="analyze_affiliate_opportunities",
        description=(
            "keyword と active AffiliateProgram.match_terms を照合し、"
            "brand tier x fit と affiliate_opportunity を出力する (DB read-only)"
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
