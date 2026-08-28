"""Opportunity Score V1 の純粋計算ロジック。

DB / FastAPI / Pydantic に依存しない。決定論的 (同じ入力 → 同じ結果)。

7 項目それぞれを 0〜100 の入力値として受け取り、重み付き合計で 0〜100 の
Opportunity Score を算出する。``competition_ease`` は「100 に近いほど競合が弱く
攻略しやすい」という向きに統一している。

V1 の重み (``OPPORTUNITY_SCORE_WEIGHTS``) がスコア仕様の唯一の集約点。
strategy pattern や scoring framework は導入しない。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

SCORE_VERSION = "v1"

SCORE_MIN = 0.0
SCORE_MAX = 100.0

# V1 配点 (合計 1.0)。ここがスコア仕様の単一の情報源。
OPPORTUNITY_SCORE_WEIGHTS: Mapping[str, float] = MappingProxyType(
    {
        "search_demand": 0.20,
        "commercial_intent": 0.20,
        "affiliate_opportunity": 0.20,
        "competition_ease": 0.15,
        "trend": 0.10,
        "originality": 0.10,
        "site_relevance": 0.05,
    }
)

# 入力・出力で共通に使うコンポーネント名の並び。
COMPONENT_NAMES: tuple[str, ...] = tuple(OPPORTUNITY_SCORE_WEIGHTS)


@dataclass(frozen=True)
class OpportunityScoreInput:
    """スコア計算の入力 (各項目 0〜100)。"""

    search_demand: float
    commercial_intent: float
    affiliate_opportunity: float
    competition_ease: float
    trend: float
    originality: float
    site_relevance: float

    def as_dict(self) -> dict[str, float]:
        return {name: float(getattr(self, name)) for name in COMPONENT_NAMES}


@dataclass(frozen=True)
class OpportunityScoreResult:
    """スコア計算の結果と根拠。

    - ``total``: Opportunity Score (0〜100、小数第 2 位)
    - ``version``: スコアバージョン (V1 では ``"v1"``)
    - ``contributions``: コンポーネントごとの寄与値 (value * weight、小数第 2 位)
    """

    total: float
    version: str
    contributions: Mapping[str, float]


def _ensure_in_range(name: str, value: float) -> None:
    if not SCORE_MIN <= value <= SCORE_MAX:
        raise ValueError(f"{name} must be within [0, 100], got {value!r}")


def calculate_opportunity_score(data: OpportunityScoreInput) -> OpportunityScoreResult:
    """7 項目の重み付き合計から Opportunity Score を算出する。"""

    components = data.as_dict()

    total = 0.0
    contributions: dict[str, float] = {}
    for name, weight in OPPORTUNITY_SCORE_WEIGHTS.items():
        value = components[name]
        _ensure_in_range(name, value)
        contribution = value * weight
        total += contribution
        contributions[name] = round(contribution, 2)

    return OpportunityScoreResult(
        total=round(total, 2),
        version=SCORE_VERSION,
        contributions=MappingProxyType(contributions),
    )
