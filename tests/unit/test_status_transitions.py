from itertools import pairwise

import pytest

from app.exceptions import InvalidStatusTransitionError
from app.models.enums import ArticleStatus, KeywordStatus
from app.services.status_transitions import (
    ARTICLE_TRANSITIONS,
    KEYWORD_TRANSITIONS,
    ensure_transition_allowed,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (KeywordStatus.DISCOVERED, KeywordStatus.ANALYZED),
        (KeywordStatus.ANALYZED, KeywordStatus.SELECTED),
        (KeywordStatus.ANALYZED, KeywordStatus.REJECTED),
        (KeywordStatus.SELECTED, KeywordStatus.ASSIGNED),
    ],
)
def test_keyword_valid_transitions(current: KeywordStatus, target: KeywordStatus) -> None:
    ensure_transition_allowed("Keyword", current, target, KEYWORD_TRANSITIONS)


def test_keyword_same_status_is_allowed() -> None:
    ensure_transition_allowed(
        "Keyword", KeywordStatus.ANALYZED, KeywordStatus.ANALYZED, KEYWORD_TRANSITIONS
    )


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (KeywordStatus.DISCOVERED, KeywordStatus.SELECTED),
        (KeywordStatus.DISCOVERED, KeywordStatus.ASSIGNED),
        (KeywordStatus.ANALYZED, KeywordStatus.ASSIGNED),
        (KeywordStatus.SELECTED, KeywordStatus.REJECTED),
        (KeywordStatus.ASSIGNED, KeywordStatus.SELECTED),
        (KeywordStatus.REJECTED, KeywordStatus.ANALYZED),
    ],
)
def test_keyword_invalid_transitions(current: KeywordStatus, target: KeywordStatus) -> None:
    with pytest.raises(InvalidStatusTransitionError):
        ensure_transition_allowed("Keyword", current, target, KEYWORD_TRANSITIONS)


def test_article_valid_forward_chain() -> None:
    chain = [
        ArticleStatus.IDEA,
        ArticleStatus.PLANNED,
        ArticleStatus.DRAFTING,
        ArticleStatus.REVIEW,
        ArticleStatus.APPROVED,
        ArticleStatus.PUBLISHED,
        ArticleStatus.REWRITE,
        ArticleStatus.REVIEW,
    ]
    for current, target in pairwise(chain):
        ensure_transition_allowed("Article", current, target, ARTICLE_TRANSITIONS)


@pytest.mark.parametrize(
    "current",
    [ArticleStatus.APPROVED, ArticleStatus.PUBLISHED, ArticleStatus.REWRITE],
)
def test_article_archived_is_reachable(current: ArticleStatus) -> None:
    ensure_transition_allowed("Article", current, ArticleStatus.ARCHIVED, ARTICLE_TRANSITIONS)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ArticleStatus.IDEA, ArticleStatus.DRAFTING),
        (ArticleStatus.PLANNED, ArticleStatus.REVIEW),
        (ArticleStatus.DRAFTING, ArticleStatus.APPROVED),
        (ArticleStatus.IDEA, ArticleStatus.ARCHIVED),
        (ArticleStatus.PLANNED, ArticleStatus.ARCHIVED),
        (ArticleStatus.ARCHIVED, ArticleStatus.REVIEW),
        (ArticleStatus.PUBLISHED, ArticleStatus.PLANNED),
    ],
)
def test_article_invalid_transitions(current: ArticleStatus, target: ArticleStatus) -> None:
    with pytest.raises(InvalidStatusTransitionError):
        ensure_transition_allowed("Article", current, target, ARTICLE_TRANSITIONS)
