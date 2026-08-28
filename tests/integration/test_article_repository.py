"""ArticleRepository の責務 (DB アクセスのみ / commit しない) を検証する。"""

from sqlalchemy.orm import Session

from app.models import Article
from app.models.enums import ArticleStatus
from app.repositories.article_repository import ArticleRepository


def test_create_flushes_but_does_not_commit(session: Session) -> None:
    repo = ArticleRepository(session)

    created = repo.create(title="タイトル", slug="title-slug")

    assert created.id is not None
    assert created.status == ArticleStatus.IDEA

    session.rollback()
    assert repo.get_by_slug("title-slug") is None


def test_get_by_id_and_get_by_slug(session: Session) -> None:
    repo = ArticleRepository(session)
    created = repo.create(title="T", slug="only-slug")

    assert repo.get_by_id(created.id) is created
    assert repo.get_by_slug("only-slug") is created
    assert repo.get_by_id(999999) is None
    assert repo.get_by_slug("no-slug") is None


def test_list_supports_limit_and_offset(session: Session) -> None:
    repo = ArticleRepository(session)
    created = [repo.create(title=f"T{index}", slug=f"slug-{index}") for index in range(5)]

    page = repo.list(limit=2, offset=3)

    assert [item.id for item in page] == [created[3].id, created[4].id]


def test_update_sets_attributes(session: Session) -> None:
    repo = ArticleRepository(session)
    created = repo.create(title="T", slug="s")

    repo.update(created, {"title": "新タイトル", "body": "本文ドラフト"})

    reloaded = repo.get_by_id(created.id)
    assert reloaded is not None
    assert reloaded.title == "新タイトル"
    assert reloaded.body == "本文ドラフト"


def test_delete_removes_row(session: Session) -> None:
    repo = ArticleRepository(session)
    created = repo.create(title="T", slug="s")

    repo.delete(created)

    assert session.get(Article, created.id) is None
