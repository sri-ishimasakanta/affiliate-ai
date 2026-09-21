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

from app.keyword.equivalence import contains_japanese
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


@cache
def _spacing_tolerant_pattern(normalized_term: str) -> re.Pattern[str]:
    """日本語の文字に隣接する位置の空白の有無を無視する term pattern。

    :func:`~app.keyword.equivalence.equivalence_key` と同じ意味 (日本語 phrase の空白位置の差は
    同一) を、空白を保った keyword に対する照合として表す。keyword を空白なしに詰めると
    ``ai 議事 録`` が ``ai議事録`` になり、隣の ASCII が term に接して英数字境界で外れるため。
    英数字同士の間の空白 (``生成AI SEO`` の ``ai`` と ``seo``) は元の term どおりに要求する。
    境界 (前後が ``[a-z0-9]`` でない) は :func:`term_matches` と同じ。
    """

    parts: list[str] = []
    prev: str | None = None
    saw_space = False
    for ch in normalized_term:
        if ch.isspace():
            saw_space = prev is not None
            continue
        if prev is not None:
            if contains_japanese(prev) or contains_japanese(ch):
                parts.append(" ?")
            elif saw_space:
                parts.append(" ")
        parts.append(re.escape(ch))
        prev, saw_space = ch, False
    return re.compile(rf"(?<![a-z0-9]){''.join(parts)}(?![a-z0-9])")


def _japanese_term_matches_spacing_tolerant(normalized_term: str, normalized_keyword: str) -> bool:
    """日本語を含む term だけを、日本語隣接の空白差を無視して照合する (英語だけの term は除く)。"""

    if not normalized_term or not contains_japanese(normalized_term):
        return False
    return _spacing_tolerant_pattern(normalized_term).search(normalized_keyword) is not None


def term_hit(
    normalized_term: str,
    normalized_keyword: str,
    *,
    ignore_japanese_spacing: bool = False,
) -> bool:
    """正規化済み term が正規化済み keyword に match するか (match の唯一の定義)。

    :func:`matched_terms_in_keyword` と tier 判定 (``affiliate_tiers``) が同じ照合を共有する。
    """

    return term_matches(normalized_term, normalized_keyword) or (
        ignore_japanese_spacing
        and _japanese_term_matches_spacing_tolerant(normalized_term, normalized_keyword)
    )


def matched_terms_in_keyword(
    normalized_keyword: str,
    terms: Iterable[str],
    *,
    ignore_japanese_spacing: bool = False,
) -> tuple[str, ...]:
    """keyword 内で実際に match した term (元の表記) を入力順で返す。

    ``ignore_japanese_spacing`` なら、日本語を含む term は日本語隣接の空白位置の差 (``議事 録`` と
    ``議事録``) を無視した照合も行う。英語だけの term は常に従来どおり (空白を保った照合のみ)。
    """

    hits: list[str] = []
    for term in terms:
        if not term:
            continue
        normalized_term = normalize_for_match(term)
        if term_hit(
            normalized_term, normalized_keyword, ignore_japanese_spacing=ignore_japanese_spacing
        ):
            hits.append(term)
    return tuple(hits)


def match_programs(
    keyword: str,
    programs: Sequence[ProgramFacts],
    *,
    ignore_japanese_spacing: bool = False,
) -> list[MatchedProgram]:
    """keyword に対して 1 term 以上 match した program を返す (呼び出し側で active 限定)。

    ``ignore_japanese_spacing=True`` は、Google Ads の分かち書き (``議事 録``) を吸収する opt-in。
    日本語を含む term だけが対象で、従来の照合結果に追加で match するだけ (外れることはない)。
    既定は False で、production の scoring / queue の呼び出し元の挙動は変わらない。
    """

    normalized_keyword = normalize_for_match(keyword)
    matched: list[MatchedProgram] = []
    for program in programs:
        hits = matched_terms_in_keyword(
            normalized_keyword,
            program.match_terms,
            ignore_japanese_spacing=ignore_japanese_spacing,
        )
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
