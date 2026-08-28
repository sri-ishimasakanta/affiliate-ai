"""Google Ads metrics 照合 (`_match_metrics` + 正規化ヘルパー) の unit テスト。

Phase 2C-1.1: Google Ads Historical Metrics の bulk 応答は CJK keyword を分かち書き
し直して返す ("AI 議事録 おすすめ" → "ai 議事 録 おすすめ")。表記上の空白差だけを
安全に吸収し、別 keyword への誤割当はしないことを固定する。fuzzy match は使わない。
"""

import pytest

from app.keyword.providers.google_ads import GoogleAdsKeywordMetrics
from app.services.keyword_metrics_collection_service import (
    _match_metrics,
    compact_keyword_match_key,
    normalize_keyword_match_text,
)


def _m(keyword: str) -> GoogleAdsKeywordMetrics:
    return GoogleAdsKeywordMetrics(
        keyword=keyword,
        avg_monthly_searches=1000,
        monthly_search_volumes=(),
        competition="LOW",
        competition_index=10,
        low_top_of_page_bid_micros=None,
        high_top_of_page_bid_micros=None,
    )


# -- normalization helpers -------------------------------------------------
def test_normalize_nfkc_fullwidth_and_case() -> None:
    # 全角 ASCII / 全角スペース + 大文字が NFKC + casefold + 空白正規化で畳まれる
    assert normalize_keyword_match_text("ＡＩ　議事録　おすすめ") == "ai 議事録 おすすめ"
    assert normalize_keyword_match_text("ChatGPT 料金") == "chatgpt 料金"
    assert normalize_keyword_match_text("ＲＰＡ２ 比較") == "rpa2 比較"


def test_normalize_collapses_runs_of_unicode_whitespace() -> None:
    assert normalize_keyword_match_text("  ai\t 議事録　\n おすすめ  ") == "ai 議事録 おすすめ"


def test_compact_key_removes_all_whitespace() -> None:
    assert compact_keyword_match_key("AI 議事録 おすすめ") == "ai議事録おすすめ"
    assert compact_keyword_match_key("ai 議事 録 おすすめ") == "ai議事録おすすめ"
    assert compact_keyword_match_key("生成AI ツール 比較") == "生成aiツール比較"
    assert compact_keyword_match_key("生成 ai ツール 比較") == "生成aiツール比較"


# -- stage 1: normalized exact match -------------------------------------
@pytest.mark.parametrize(
    ("requested", "response"),
    [
        ("ChatGPT 料金", "chatgpt 料金"),  # case D
        ("RPA 比較", "rpa 比較"),  # case E
        ("ＡＩ 議事録", "ai 議事録"),  # 全角 → NFKC
    ],
)
def test_normalized_exact_match(requested: str, response: str) -> None:
    assert _match_metrics([_m(response)], requested).keyword == response


# -- stage 2: whitespace-insensitive compact match ---------------------
@pytest.mark.parametrize(
    ("requested", "response"),
    [
        ("AI 議事録 おすすめ", "ai 議事 録 おすすめ"),  # case A
        ("AI 業務効率化", "ai 業務 効率 化"),  # case B
        ("生成AI ツール 比較", "生成 ai ツール 比較"),  # case C
    ],
)
def test_whitespace_insensitive_compact_match(requested: str, response: str) -> None:
    other = _m("無関係 キーワード")
    hit = _match_metrics([other, _m(response)], requested)
    assert hit is not None and hit.keyword == response


def test_compact_match_can_be_disabled() -> None:
    # allow_whitespace_insensitive_match=False なら空白差は吸収しない
    assert (
        _match_metrics(
            [_m("ai 議事 録 おすすめ")],
            "AI 議事録 おすすめ",
            allow_single_result_fallback=False,
            allow_whitespace_insensitive_match=False,
        )
        is None
    )


# -- exact-first ------------------------------------------------------
def test_exact_wins_before_compact() -> None:
    exact = _m("ai 議事録 おすすめ")  # normalized exact
    compact_variant = _m("ai 議事 録 おすすめ")  # compact のみ一致
    assert _match_metrics([compact_variant, exact], "AI 議事録 おすすめ") is exact


# -- false positives (§11) -----------------------------------------
@pytest.mark.parametrize(
    ("requested", "response"),
    [
        ("AI 議事録", "AI 議事録 比較"),
        ("RPA 比較", "RPA 導入"),
        ("ChatGPT 料金", "ChatGPT Plus 料金"),
        ("AI 議事録 おすすめ", "AI 議事録 おすすめ ツール"),
    ],
)
def test_different_keyword_does_not_match(requested: str, response: str) -> None:
    # 空白差ではなく語そのものが違うので、compact key でも一致しない。
    # (単一応答 fallback は bulk と同じく無効化して純粋に照合ロジックを見る)
    assert (
        _match_metrics([_m(response)], requested, allow_single_result_fallback=False)
        is None
    )
    # decoy を足して len != 1 にしても一致しない
    assert (
        _match_metrics([_m("別のもの"), _m(response)], requested)
        is None
    )


# -- ambiguity: multiple response rows share a key (§12) ---------
def test_ambiguous_normalized_response_returns_none() -> None:
    rows = [_m("chatgpt 料金"), _m("ChatGPT　料金")]  # NFKC 後どちらも "chatgpt 料金"
    assert _match_metrics(rows, "ChatGPT 料金") is None


def test_ambiguous_compact_response_returns_none() -> None:
    # 応答 2 行がどちらも compact key "ai議事録おすすめ" に該当 (normalized では別物)
    rows = [_m("ai 議事 録 おすすめ"), _m("ai議事 録 おすすめ")]
    assert _match_metrics(rows, "AI 議事録 おすすめ") is None
    # 決定論: 順序を入れ替えても None
    assert _match_metrics(list(reversed(rows)), "AI 議事録 おすすめ") is None


# -- single-result fallback policy (§7 / §13 / §14) -----------
def test_single_result_fallback_enabled_for_single_collector() -> None:
    # 応答 1 件・表記が想定外でも従来どおり拾う (単体 collector 挙動を維持)
    only = _m("なんらかの別表記")
    assert _match_metrics([only], "AI 議事録 おすすめ") is only
    assert (
        _match_metrics([only], "AI 議事録 おすすめ", allow_single_result_fallback=True)
        is only
    )


def test_single_result_fallback_disabled_for_bulk() -> None:
    only = _m("なんらかの別表記")
    assert (
        _match_metrics([only], "AI 議事録 おすすめ", allow_single_result_fallback=False)
        is None
    )


def test_bulk_single_response_matches_only_the_real_keyword() -> None:
    # 複数 requested に対し応答が 1 件。明示的に一致する keyword だけが拾える。
    resp = [_m("chatgpt 料金")]
    assert _match_metrics(resp, "ChatGPT 料金", allow_single_result_fallback=False) is resp[0]
    assert _match_metrics(resp, "RPA 比較", allow_single_result_fallback=False) is None
    assert _match_metrics(resp, "AI 業務効率化", allow_single_result_fallback=False) is None


def test_empty_metrics_list_returns_none() -> None:
    assert _match_metrics([], "AI 議事録 おすすめ") is None
    assert _match_metrics([], "AI 議事録 おすすめ", allow_single_result_fallback=True) is None
