"""originality component の正規化ロジック (V1)。

**サイト内部のカニバリゼーション可能性の逆指標。** 新しい keyword で記事を作ったとき、
既存の内部 Keyword / Article と検索意図がどれだけ重複しないか (= どれだけ新しい
コンテンツ機会か) を 0〜100 で表す。

- 高い → 内部重複が少ない / 低い → 既存 Keyword・Article と非常に近い。
- **Google 検索結果上の外部競合 (`competition_ease`) とは別物。**
- 外部 API / LLM / embedding / vector DB / 追加 pip dependency なし。決定論的。
- similarity = ``max(char bigram Dice, difflib.SequenceMatcher ratio)`` (`text_similarity`)。
  commercial suffix 削除なし / intent adjustment なし / Article body 不使用。
- Article title の一致は keyword 一致より弱い証拠なので evidence weight 0.80。

DB / FastAPI / SQLAlchemy 非依存 (入力は candidate DTO の sequence)。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.keyword.text_similarity import text_similarity

NORMALIZER_NAME = "originality"
NORMALIZER_VERSION = "v1"

NGRAM_SIZE = 2
KEYWORD_EVIDENCE_WEIGHT = 1.0
ARTICLE_KEYWORD_EVIDENCE_WEIGHT = 1.0
TITLE_EVIDENCE_WEIGHT = 0.80
EMPTY_CORPUS_VALUE = 100.0

KIND_KEYWORD = "keyword"
KIND_ARTICLE_KEYWORD = "article_keyword"
KIND_ARTICLE_TITLE = "article_title"

# 同率 max のときの安定 tie-break: effective DESC → kind priority ASC → id ASC。
_KIND_PRIORITY = {
    KIND_KEYWORD: 0,
    KIND_ARTICLE_KEYWORD: 1,
    KIND_ARTICLE_TITLE: 2,
}


@dataclass(frozen=True)
class OriginalityCandidate:
    kind: str
    text: str
    evidence_weight: float
    keyword_id: int | None = None
    article_id: int | None = None


@dataclass(frozen=True)
class OriginalityResult:
    normalized_value: float
    max_similarity: float
    raw_similarity: float
    bigram_dice: float
    sequence_matcher: float
    corpus_available: bool
    evidence_coverage: float
    candidates_count: int
    most_similar_kind: str | None
    most_similar_text: str | None
    most_similar_keyword_id: int | None
    most_similar_article_id: int | None
    normalizer_name: str
    normalizer_version: str


def _clamp_unit(value: float) -> float:
    return min(1.0, max(0.0, value))


def _clamp_score(value: float) -> float:
    return min(100.0, max(0.0, value))


def _empty_result() -> OriginalityResult:
    return OriginalityResult(
        normalized_value=EMPTY_CORPUS_VALUE,
        max_similarity=0.0,
        raw_similarity=0.0,
        bigram_dice=0.0,
        sequence_matcher=0.0,
        corpus_available=False,
        evidence_coverage=0.0,
        candidates_count=0,
        most_similar_kind=None,
        most_similar_text=None,
        most_similar_keyword_id=None,
        most_similar_article_id=None,
        normalizer_name=NORMALIZER_NAME,
        normalizer_version=NORMALIZER_VERSION,
    )


def calculate_originality(
    candidates: Sequence[OriginalityCandidate],
    *,
    keyword: str,
) -> OriginalityResult:
    """keyword と内部 corpus candidate から originality (0〜100) を算出する。

    eligible candidate が 0 件 → ``EMPTY_CORPUS_VALUE`` (100) だが
    ``corpus_available = false`` / ``evidence_coverage = 0``。これは「比較対象が
    現在の内部 corpus に存在しない」の意味であり、独創性が証明された意味ではない。
    """

    if not candidates:
        return _empty_result()

    best_effective = -1.0
    best_priority = 0
    best_id = 0
    best_candidate: OriginalityCandidate | None = None
    best_raw = 0.0
    best_bigram = 0.0
    best_sequence = 0.0

    for candidate in candidates:
        result = text_similarity(keyword, candidate.text)
        raw = result.similarity
        effective = _clamp_unit(raw * candidate.evidence_weight)
        priority = _KIND_PRIORITY.get(candidate.kind, len(_KIND_PRIORITY))
        candidate_id = candidate.keyword_id or candidate.article_id or 0

        key = (effective, -priority, -candidate_id)
        best_key = (best_effective, -best_priority, -best_id)
        if best_candidate is None or key > best_key:
            best_effective = effective
            best_priority = priority
            best_id = candidate_id
            best_candidate = candidate
            best_raw = raw
            best_bigram = result.bigram_dice
            best_sequence = result.sequence_matcher

    assert best_candidate is not None  # candidates is non-empty
    originality = round(_clamp_score(100.0 * (1.0 - best_effective)), 2)

    return OriginalityResult(
        normalized_value=originality,
        max_similarity=best_effective,
        raw_similarity=best_raw,
        bigram_dice=best_bigram,
        sequence_matcher=best_sequence,
        corpus_available=True,
        evidence_coverage=1.0,
        candidates_count=len(candidates),
        most_similar_kind=best_candidate.kind,
        most_similar_text=best_candidate.text,
        most_similar_keyword_id=best_candidate.keyword_id,
        most_similar_article_id=best_candidate.article_id,
        normalizer_name=NORMALIZER_NAME,
        normalizer_version=NORMALIZER_VERSION,
    )
