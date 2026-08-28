"""CommercialIntentNormalizer V1 の unit テスト (DB / SDK / FastAPI 非依存)。"""

from itertools import pairwise

import pytest

from app.keyword.normalizers.commercial_intent import (
    CPC_CALIBRATION_JPY,
    CURRENCY_ASSUMPTION,
    NORMALIZER_NAME,
    NORMALIZER_VERSION,
    QueryIntent,
    calculate_commercial_intent,
    classify_query_intent,
    normalize_ad_competition_score,
    normalize_cpc_score,
    score_commercial_intent,
)


def _yen(amount: int) -> int:
    """JPY 金額を micros へ (low_top_of_page_bid_micros 相当)。"""

    return amount * 1_000_000


# -- Query Intent Score --------------------------------------------------
@pytest.mark.parametrize(
    ("keyword", "expected_type", "expected_score"),
    [
        ("ChatGPT 料金", "price", 95.0),
        ("SaaS 価格 一覧", "price", 95.0),
        ("導入 費用 目安", "price", 95.0),
        ("AI 議事録 比較", "compare", 90.0),
        ("AI ツール おすすめ", "recommend", 90.0),
        ("AI 文字起こし ランキング", "recommend", 90.0),
        ("生成AI 法人 導入", "b2b", 85.0),
        ("法人向け チャットボット", "b2b", 85.0),
        ("業務効率化 ツール", "tool", 65.0),
        ("AI 議事録 無料", "free", 45.0),
        ("議事録 自動作成 サービス", "generic", 40.0),
        ("ChatGPT 使い方", "how_to", 20.0),
        ("生成AI とは", "informational", 10.0),
    ],
)
def test_classify_query_intent_single_rule(
    keyword: str, expected_type: str, expected_score: float
) -> None:
    assert classify_query_intent(keyword) == QueryIntent(
        type=expected_type, score=expected_score
    )


@pytest.mark.parametrize(
    ("keyword", "expected_type", "expected_score"),
    [
        ("AI 無料 比較", "compare", 90.0),          # 無料(45) + 比較(90) -> 90
        ("法人向け AI ツール", "b2b", 85.0),         # 法人向け(85) + ツール(65) -> 85
        ("ChatGPT 料金 とは", "price", 95.0),        # 料金(95) + とは(10) -> 95
        ("AI おすすめ 使い方", "recommend", 90.0),   # おすすめ(90) + 使い方(20) -> 90
    ],
)
def test_classify_query_intent_multi_match_takes_highest(
    keyword: str, expected_type: str, expected_score: float
) -> None:
    intent = classify_query_intent(keyword)
    assert intent.type == expected_type
    assert intent.score == expected_score


def test_classify_query_intent_normalizes_spacing_and_case() -> None:
    assert classify_query_intent("  ChatGPT　料金  ").type == "price"  # 全角スペース
    assert classify_query_intent("CHATGPT 使い方").type == "how_to"


def test_classify_query_intent_is_google_ads_independent() -> None:
    # Google Ads を一切触らずに動く純粋関数
    assert classify_query_intent("生成AI とは") == QueryIntent("informational", 10.0)


# -- Low CPC Score -----------------------------------------------------
@pytest.mark.parametrize(
    ("micros", "expected"),
    [
        (_yen(0), 0.0),
        (_yen(100), 32.97),
        (_yen(250), 63.21),
        (_yen(400), 79.81),
        (_yen(700), 93.92),
    ],
)
def test_normalize_cpc_score_known_values(micros: int, expected: float) -> None:
    assert normalize_cpc_score(micros) == expected


def test_normalize_cpc_score_missing_returns_none_not_zero() -> None:
    assert normalize_cpc_score(None) is None


def test_normalize_cpc_score_calibration_constant() -> None:
    assert CPC_CALIBRATION_JPY == 250.0


def test_normalize_cpc_score_monotonic_and_bounded() -> None:
    values = [normalize_cpc_score(_yen(a)) for a in (0, 50, 100, 250, 500, 1000, 5000)]
    assert values == sorted(values)
    assert all(0.0 <= v <= 100.0 for v in values)
    assert all(a <= b for a, b in pairwise(values))


def test_normalize_cpc_score_negative_raises() -> None:
    with pytest.raises(ValueError, match=">= 0"):
        normalize_cpc_score(-1)


# -- Ad Competition Score -------------------------------------------
@pytest.mark.parametrize("index", [0, 50, 100])
def test_normalize_ad_competition_score_passthrough(index: int) -> None:
    assert normalize_ad_competition_score(index) == float(index)


def test_normalize_ad_competition_score_missing_returns_none_not_zero() -> None:
    assert normalize_ad_competition_score(None) is None


@pytest.mark.parametrize("index", [-1, -50, 101, 200])
def test_normalize_ad_competition_score_out_of_range_raises(index: int) -> None:
    with pytest.raises(ValueError, match=r"\[0, 100\]"):
        normalize_ad_competition_score(index)


