"""文字ベースの類似度計算 (pure)。

DB / SQLAlchemy / FastAPI 非依存。追加 pip dependency なし (標準ライブラリのみ)。

originality V1 で使う似度尺度:

    similarity = max( character bigram Dice , SequenceMatcher.ratio )

- 入力は「Unicode NFKC → casefold → 空白正規化」した文字列から **さらに空白を除去**
  したもの (``"AI 議事録" → "ai 議事録" → "ai議事録"``)。日本語は空白分割されないため、
  文字 bigram を主尺度にする (token Jaccard / TF-IDF / trigram は使わない)。
- **bigram は set 方式**。keyword は短く、同一 bigram の反復は稀。multiset (Counter)
  で重み付けすると反復部分文字列を過剰に減点し metric が解釈しづらくなるため、
  fuzzy string matching で一般的な「n-gram 集合の Sørensen–Dice」を採用する。
- 丸めは行わない。丸めは呼び出し側 (normalizer) が必要な箇所だけで行う。
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from app.keyword.normalizers.site_relevance import normalize_keyword

_NGRAM_SIZE = 2


@dataclass(frozen=True)
class SimilarityResult:
    bigram_dice: float
    sequence_matcher: float
    similarity: float


def _similarity_text(value: str) -> str:
    """正規化 (NFKC / casefold / 空白正規化) 後、さらに空白を除去した比較用文字列。"""

    return normalize_keyword(value).replace(" ", "")


def _bigrams(text: str) -> set[str]:
    return {text[i : i + _NGRAM_SIZE] for i in range(len(text) - _NGRAM_SIZE + 1)}


def character_bigram_dice(a: str, b: str) -> float:
    """文字 bigram 集合の Sørensen–Dice 係数 (0.0〜1.0)。

    - 同一文字列 (両空文字列を含む) → 1.0
    - 片方のみ空 → 0.0
    - bigram を作れない (1 文字) 不一致文字列 → 0.0 (SequenceMatcher との max で救済)
    """

    if a == b:
        return 1.0
    if not a or not b:
        return 0.0

    grams_a = _bigrams(a)
    grams_b = _bigrams(b)
    if not grams_a or not grams_b:
        return 0.0

    overlap = len(grams_a & grams_b)
    return 2 * overlap / (len(grams_a) + len(grams_b))


def sequence_similarity(a: str, b: str) -> float:
    """``difflib.SequenceMatcher`` の ratio (autojunk 無効)。"""

    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def _clamp_unit(value: float) -> float:
    return min(1.0, max(0.0, value))


def text_similarity(a: str, b: str) -> SimilarityResult:
    """2 つのテキストの類似度を返す。``similarity = max(bigram_dice, sequence_matcher)``。"""

    text_a = _similarity_text(a)
    text_b = _similarity_text(b)

    bigram = _clamp_unit(character_bigram_dice(text_a, text_b))
    sequence = _clamp_unit(sequence_similarity(text_a, text_b))
    return SimilarityResult(
        bigram_dice=bigram,
        sequence_matcher=sequence,
        similarity=max(bigram, sequence),
    )
