"""trend component の正規化ロジック (V1)。

DB / FastAPI / Google Ads SDK に依存しない純粋関数。決定論的。
DB 保存や Provider 呼び出しは行わない。

**Google Ads historical metrics の ``monthly_search_volumes`` だけ** を使う
(Google Trends API / pytrends は V1 では使わない)。

trend は「検索需要が最近伸びているか / 落ちているか」を 0〜100 で表す:

    0   = 強い下降傾向
    50  = 横ばい
    100 = 強い上昇傾向

検索需要の *絶対量* は search_demand が担当する。trend は **方向と勢いだけ** を見る
ため ``[100, 100, 100, ...]`` は検索数の大小に関わらず trend ≈ 50。

V1 formula
----------
単月ノイズを避けるため、年月順にソートした **最新 6 か月** について
「前半 3 か月平均」と「直近 3 か月平均」を比較する:

    previous_3   = mean(最新 6 か月のうち 1〜3 番目)
    recent_3     = mean(最新 6 か月のうち 4〜6 番目)
    change_ratio = (recent_3 - previous_3) / max((recent_3 + previous_3) / 2, 1.0)
                   を [-1.0, +1.0] に clamp
    trend_score  = round(clamp0_100(50 + 50 * change_ratio), 2)

- symmetric percent change なので previous_3 が 0 でもゼロ除算せず、増加と減少を
  対称に扱える (分母下限 1.0 で極端な発散も防ぐ)。
- 直近 1 か月 vs 前月ではノイズが大きいため 3 か月平均同士で比較する。
- 絶対的な検索ボリュームの大小は評価しない (search_demand の担当)。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

NORMALIZER_NAME = "trend"
NORMALIZER_VERSION = "v1"

# V1 は最低 6 か月の有効データを必要とする (3 か月平均 × 2 窓)。
MIN_MONTHS = 6
_WINDOW = 3

_SCORE_MIN = 0.0
_SCORE_MAX = 100.0
_MIDPOINT = 50.0
_SPREAD = 50.0
_RATIO_MIN = -1.0
_RATIO_MAX = 1.0
_MIN_DENOMINATOR = 1.0


class _MonthlyVolume(Protocol):
    """``year`` / ``month`` / ``monthly_searches`` を持つ月次ボリューム (構造的型)。"""

    year: int
    month: int
    monthly_searches: int | None


@dataclass(frozen=True)
class TrendMonth:
    year: int
    month: int
    monthly_searches: int


@dataclass(frozen=True)
class TrendResult:
    """trend V1 の計算結果と根拠 (Signal.raw_data 保存用)。"""

    normalized_value: float
    previous_3_average: float
    recent_3_average: float
    change_ratio: float
    months_used: int
    available_months: int
    window: tuple[TrendMonth, ...]
    normalizer_name: str
    normalizer_version: str


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _reject_negative(values: Sequence[float]) -> None:
    for value in values:
        if value < 0:
            raise ValueError(f"monthly_searches must be >= 0, got {value!r}")


def _core(window: Sequence[float]) -> tuple[float, float, float, float]:
    """最新 6 か月ちょうどの window から (score, previous_3, recent_3, change_ratio)。"""

    previous_3 = math.fsum(window[:_WINDOW]) / _WINDOW
    recent_3 = math.fsum(window[_WINDOW:]) / _WINDOW
    denominator = max((recent_3 + previous_3) / 2.0, _MIN_DENOMINATOR)
    change_ratio = _clamp(
        (recent_3 - previous_3) / denominator, _RATIO_MIN, _RATIO_MAX
    )
    score = _clamp(_MIDPOINT + _SPREAD * change_ratio, _SCORE_MIN, _SCORE_MAX)
    return (
        round(score, 2),
        round(previous_3, 2),
        round(recent_3, 2),
        round(change_ratio, 2),
    )


def prepare_monthly_series(
    monthly_volumes: Sequence[_MonthlyVolume],
) -> list[TrendMonth]:
    """None / 不正月を除外し、負値は ``ValueError``、年月昇順にソートした有効月を返す。"""

    valid: list[TrendMonth] = []
    for volume in monthly_volumes:
        searches = volume.monthly_searches
        if searches is None:
            continue
        if volume.year <= 0 or not (1 <= volume.month <= 12):
            continue
        if searches < 0:
            raise ValueError(f"monthly_searches must be >= 0, got {searches!r}")
        valid.append(
            TrendMonth(
                year=int(volume.year),
                month=int(volume.month),
                monthly_searches=int(searches),
            )
        )
    valid.sort(key=lambda month: (month.year, month.month))
    return valid


def trend_from_monthly_searches(monthly_searches: Sequence[float]) -> TrendResult:
    """年月順に並んだ月次検索数から trend を算出する (計算部分の単体テスト用)。

    最新 ``MIN_MONTHS`` 個を使う。件数不足・負値は ``ValueError``。
    """

    values = [float(value) for value in monthly_searches]
    _reject_negative(values)
    if len(values) < MIN_MONTHS:
        raise ValueError(
            f"trend requires at least {MIN_MONTHS} monthly values, got {len(values)}"
        )

    window = values[-MIN_MONTHS:]
    score, previous_3, recent_3, change_ratio = _core(window)
    return TrendResult(
        normalized_value=score,
        previous_3_average=previous_3,
        recent_3_average=recent_3,
        change_ratio=change_ratio,
        months_used=MIN_MONTHS,
        available_months=len(values),
        window=(),
        normalizer_name=NORMALIZER_NAME,
        normalizer_version=NORMALIZER_VERSION,
    )


def calculate_trend(monthly_volumes: Sequence[_MonthlyVolume]) -> TrendResult:
    """Google Ads ``monthly_search_volumes`` から trend (0〜100) を算出する。

    None の月は除外、負値は ``ValueError``。有効月を年月昇順にソートし、
    最新 ``MIN_MONTHS`` か月で前半 3 / 後半 3 平均を比較する。
    有効月が ``MIN_MONTHS`` 未満なら ``ValueError``。
    """

    valid = prepare_monthly_series(monthly_volumes)
    if len(valid) < MIN_MONTHS:
        raise ValueError(
            f"trend requires at least {MIN_MONTHS} valid months, got {len(valid)}"
        )

    window = valid[-MIN_MONTHS:]
    score, previous_3, recent_3, change_ratio = _core(
        [month.monthly_searches for month in window]
    )
    return TrendResult(
        normalized_value=score,
        previous_3_average=previous_3,
        recent_3_average=recent_3,
        change_ratio=change_ratio,
        months_used=MIN_MONTHS,
        available_months=len(valid),
        window=tuple(window),
        normalizer_name=NORMALIZER_NAME,
        normalizer_version=NORMALIZER_VERSION,
    )
