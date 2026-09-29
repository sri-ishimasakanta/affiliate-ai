"""search_demand component の正規化ロジック (V1)。

DB / FastAPI / Google Ads SDK に依存しない純粋関数。決定論的。
DB 保存や Provider 呼び出しは行わない。

将来数式を差し替えられるよう、呼び出し側は ``NORMALIZER_NAME`` / ``NORMALIZER_VERSION``
を Signal の ``raw_data`` に metadata として保存する。

**V2 (C10-2)**: 数式は V1 のまま。「検索量が 0」と「値が無い」を分ける (``search_volume_evidence``):

- Google Ads は推定の無い項目を proto3 の既定値 0 で返す (commercial_intent v2 と同じ知見)。
  平均 0 **かつ** 月ごとの履歴が無いなら **欠測** (``missing``。0 点にしない)。
- 平均 0 で、月ごとの履歴が ``MIN_ZERO_MONTHS`` か月以上そろい全部 0 なら **本当の 0**
  (``observed_zero`` → 0.0)。履歴が少なければ ``insufficient`` (0 と言い切らない)。
- 平均が無い (None) は欠測。平均 > 0 は観測 (``observed``)。
- 欠測・不十分のときは signal を作らない。保存済みの V1 の行 (平均 0・履歴無しの 0.0) は、
  読む側 (``is_missing_search_demand``) で欠測として扱う (履歴は消さない)。
"""

from __future__ import annotations

import math

NORMALIZER_NAME = "search_demand"
NORMALIZER_VERSION = "v2"

OBSERVED = "observed"
OBSERVED_ZERO = "observed_zero"
MISSING = "missing"
INSUFFICIENT = "insufficient"
#: 本当の 0 と言うのに要る、月ごとの履歴の数 (これより少ない 0 は言い切らない)。
MIN_ZERO_MONTHS = 6

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


def search_volume_evidence(avg_monthly_searches: int | None,
                           monthly_searches: list[int | None] | tuple = ()) -> str:
    """検索量の値が証拠として有るか (observed / observed_zero / missing / insufficient)。"""

    if avg_monthly_searches is None:
        return MISSING
    if avg_monthly_searches > 0:
        return OBSERVED
    months = [m for m in monthly_searches if m is not None]
    if not months:
        return MISSING  # 平均 0 + 履歴無し = Google Ads の既定値 (推定が無い)
    if any(m > 0 for m in months):
        return OBSERVED  # 平均は丸めで 0 だが、月の観測がある
    return OBSERVED_ZERO if len(months) >= MIN_ZERO_MONTHS else INSUFFICIENT


def normalize_search_demand_v2(avg_monthly_searches: int | None,
                               monthly_searches: list[int | None] | tuple = ()) -> float | None:
    """V2: 証拠が有れば V1 の数式で 0〜100、無ければ ``None`` (0 にしない)。"""

    state = search_volume_evidence(avg_monthly_searches, monthly_searches)
    if state in (MISSING, INSUFFICIENT):
        return None
    return normalize_search_demand(int(avg_monthly_searches or 0))


def monthly_from_raw(raw: dict) -> list[int | None]:
    return [m.get("monthly_searches") for m in raw.get("monthly_search_volumes") or []
            if isinstance(m, dict)]


def is_missing_search_demand(raw: dict | None) -> bool:
    """保存済みの search_demand の行が、実は欠測か (V1 の平均 0・履歴無しの 0.0 など)。"""

    if not isinstance(raw, dict) or "avg_monthly_searches" not in raw:
        return False
    return search_volume_evidence(raw.get("avg_monthly_searches"),
                                  monthly_from_raw(raw)) in (MISSING, INSUFFICIENT)
