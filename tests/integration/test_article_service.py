"""ArticleService の責務 (ビジネスロジック / トランザクション制御) を検証する。"""

import pytest
from sqlalchemy.orm import Session

from app.article.schemas import ArticleCreate, ArticleUpdate
from app.exceptions import (
    DuplicateEntityError,
    EntityNotFoundError,
    InvalidStatusTransitionError,
    ProtectedArticleStatusTransitionError,
)
from app.keyword.schemas import KeywordCreate
from app.models import Article
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


def test_valid_generic_status_transitions_up_to_review(session: Session) -> None:
    """review 到達までの非センシティブな遷移は汎用 change_status で引き続き可能。

    review -> approved と * -> published は保護対象 (別テストで検証)。
    approved / published への到達後の published_at 設定は将来の専用
    publication workflow の責務であり、このサービスでは扱わない。
    """

    service = _article_service(session)
    read = service.create_article(ArticleCreate(title="T", slug="flow-slug"))

    for target in (
        ArticleStatus.PLANNED,
        ArticleStatus.DRAFTING,
        ArticleStatus.REVIEW,
    ):
        assert service.change_status(read.id, target).status == target

    # rewrite への到達は approved/published を経由しないと許可表上不可なので、
    # ここでは review までの正当な汎用遷移が機能することのみを確認する。


def test_generic_change_status_rejects_review_to_approved(session: Session) -> None:
    service = _article_service(session)
    read = service.create_article(ArticleCreate(title="T", slug="protected-approved"))
    for target in (ArticleStatus.PLANNED, ArticleStatus.DRAFTING, ArticleStatus.REVIEW):
        service.change_status(read.id, target)

    with pytest.raises(ProtectedArticleStatusTransitionError):
        service.change_status(read.id, ArticleStatus.APPROVED)

    assert service.get_article(read.id).status == ArticleStatus.REVIEW


def test_generic_change_status_rejects_any_target_published(session: Session) -> None:
    service = _article_service(session)
    read = service.create_article(ArticleCreate(title="T", slug="protected-published"))
    for target in (ArticleStatus.PLANNED, ArticleStatus.DRAFTING, ArticleStatus.REVIEW):
        service.change_status(read.id, target)

    # directly move to approved at the DB level (bypassing the generic API,
    # simulating the dedicated approval workflow) so we can prove the generic
    # endpoint also rejects approved -> published specifically.
    entity = session.get(Article, read.id)
    entity.status = ArticleStatus.APPROVED.value
    session.flush()

    with pytest.raises(ProtectedArticleStatusTransitionError):
        service.change_status(read.id, ArticleStatus.PUBLISHED)

    assert service.get_article(read.id).status == ArticleStatus.APPROVED


def test_invalid_status_transition_raises(session: Session) -> None:
    service = _article_service(session)
    read = service.create_article(ArticleCreate(title="T", slug="bad-flow"))

    with pytest.raises(InvalidStatusTransitionError):
        service.change_status(read.id, ArticleStatus.REVIEW)  # idea -> review

    assert service.get_article(read.id).status == ArticleStatus.IDEA


def test_archived_from_approved_is_allowed(session: Session) -> None:
    """approved -> archived は ARTICLE_TRANSITIONS 上は引き続き有効。

    approved 自体には汎用 API から到達できないため (保護対象)、専用の
    approval workflow を模して DB 上で直接 approved にしてから確認する。
    """

    service = _article_service(session)
    read = service.create_article(ArticleCreate(title="T", slug="arch-slug"))
    for target in (ArticleStatus.PLANNED, ArticleStatus.DRAFTING, ArticleStatus.REVIEW):
        service.change_status(read.id, target)

    entity = session.get(Article, read.id)
    entity.status = ArticleStatus.APPROVED.value
    session.flush()

    archived = service.change_status(read.id, ArticleStatus.ARCHIVED)
    assert archived.status == ArticleStatus.ARCHIVED
