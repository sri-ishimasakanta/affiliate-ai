"""competition_ease component の正規化ロジック (V1)。

**Google Organic SEO の攻略しやすさ** を 0〜100 で表す。

- HIGH (100 に近い) = organic SEO で比較的競合が弱い
- LOW  (0 に近い)   = organic SEO の競合が強い

入力は **Organic SEO Keyword Difficulty (0 = easy 〜 100 = hard)** のみ。
別スケール (0-10 / low-medium-high / Google Ads competition index 等) は受け付けない。

    competition_ease = round(clamp(100 - keyword_difficulty, 0, 100), 2)

**Google Ads の ``competition`` / ``competition_index`` は絶対に使わない**
(広告オークションの競争度であり Organic SEO の Keyword Difficulty ではない)。

DB / SQLAlchemy / FastAPI 非依存・決定論的。外部 API 通信なし。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

NORMALIZER_NAME = "competition_ease"
NORMALIZER_VERSION = "v1"

# 受け付ける Difficulty の尺度 (provenance に必ず保存する)。
DIFFICULTY_SCALE = "0_easy_100_hard"

_MIN = 0.0
_MAX = 100.0


@dataclass(frozen=True)
class CompetitionEaseResult:
    normalized_value: float
    keyword_difficulty: float
    difficulty_scale: str
    evidence_coverage: float
    evidence_available: bool
    normalizer_name: str
    normalizer_version: str


def _validate_difficulty(value: object) -> float:
    """Keyword Difficulty を検証して float へ。

    required / numeric / finite / 0〜100。bool・NaN・Infinity・範囲外は ``ValueError``。
    """

    # bool は int のサブクラスなので numeric として誤受付しない。
    if isinstance(value, bool):
        raise ValueError("keyword_difficulty must be a number, not a boolean")
    if not isinstance(value, (int, float)):
        raise ValueError("keyword_difficulty must be a number")

    number = float(value)
    if not math.isfinite(number):
        raise ValueError("keyword_difficulty must be a finite number")
    if number < _MIN or number > _MAX:
        raise ValueError(
            f"keyword_difficulty must be within [0, 100], got {number!r}"
        )
    return number


def _clamp(value: float) -> float:
    return min(_MAX, max(_MIN, value))


def calculate_competition_ease(keyword_difficulty: object) -> CompetitionEaseResult:
    """Organic SEO Keyword Difficulty (0 easy 〜 100 hard) から competition_ease を算出する。"""

    difficulty = _validate_difficulty(keyword_difficulty)
    ease = round(_clamp(_MAX - difficulty), 2)

    return CompetitionEaseResult(
        normalized_value=ease,
        keyword_difficulty=difficulty,
        difficulty_scale=DIFFICULTY_SCALE,
        evidence_coverage=1.0,
        evidence_available=True,
        normalizer_name=NORMALIZER_NAME,
        normalizer_version=NORMALIZER_VERSION,
    )
