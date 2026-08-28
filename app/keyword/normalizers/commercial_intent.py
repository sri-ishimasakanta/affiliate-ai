"""commercial_intent component の正規化ロジック (V1)。

DB / FastAPI / Google Ads SDK に依存しない純粋関数。決定論的。
DB 保存や Provider 呼び出しは行わない。

V1 の構成は「実際に取得した 30 キーワードの Google Ads データを比較して決めた」もの:

    commercial_intent =
        query_intent_score * 0.60
      + cpc_score          * 0.30
      + ad_competition     * 0.10

- Query Intent Score : keyword 文字列から純粋に判定する (Google Ads 非依存)。
- Low CPC Score      : Google Ads ``low_top_of_page_bid_micros`` を通貨単位へ直し
  ``100 * (1 - exp(-low_bid / 250))``。**250 は JPY calibration 定数**。
  ``high_top_of_page_bid_micros`` は V1 では score に使わない (実データ確認時に
  外れ値が大きく score が不安定になったため。raw_data には保存する)。
- Ad Competition Score: Google Ads ``competition_index`` (0〜100) をそのまま採用。
  **これは広告オークションの競争度であり、SEO organic の competition_ease とは
  別物。competition_ease へ流用しないこと。**

CPC / competition_index が取得できない keyword では、その要素を欠測 (missing)
として扱い、**0 点で減点せず**、利用できた weight だけで再正規化する。
query intent は keyword から常に得られるため available weight は通常 0.60 以上。

前提: 日本市場・JPY の Google Ads アカウント (V1 calibration)。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

NORMALIZER_NAME = "commercial_intent"
NORMALIZER_VERSION = "v1"

# V1 は日本市場・JPY アカウント前提。CPC を円換算して calibration する。
CURRENCY_ASSUMPTION = "JPY"

# redistribution 前の V1 重み (合計 1.0)。
QUERY_INTENT_WEIGHT = 0.60
CPC_WEIGHT = 0.30
AD_COMPETITION_WEIGHT = 0.10

# Low CPC Score の calibration 定数 (JPY)。low_bid(円) がこの値のとき cpc_score ≈ 63。
# 実データ 30 件を比較して決めた V1 値。散らばった magic number にしない。
CPC_CALIBRATION_JPY = 250.0

_SCORE_MIN = 0.0
_SCORE_MAX = 100.0
_MICROS_PER_CURRENCY_UNIT = 1_000_000

# 判定ルール: (該当語, intent type, score)。複数該当時は「最も高い score」を採用し、
# 同点は先に並ぶルールを優先する。どれにも該当しなければ generic (score 40)。
_QUERY_INTENT_RULES: tuple[tuple[tuple[str, ...], str, float], ...] = (
    (("料金", "価格", "費用"), "price", 95.0),
    (("比較",), "compare", 90.0),
    (("おすすめ", "ランキング"), "recommend", 90.0),
    (("導入", "法人向け"), "b2b", 85.0),
    (("ツール",), "tool", 65.0),
    (("無料",), "free", 45.0),
    (("使い方",), "how_to", 20.0),
    (("とは",), "informational", 10.0),
)

_GENERIC_INTENT_TYPE = "generic"
_GENERIC_INTENT_SCORE = 40.0


def _clamp(value: float) -> float:
    return min(_SCORE_MAX, max(_SCORE_MIN, value))


@dataclass(frozen=True)
class QueryIntent:
    """keyword 文字列から判定した検索意図。"""

    type: str
    score: float


@dataclass(frozen=True)
class CommercialIntentResult:
    """commercial_intent V1 の計算結果と根拠 (Signal.raw_data 保存用)。"""

    score: float
    query_intent_type: str
    query_intent_score: float
    cpc_score: float | None
    ad_competition_score: float | None
    query_intent_weight: float
    cpc_weight: float
    ad_competition_weight: float
    available_weight: float
    evidence_coverage: float
    market_evidence_available: bool
    normalizer_name: str
    normalizer_version: str
    currency_assumption: str


def _normalize_keyword(keyword: str) -> str:
    """前後空白除去 + 全角スペース→半角 + 連続空白圧縮 + casefold (最低限の正規化)。"""

    return " ".join(keyword.replace("　", " ").split()).casefold()


def classify_query_intent(keyword: str) -> QueryIntent:
    """keyword 文字列から Query Intent Score を判定する (Google Ads 非依存の純粋関数)。

    複数ルールに該当する場合は最も高い score の intent を採用する。
    どのルールにも該当しなければ ``generic`` (score 40)。
    """

    text = _normalize_keyword(keyword)

    best: QueryIntent | None = None
    for needles, intent_type, score in _QUERY_INTENT_RULES:
        if any(needle in text for needle in needles) and (
            best is None or score > best.score
        ):
            best = QueryIntent(type=intent_type, score=score)

    if best is not None:
        return best
    return QueryIntent(type=_GENERIC_INTENT_TYPE, score=_GENERIC_INTENT_SCORE)


def normalize_cpc_score(low_top_of_page_bid_micros: int | None) -> float | None:
    """Low CPC Score (0〜100) を算出する。

    ``low_bid(JPY) = low_top_of_page_bid_micros / 1_000_000``
    ``cpc_score = 100 * (1 - exp(-low_bid / CPC_CALIBRATION_JPY))``  (JPY calibration)

    概ね: 0 円→0 / 100 円→約 33 / 250 円→約 63 / 400 円→約 80 / 700 円→約 94。

    ``low_top_of_page_bid_micros`` が ``None`` の場合は欠測として ``None`` を返す
    (**0 点にはしない**)。``high_top_of_page_bid_micros`` は V1 では使わない。
    """

    if low_top_of_page_bid_micros is None:
        return None
    if low_top_of_page_bid_micros < 0:
        raise ValueError(
            "low_top_of_page_bid_micros must be >= 0, "
            f"got {low_top_of_page_bid_micros!r}"
        )

    low_bid = low_top_of_page_bid_micros / _MICROS_PER_CURRENCY_UNIT
    score = _SCORE_MAX * (1.0 - math.exp(-low_bid / CPC_CALIBRATION_JPY))
    return round(_clamp(score), 2)


def normalize_ad_competition_score(competition_index: int | None) -> float | None:
    """Ad Competition Score を算出する。

    Google Ads ``competition_index`` は既に 0〜100 なのでそのまま採用する
    (妥当性のみ検証)。``None`` は欠測として ``None`` (**0 点にはしない**)。

    **``competition_index`` は広告オークションの競争度であり、Opportunity Score の
    ``competition_ease`` (organic SEO の攻略しやすさ) とは別物。competition_ease へ
    流用しないこと。** ``competition`` enum (LOW/MEDIUM/HIGH) からの推測補完も
    V1 では行わない。
    """

    if competition_index is None:
        return None

    value = float(competition_index)
    if not _SCORE_MIN <= value <= _SCORE_MAX:
        raise ValueError(
            f"competition_index must be within [0, 100], got {competition_index!r}"
        )
    return round(value, 2)


def score_commercial_intent(
    *,
    query_intent: QueryIntent,
    cpc_score: float | None,
    ad_competition_score: float | None,
) -> CommercialIntentResult:
    """正規化済みの 3 サブスコアを V1 重みで合成する。

    ``cpc_score`` / ``ad_competition_score`` が ``None`` (欠測) の要素は
    **0 点扱いにせず**、利用できた weight だけで再正規化する
    (``score = Σ(value * weight) / Σ(利用できた weight)``)。
    """

    parts: list[tuple[float, float]] = [(query_intent.score, QUERY_INTENT_WEIGHT)]
    if cpc_score is not None:
        parts.append((cpc_score, CPC_WEIGHT))
    if ad_competition_score is not None:
        parts.append((ad_competition_score, AD_COMPETITION_WEIGHT))

    available_weight = math.fsum(weight for _, weight in parts)
    if available_weight <= 0.0:
        # query intent が常に 0.60 を占めるため通常到達しない (防御的検証)。
        raise ValueError("commercial_intent: available weight must be > 0")

    weighted = math.fsum(value * weight for value, weight in parts)
    score = round(_clamp(weighted / available_weight), 2)
    coverage = round(available_weight, 2)

    return CommercialIntentResult(
        score=score,
        query_intent_type=query_intent.type,
        query_intent_score=query_intent.score,
        cpc_score=cpc_score,
        ad_competition_score=ad_competition_score,
        query_intent_weight=QUERY_INTENT_WEIGHT,
        cpc_weight=CPC_WEIGHT,
        ad_competition_weight=AD_COMPETITION_WEIGHT,
        available_weight=coverage,
        evidence_coverage=coverage,
        market_evidence_available=(
            cpc_score is not None or ad_competition_score is not None
        ),
        normalizer_name=NORMALIZER_NAME,
        normalizer_version=NORMALIZER_VERSION,
        currency_assumption=CURRENCY_ASSUMPTION,
    )


def calculate_commercial_intent(
    *,
    keyword: str,
    low_top_of_page_bid_micros: int | None,
    competition_index: int | None,
) -> CommercialIntentResult:
    """keyword 文字列 + Google Ads 指標から commercial_intent (0〜100) を算出する。

    query intent は keyword から常に得られる。CPC / competition_index が欠測でも
    query intent だけで score を出す (weight 再正規化)。
    """

    return score_commercial_intent(
        query_intent=classify_query_intent(keyword),
        cpc_score=normalize_cpc_score(low_top_of_page_bid_micros),
        ad_competition_score=normalize_ad_competition_score(competition_index),
    )
