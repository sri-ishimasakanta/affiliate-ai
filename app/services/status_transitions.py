"""status 遷移の許可ルール。

宣言的な遷移表と 1 つの検証関数のみ。汎用ワークフローエンジンは作らない。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.exceptions import InvalidStatusTransitionError
from app.models.enums import ArticleStatus, KeywordStatus

# Keyword:
#   discovered -> analyzed
#   analyzed   -> selected / rejected
#   selected   -> assigned
KEYWORD_TRANSITIONS: Mapping[KeywordStatus, frozenset[KeywordStatus]] = {
    KeywordStatus.DISCOVERED: frozenset({KeywordStatus.ANALYZED}),
    KeywordStatus.ANALYZED: frozenset({KeywordStatus.SELECTED, KeywordStatus.REJECTED}),
    KeywordStatus.SELECTED: frozenset({KeywordStatus.ASSIGNED}),
    KeywordStatus.ASSIGNED: frozenset(),
    KeywordStatus.REJECTED: frozenset(),
}

# Article:
#   idea -> planned -> drafting -> review -> approved -> published -> rewrite -> review
#   archived <- approved / published / rewrite
ARTICLE_TRANSITIONS: Mapping[ArticleStatus, frozenset[ArticleStatus]] = {
    ArticleStatus.IDEA: frozenset({ArticleStatus.PLANNED}),
    ArticleStatus.PLANNED: frozenset({ArticleStatus.DRAFTING}),
    ArticleStatus.DRAFTING: frozenset({ArticleStatus.REVIEW}),
    ArticleStatus.REVIEW: frozenset({ArticleStatus.APPROVED}),
    ArticleStatus.APPROVED: frozenset({ArticleStatus.PUBLISHED, ArticleStatus.ARCHIVED}),
    ArticleStatus.PUBLISHED: frozenset({ArticleStatus.REWRITE, ArticleStatus.ARCHIVED}),
    ArticleStatus.REWRITE: frozenset({ArticleStatus.REVIEW, ArticleStatus.ARCHIVED}),
    ArticleStatus.ARCHIVED: frozenset(),
}


def ensure_transition_allowed(
    entity: str,
    current: KeywordStatus | ArticleStatus,
    target: KeywordStatus | ArticleStatus,
    table: Mapping[Any, frozenset[Any]],
) -> None:
    """``current`` から ``target`` への遷移が許可されているか検証する。

    同一 status への変更は許可する。それ以外で遷移表に無い組み合わせは
    :class:`InvalidStatusTransitionError` を送出する。
    """

    if target == current:
        return
    if target not in table.get(current, frozenset()):
        raise InvalidStatusTransitionError(entity, current, target)
