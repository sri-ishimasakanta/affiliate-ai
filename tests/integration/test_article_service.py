"""ArticleService の責務 (ビジネスロジック / トランザクション制御) を検証する。"""

import pytest
from sqlalchemy.orm import Session

from app.article.schemas import ArticleCreate, ArticleUpdate
from app.exceptions import (
    DuplicateEntityError,
    EntityNotFoundError,
    InvalidStatusTransitionError,
)
from app.keyword.schemas import KeywordCreate
from app.models.enums import ArticleStatus
from app.services.article_service import ArticleService
from app.services.keyword_service import KeywordService


def _article_service(session: Session) -> ArticleService:
    return ArticleService(session)


def _make_keyword_id(session: Session, keyword: str = "kw") -> int:
    return KeywordService(session).create_keyword(KeywordCreate(keyword=keyword)).id


def test_create_article_persists_and_maps_fields(session: Session) -> None:
    service = _article_service(session)
    keyword_id = _make_keyword_id(session)

    read = service.create_article(
        ArticleCreate(keyword_id=keyword_id, title="記事タイトル", slug="kiji-title")
    )

    session.rollback()

    assert read.id is not None
    assert read.keyword_id == keyword_id
    assert read.slug == "kiji-title"
    assert read.status == ArticleStatus.IDEA
    assert read.draft_content is None
    assert read.published_url is None
    assert read.wordpress_id is None
    assert read.published_at is None
    assert service.get_article(read.id).title == "記事タイトル"


def test_create_article_without_keyword_is_allowed(session: Session) -> None:
    read = _article_service(session).create_article(
        ArticleCreate(title="キーワードなし", slug="no-keyword")
    )

    assert read.keyword_id is None


def test_get_article_missing_raises(session: Session) -> None:
    with pytest.raises(EntityNotFoundError):
        _article_service(session).get_article(777)


def test_list_articles_pagination(session: Session) -> None:
    service = _article_service(session)
    for index in range(4):
        service.create_article(ArticleCreate(title=f"T{index}", slug=f"slug-{index}"))

    page = service.list_articles(limit=2, offset=1)

    assert [item.slug for item in page] == ["slug-1", "slug-2"]


def test_update_article_is_partial_and_maps_draft_content(session: Session) -> None:
    service = _article_service(session)
    read = service.create_article(ArticleCreate(title="旧タイトル", slug="old-slug"))

    updated = service.update_article(
        read.id, ArticleUpdate(draft_content="# 下書き本文")
    )

    assert updated.draft_content == "# 下書き本文"
    assert updated.title == "旧タイトル"
    assert updated.slug == "old-slug"


def test_update_article_missing_raises(session: Session) -> None:
    with pytest.raises(EntityNotFoundError):
        _article_service(session).update_article(1, ArticleUpdate(title="x"))


def test_delete_article(session: Session) -> None:
    service = _article_service(session)
    read = service.create_article(ArticleCreate(title="T", slug="del-slug"))

    service.delete_article(read.id)

    with pytest.raises(EntityNotFoundError):
        service.get_article(read.id)


def test_duplicate_slug_raises_application_error(session: Session) -> None:
    service = _article_service(session)
    service.create_article(ArticleCreate(title="A", slug="dup-slug"))

    with pytest.raises(DuplicateEntityError):
        service.create_article(ArticleCreate(title="B", slug="dup-slug"))


def test_update_to_existing_slug_raises(session: Session) -> None:
    service = _article_service(session)
    service.create_article(ArticleCreate(title="A", slug="slug-a"))
    other = service.create_article(ArticleCreate(title="B", slug="slug-b"))

    with pytest.raises(DuplicateEntityError):
        service.update_article(other.id, ArticleUpdate(slug="slug-a"))


def test_nonexistent_keyword_raises_application_error(session: Session) -> None:
    service = _article_service(session)

    with pytest.raises(EntityNotFoundError):
        service.create_article(
            ArticleCreate(keyword_id=987654, title="T", slug="ghost-keyword")
        )

    # ロールバック済みで記事は作られていない
    assert service.list_articles() == []


def test_valid_status_transition_sets_published_at(session: Session) -> None:
    service = _article_service(session)
    read = service.create_article(ArticleCreate(title="T", slug="flow-slug"))

    for target in (
        ArticleStatus.PLANNED,
        ArticleStatus.DRAFTING,
        ArticleStatus.REVIEW,
        ArticleStatus.APPROVED,
    ):
        assert service.change_status(read.id, target).status == target

    published = service.change_status(read.id, ArticleStatus.PUBLISHED)
    assert published.status == ArticleStatus.PUBLISHED
    assert published.published_at is not None

    # published -> rewrite -> review
    assert service.change_status(read.id, ArticleStatus.REWRITE).status == ArticleStatus.REWRITE
    assert service.change_status(read.id, ArticleStatus.REVIEW).status == ArticleStatus.REVIEW


def test_invalid_status_transition_raises(session: Session) -> None:
    service = _article_service(session)
    read = service.create_article(ArticleCreate(title="T", slug="bad-flow"))

    with pytest.raises(InvalidStatusTransitionError):
        service.change_status(read.id, ArticleStatus.REVIEW)  # idea -> review

    assert service.get_article(read.id).status == ArticleStatus.IDEA


def test_archived_from_approved_is_allowed(session: Session) -> None:
    service = _article_service(session)
    read = service.create_article(ArticleCreate(title="T", slug="arch-slug"))
    for target in (
        ArticleStatus.PLANNED,
        ArticleStatus.DRAFTING,
        ArticleStatus.REVIEW,
        ArticleStatus.APPROVED,
    ):
        service.change_status(read.id, target)

    archived = service.change_status(read.id, ArticleStatus.ARCHIVED)
    assert archived.status == ArticleStatus.ARCHIVED
