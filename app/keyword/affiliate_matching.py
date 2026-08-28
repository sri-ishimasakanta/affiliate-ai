"""keyword と ``AffiliateProgram.match_terms`` の照合ロジック (pure)。

分析 CLI (``scripts/analyze_affiliate_opportunities.py``) と production の
affiliate_opportunity normalizer / service が **同一の matching semantics** を
使うための共有 helper。DB / FastAPI / SQLAlchemy 非依存。

- 正規化は ``site_relevance`` の公開関数 ``normalize_keyword`` を再利用する
  (Unicode NFKC → casefold → 連続空白の単一化)。site_relevance 側には変更を加えない。
- ASCII 英数字語は英数字境界で照合し ``maker`` の中の ``make`` 等を誤検知しない。
- 日本語を含む語は前後が英数字でない位置での一致 = 実質 substring 一致。
- URL / credential / ASP account 情報は ``ProgramFacts`` / ``MatchedProgram`` に持たない。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import cache

from app.keyword.normalizers.site_relevance import normalize_keyword


def normalize_for_match(text: str) -> str:
    """keyword / term を照合用に正規化する (NFKC → casefold → 空白正規化)。"""

    return normalize_keyword(text)


@cache
def _term_pattern(normalized_term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![a-z0-9]){re.escape(normalized_term)}(?![a-z0-9])")


def term_matches(normalized_term: str, normalized_keyword: str) -> bool:
    """正規化済み term が正規化済み keyword 内に (境界を尊重して) 出現するか。"""

    if not normalized_term:
        return False
    return _term_pattern(normalized_term).search(normalized_keyword) is not None


@dataclass(frozen=True)
class ProgramFacts:
    """照合・provenance に必要な安全なフィールドのみ (URL は含まない)。"""

    program_id: int
    name: str
    provider: str | None
    category: str | None
    commission_type: str | None
    commission_value: float | None
    currency: str | None
    match_terms: tuple[str, ...]


@dataclass(frozen=True)
class MatchedProgram:
    program_id: int
    name: str
    provider: str | None
    category: str | None
    matched_terms: tuple[str, ...]
    commission_type: str | None
    commission_value: float | None
    currency: str | None


def matched_terms_in_keyword(
    normalized_keyword: str, terms: Iterable[str]
) -> tuple[str, ...]:
    """keyword 内で実際に match した term (元の表記) を入力順で返す。"""

    return tuple(
        term
        for term in terms
        if term and term_matches(normalize_for_match(term), normalized_keyword)
    )


def match_programs(
    keyword: str, programs: Sequence[ProgramFacts]
) -> list[MatchedProgram]:
    """keyword に対して 1 term 以上 match した program を返す (呼び出し側で active 限定)。"""

    normalized_keyword = normalize_for_match(keyword)
    matched: list[MatchedProgram] = []
    for program in programs:
        hits = matched_terms_in_keyword(normalized_keyword, program.match_terms)
        if hits:
            matched.append(
                MatchedProgram(
                    program_id=program.program_id,
                    name=program.name,
                    provider=program.provider,
                    category=program.category,
                    matched_terms=hits,
                    commission_type=program.commission_type,
                    commission_value=program.commission_value,
                    currency=program.currency,
                )
            )
    return matched
