"""affiliate_opportunity component の正規化ロジック (V1)。

**供給側** の評価: 現在の active Affiliate Catalog に、この keyword へ直接 match する
収益化案件がどれだけ存在し、どの程度の収益余地があるか。**検索者の購買意図
(commercial_intent) とは別物。** DB / FastAPI / SQLAlchemy 非依存・決定論的。

V1 formula
----------
    program_match_score    weight 0.55  (選択肢の広さ。案件数は限界効用逓減で 100 点化)
    commission_score       weight 0.35  (percentage commission のみ。fixed は score 不使用)
    provider_spread_score  weight 0.10  (弱い補助指標。"direct" が複数案件をまとめるため)

- matched program == 0: ``affiliate_opportunity = 0.0`` / ``market_evidence_available = false``。
  0 match は「市場に案件が無い」ではなく **「現在の active catalog に直接 match する
  案件が無い」**。catalog completeness は保証しない。
- missing commission は **0 点にせず**、利用できた weight だけで再正規化する。
- fixed commission は JPY / USD を公平に比較する calibration がまだ無いため V1 score に
  使わない (**FX 換算はしない**。raw_data provenance には残す)。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from app.keyword.affiliate_matching import MatchedProgram

NORMALIZER_NAME = "affiliate_opportunity"
NORMALIZER_VERSION = "v1"

PROGRAM_MATCH_WEIGHT = 0.55
COMMISSION_WEIGHT = 0.35
PROVIDER_SPREAD_WEIGHT = 0.10

# matched program 数 n -> 0..100: 100 * (1 - exp(-n / 4.0))。
# 4.0 は V1 calibration constant。案件が増えるほど限界効用が小さくなる。
PROGRAM_MATCH_CALIBRATION = 4.0
# percentage(%) -> 0..100: min(100, pct * 2.5)。40% で満点。
PERCENTAGE_COMMISSION_MULTIPLIER = 2.5
# distinct provider 数 p -> 0..100: min(100, p * 40)。3 provider で満点。
PROVIDER_SPREAD_PER_PROVIDER = 40.0

_PERCENTAGE = "percentage"
_FIXED = "fixed"
_SCORE_MIN = 0.0
_SCORE_MAX = 100.0


@dataclass(frozen=True)
class AffiliateOpportunityResult:
    """affiliate_opportunity V1 の計算結果と根拠。"""

    normalized_value: float
    program_match_score: float
    commission_score: float | None
    provider_spread_score: float
    matched_program_count: int
    distinct_provider_count: int
    available_weight: float
    evidence_coverage: float
    market_evidence_available: bool
    normalizer_name: str
    normalizer_version: str


def _clamp(value: float) -> float:
    return min(_SCORE_MAX, max(_SCORE_MIN, value))


def _commission_kind(program: MatchedProgram) -> str:
    return (program.commission_type or "").strip().casefold()


def valid_percentage_values(matched: Sequence[MatchedProgram]) -> list[float]:
    """score に使える percentage commission 値 (0 以上) を返す。"""

    return [
        float(program.commission_value)
        for program in matched
        if _commission_kind(program) == _PERCENTAGE
        and program.commission_value is not None
        and program.commission_value >= 0
    ]


def valid_fixed_programs(matched: Sequence[MatchedProgram]) -> list[MatchedProgram]:
    """fixed commission を持つ program (provenance 用。score には使わない)。"""

    return [
        program
        for program in matched
        if _commission_kind(program) == _FIXED
        and program.commission_value is not None
        and program.commission_value >= 0
    ]


def distinct_provider_count(matched: Sequence[MatchedProgram]) -> int:
    return len(
        {
            program.provider.strip()
            for program in matched
            if program.provider and program.provider.strip()
        }
    )


def program_match_score(matched_count: int) -> float:
    if matched_count <= 0:
        return 0.0
    raw = _SCORE_MAX * (
        1.0 - math.exp(-matched_count / PROGRAM_MATCH_CALIBRATION)
    )
    return round(_clamp(raw), 2)


def commission_score(matched: Sequence[MatchedProgram]) -> float | None:
    values = valid_percentage_values(matched)
    if not values:
        return None
    raw = min(_SCORE_MAX, max(values) * PERCENTAGE_COMMISSION_MULTIPLIER)
    return round(_clamp(raw), 2)


def provider_spread_score(matched: Sequence[MatchedProgram]) -> float:
    if not matched:
        return 0.0
    return round(
        _clamp(min(_SCORE_MAX, distinct_provider_count(matched) * PROVIDER_SPREAD_PER_PROVIDER)),
        2,
    )


def calculate_affiliate_opportunity(
    matched: Sequence[MatchedProgram],
) -> AffiliateOpportunityResult:
    """matched active program のリストから affiliate_opportunity (0〜100) を算出する。"""

    count = len(matched)
    pm_score = program_match_score(count)
    ps_score = provider_spread_score(matched)
    cm_score = commission_score(matched)
    provider_count = distinct_provider_count(matched)

    if count == 0:
        coverage = round(PROGRAM_MATCH_WEIGHT, 2)
        return AffiliateOpportunityResult(
            normalized_value=0.0,
            program_match_score=0.0,
            commission_score=None,
            provider_spread_score=0.0,
            matched_program_count=0,
            distinct_provider_count=0,
            available_weight=coverage,
            evidence_coverage=coverage,
            market_evidence_available=False,
            normalizer_name=NORMALIZER_NAME,
            normalizer_version=NORMALIZER_VERSION,
        )

    present: list[tuple[float, float]] = [
        (pm_score, PROGRAM_MATCH_WEIGHT),
        (ps_score, PROVIDER_SPREAD_WEIGHT),
    ]
    if cm_score is not None:
        present.append((cm_score, COMMISSION_WEIGHT))

    available_weight = math.fsum(weight for _, weight in present)
    weighted_sum = math.fsum(score * weight for score, weight in present)
    normalized = round(_clamp(weighted_sum / available_weight), 2)
    coverage = round(available_weight, 2)

    return AffiliateOpportunityResult(
        normalized_value=normalized,
        program_match_score=pm_score,
        commission_score=cm_score,
        provider_spread_score=ps_score,
        matched_program_count=count,
        distinct_provider_count=provider_count,
        available_weight=coverage,
        evidence_coverage=coverage,
        market_evidence_available=True,
        normalizer_name=NORMALIZER_NAME,
        normalizer_version=NORMALIZER_VERSION,
    )
