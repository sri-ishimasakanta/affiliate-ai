"""OriginalityNormalizer V1 の unit テスト (DB / FastAPI 非依存)。"""

import pytest

from app.keyword.normalizers.originality import (
    EMPTY_CORPUS_VALUE,
    KIND_ARTICLE_KEYWORD,
    KIND_ARTICLE_TITLE,
    KIND_KEYWORD,
    NGRAM_SIZE,
    NORMALIZER_NAME,
    NORMALIZER_VERSION,
    TITLE_EVIDENCE_WEIGHT,
    OriginalityCandidate,
    calculate_originality,
)


def _kw(text: str, *, keyword_id: int = 1) -> OriginalityCandidate:
    return OriginalityCandidate(
        kind=KIND_KEYWORD, text=text, evidence_weight=1.0, keyword_id=keyword_id
    )


def _title(text: str, *, article_id: int = 1) -> OriginalityCandidate:
    return OriginalityCandidate(
        kind=KIND_ARTICLE_TITLE,
        text=text,
        evidence_weight=TITLE_EVIDENCE_WEIGHT,
        article_id=article_id,
    )


def _art_kw(text: str, *, keyword_id: int = 1, article_id: int = 1) -> OriginalityCandidate:
    return OriginalityCandidate(
        kind=KIND_ARTICLE_KEYWORD,
        text=text,
        evidence_weight=1.0,
        keyword_id=keyword_id,
        article_id=article_id,
    )


# -- empty corpus ------------------------------------------------
def test_empty_candidates() -> None:
    result = calculate_originality([], keyword="AI 議事録")
    assert result.normalized_value == EMPTY_CORPUS_VALUE == 100.0
    assert result.corpus_available is False
    assert result.evidence_coverage == 0.0
    assert result.max_similarity == 0.0
    assert result.candidates_count == 0
    assert result.most_similar_kind is None
    assert result.most_similar_keyword_id is None
    assert result.most_similar_article_id is None


# -- exact / near duplicate ------------------------------------
def test_exact_duplicate_keyword_is_zero() -> None:
    result = calculate_originality([_kw("AI 議事録", keyword_id=7)], keyword="AI 議事録")
    assert result.max_similarity == 1.0
    assert result.raw_similarity == 1.0
    assert result.normalized_value == 0.0
    assert result.most_similar_kind == KIND_KEYWORD
    assert result.most_similar_keyword_id == 7


def test_title_exact_only_effective_is_0_8() -> None:
    result = calculate_originality([_title("AI 議事録", article_id=3)], keyword="AI 議事録")
    assert result.raw_similarity == 1.0
    assert result.max_similarity == pytest.approx(0.80)
    assert result.normalized_value == 20.0
    assert result.most_similar_kind == KIND_ARTICLE_TITLE
    assert result.most_similar_article_id == 3
    assert result.most_similar_keyword_id is None


# -- representative pairs (調査傾向の regression) -----------------
@pytest.mark.parametrize(
    ("keyword", "candidate", "lo", "hi"),
    [
        ("AI 議事録", "AI 議事録 おすすめ", 0.55, 0.85),        # high
        ("AI 議事録 おすすめ", "AI 議事録 比較", 0.50, 0.80),     # near-dup
        ("ChatGPT 料金", "ChatGPT 使い方", 0.55, 0.95),         # similar だが < 1.0
        ("ChatGPT 料金", "ChatGPT Plus 料金", 0.70, 0.98),      # high
        ("業務効率化", "AI 業務効率化", 0.70, 0.95),             # high
        ("AI 議事録 おすすめ", "生成AI 法人 導入", 0.0, 0.35),    # low
        ("ChatGPT 料金", "RPA 比較", 0.0, 0.30),               # low
    ],
)
def test_representative_pair_similarity_ranges(
    keyword: str, candidate: str, lo: float, hi: float
) -> None:
    result = calculate_originality([_kw(candidate, keyword_id=1)], keyword=keyword)
    assert lo <= result.max_similarity <= hi
    assert result.max_similarity < 1.0  # 完全重複ではない


def test_commercial_suffix_not_stripped() -> None:
    # "ChatGPT 料金" vs "ChatGPT 使い方" は 1.0 (完全重複) にならない
    result = calculate_originality(
        [_kw("ChatGPT 使い方", keyword_id=1)], keyword="ChatGPT 料金"
    )
    assert result.max_similarity < 1.0
    assert result.normalized_value > 0.0


# -- multiple candidates / tie-break --------------------------
def test_maximum_effective_similarity_wins() -> None:
    result = calculate_originality(
        [
            _kw("生成AI 法人 導入", keyword_id=1),  # low
            _kw("AI 議事録 比較", keyword_id=2),     # near-dup with "AI 議事録 おすすめ"
            _title("完全に無関係なタイトル", article_id=9),
        ],
        keyword="AI 議事録 おすすめ",
    )
    assert result.most_similar_kind == KIND_KEYWORD
    assert result.most_similar_keyword_id == 2


def test_keyword_beats_title_when_same_raw_similarity() -> None:
    # 同じ text "AI 議事録"。keyword は weight 1.0、title は 0.8 -> keyword が勝つ
    result = calculate_originality(
        [
            _title("AI 議事録", article_id=5),
            _kw("AI 議事録", keyword_id=8),
        ],
        keyword="AI 議事録",
    )
    assert result.most_similar_kind == KIND_KEYWORD
    assert result.most_similar_keyword_id == 8
    assert result.max_similarity == 1.0
    assert result.normalized_value == 0.0


def test_tie_break_is_deterministic_by_id() -> None:
    # 同一 text / 同一 kind / 同一 effective -> id ASC で安定
    a = calculate_originality(
        [_kw("AI 議事録", keyword_id=3), _kw("AI 議事録", keyword_id=1)],
        keyword="AI 議事録",
    )
    b = calculate_originality(
        [_kw("AI 議事録", keyword_id=1), _kw("AI 議事録", keyword_id=3)],
        keyword="AI 議事録",
    )
    assert a.most_similar_keyword_id == b.most_similar_keyword_id == 1


def test_article_keyword_kind_beats_title_kind_on_tie() -> None:
    result = calculate_originality(
        [
            _title("AI 議事録", article_id=1),
            _art_kw("AI 議事録", keyword_id=4, article_id=2),
        ],
        keyword="AI 議事録",
    )
    # どちらも text 一致だが title は weight 0.8、article_keyword は 1.0
    assert result.most_similar_kind == KIND_ARTICLE_KEYWORD
    assert result.most_similar_keyword_id == 4


# -- rounding / metadata --------------------------------------
def test_rounded_to_two_decimals() -> None:
    result = calculate_originality(
        [_kw("AI 文字起こし ツール", keyword_id=1)], keyword="AI 議事録 おすすめ"
    )
    assert round(result.normalized_value, 2) == result.normalized_value


def test_metadata() -> None:
    result = calculate_originality([_kw("x", keyword_id=1)], keyword="y")
    assert result.normalizer_name == NORMALIZER_NAME == "originality"
    assert result.normalizer_version == NORMALIZER_VERSION == "v1"
    assert NGRAM_SIZE == 2
    assert result.corpus_available is True
    assert result.evidence_coverage == 1.0
    assert result.candidates_count == 1


def test_deterministic() -> None:
    cands = [_kw("AI 議事録 比較", keyword_id=2), _title("生成AI", article_id=3)]
    assert calculate_originality(cands, keyword="AI 議事録 おすすめ") == (
        calculate_originality(cands, keyword="AI 議事録 おすすめ")
    )
