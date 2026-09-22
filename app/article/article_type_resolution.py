"""C3: 記事タイプの実効値 (pure)。

記事タイプは **編集上の決定** であって keyword の推論結果ではない。C2.5.8 の
monetization mode と同じ形にそろえる:

    articles.article_type が NOT NULL -> その明示値 (source = ``explicit``)
    NULL                              -> keyword からの推論 (source = ``inferred``)

推論 (:func:`app.article.planning.classify_article_type`) は **推奨** として残す。
料金 / 無料 のような keyword は marker だけでは決まらないので、承認時に人が選べることが
必須 (推論だけが確定手段ではない)。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.article.planning import ArticleType, classify_article_type

SOURCE_EXPLICIT = "explicit"
SOURCE_INFERRED = "inferred"
TYPE_SOURCES = (SOURCE_EXPLICIT, SOURCE_INFERRED)


@dataclass(frozen=True)
class EffectiveArticleType:
    """``(article_type, source)``。``stored`` は DB の生の値 (legacy 行は None)。"""

    article_type: ArticleType | None
    source: str
    stored: str | None
    #: 推論結果 (推奨)。明示値があっても参考として残す
    recommended: ArticleType | None
    recommendation_marker: str | None

    @property
    def is_explicit(self) -> bool:
        return self.source == SOURCE_EXPLICIT

    @property
    def is_resolved(self) -> bool:
        return self.article_type is not None

    @property
    def overrides_recommendation(self) -> bool:
        """人が推論と違う型を選んだか (推論できなかった場合を含む)。"""

        return self.is_explicit and self.article_type is not self.recommended


def validated_article_type(value: str) -> ArticleType:
    """既知の記事タイプならその enum。未知なら fail-closed で :class:`ValueError`。"""

    try:
        return ArticleType(value)
    except ValueError as exc:
        raise ValueError(
            f"unknown article_type {value!r} (expected one of "
            f"{[t.value for t in ArticleType]})"
        ) from exc


def resolve_article_type(stored: str | None, keyword: str) -> EffectiveArticleType:
    """article の実効記事タイプ。明示値があればそれ、無ければ keyword から推論する。"""

    recommendation = classify_article_type(keyword)
    if stored is None:
        return EffectiveArticleType(
            article_type=recommendation.article_type,
            source=SOURCE_INFERRED,
            stored=None,
            recommended=recommendation.article_type,
            recommendation_marker=recommendation.matched_marker,
        )
    return EffectiveArticleType(
        article_type=validated_article_type(stored),
        source=SOURCE_EXPLICIT,
        stored=stored,
        recommended=recommendation.article_type,
        recommendation_marker=recommendation.matched_marker,
    )
