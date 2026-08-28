"""search_demand component の正規化ロジック (V1)。

DB / FastAPI / Google Ads SDK に依存しない純粋関数。決定論的。
DB 保存や Provider 呼び出しは行わない。

将来数式を差し替えられるよう、呼び出し側は ``NORMALIZER_NAME`` / ``NORMALIZER_VERSION``
を Signal の ``raw_data`` に metadata として保存する。
"""

from __future__ import annotations

import math

NORMALIZER_NAME = "search_demand"
NORMALIZER_VERSION = "v1"

_MAX_SCORE = 100.0
_COEFFICIENT = 20.0


def normalize_search_demand(avg_monthly_searches: int) -> float:
    """平均月間検索数を 0〜100 の search_demand スコアへ変換する (V1)。

    ``score = min(100.0, 20.0 * log10(avg_monthly_searches + 1))`` を小数第 2 位で丸める。

    例: 0 -> 0.00 / 10 -> 20.83 / 100 -> 40.09 / 1000 -> 60.01 /
    10000 -> 80.00 / 100000 以上 -> 100.00
    """

    if avg_monthly_searches < 0:
        raise ValueError(
            f"avg_monthly_searches must be >= 0, got {avg_monthly_searches!r}"
        )

    score = min(_MAX_SCORE, _COEFFICIENT * math.log10(avg_monthly_searches + 1))
    return round(score, 2)