# -- weight redistribution ----------------------------------------
def test_score_all_three_available_coverage_1() -> None:
    result = score_commercial_intent(
        query_intent=QueryIntent("compare", 90.0),
        cpc_score=63.21,
        ad_competition_score=88.0,
    )
    assert result.available_weight == 1.0
    assert result.evidence_coverage == 1.0
    assert result.market_evidence_available is True
    # (90*0.6 + 63.21*0.3 + 88*0.1) / 1.0
    assert result.score == pytest.approx(81.76, abs=0.005)


def test_score_query_and_cpc_only_coverage_0_90() -> None:
    result = score_commercial_intent(
        query_intent=QueryIntent("compare", 90.0),
        cpc_score=70.0,
        ad_competition_score=None,
    )
    assert result.available_weight == 0.9
    assert result.evidence_coverage == 0.9
    assert result.market_evidence_available is True
    # (90*0.6 + 70*0.3) / 0.9
    assert result.score == pytest.approx(83.33, abs=0.005)


def test_score_query_and_competition_only_coverage_0_70() -> None:
    result = score_commercial_intent(
        query_intent=QueryIntent("recommend", 90.0),
        cpc_score=None,
        ad_competition_score=50.0,
    )
    assert result.available_weight == 0.7
    assert result.evidence_coverage == 0.7
    assert result.market_evidence_available is True
    # (90*0.6 + 50*0.1) / 0.7
    assert result.score == pytest.approx(84.29, abs=0.005)


def test_score_query_only_coverage_0_60_no_market_evidence() -> None:
    result = score_commercial_intent(
        query_intent=QueryIntent("recommend", 90.0),
        cpc_score=None,
        ad_competition_score=None,
    )
    assert result.available_weight == 0.6
    assert result.evidence_coverage == 0.6
    assert result.market_evidence_available is False
    assert result.score == 90.0  # (90*0.6) / 0.6
    assert result.cpc_score is None
    assert result.ad_competition_score is None


def test_missing_data_is_not_penalised_as_zero() -> None:
    query_only = score_commercial_intent(
        query_intent=QueryIntent("price", 95.0),
        cpc_score=None,
        ad_competition_score=None,
    )
    real_zero_market = score_commercial_intent(
        query_intent=QueryIntent("price", 95.0),
        cpc_score=0.0,
        ad_competition_score=0.0,
    )
    assert query_only.score == 95.0  # 欠測なら下がらない
    assert real_zero_market.score == 57.0  # 本当に 0 のときだけ下がる (95*0.6/1.0)


def test_result_carries_v1_weights_and_metadata() -> None:
    result = score_commercial_intent(
        query_intent=QueryIntent("tool", 65.0),
        cpc_score=10.0,
        ad_competition_score=20.0,
    )
    assert result.query_intent_weight == 0.60
    assert result.cpc_weight == 0.30
    assert result.ad_competition_weight == 0.10
    assert result.normalizer_name == NORMALIZER_NAME == "commercial_intent"
    assert result.normalizer_version == NORMALIZER_VERSION == "v1"
    assert result.currency_assumption == CURRENCY_ASSUMPTION == "JPY"


# -- calculate_commercial_intent (keyword + metrics まとめて) -------
def test_calculate_from_keyword_and_full_metrics() -> None:
    result = calculate_commercial_intent(
        keyword="AI 議事録 比較",
        low_top_of_page_bid_micros=_yen(250),
        competition_index=88,
    )
    assert result.query_intent_type == "compare"
    assert result.query_intent_score == 90.0
    assert result.cpc_score == 63.21
    assert result.ad_competition_score == 88.0
    assert result.available_weight == 1.0
    assert result.score == pytest.approx(81.76, abs=0.005)


def test_calculate_with_missing_market_data_uses_query_only() -> None:
    result = calculate_commercial_intent(
        keyword="生成AI とは",
        low_top_of_page_bid_micros=None,
        competition_index=None,
    )
    assert result.query_intent_type == "informational"
    assert result.score == 10.0
    assert result.evidence_coverage == 0.6
    assert result.market_evidence_available is False


def test_calculate_is_deterministic() -> None:
    kwargs = {
        "keyword": "ChatGPT 料金",
        "low_top_of_page_bid_micros": _yen(300),
        "competition_index": 40,
    }
    assert calculate_commercial_intent(**kwargs) == calculate_commercial_intent(**kwargs)


def test_calculate_result_is_rounded_to_two_decimals() -> None:
    for micros in (_yen(37), _yen(123), _yen(456)):
        score = calculate_commercial_intent(
            keyword="AI ツール おすすめ",
            low_top_of_page_bid_micros=micros,
            competition_index=33,
        ).score
        assert round(score, 2) == score
