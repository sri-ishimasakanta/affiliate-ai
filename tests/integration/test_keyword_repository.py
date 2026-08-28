"""KeywordRepository の責務 (DB アクセスのみ / commit しない) を検証する。"""

from sqlalchemy.orm import Session

from app.models import Keyword
from app.models.enums import KeywordStatus
from app.repositories.keyword_repository import KeywordRepository


def test_create_flushes_but_does_not_commit(session: Session) -> None:
    repo = KeywordRepository(session)

    created = repo.create(keyword="python 入門", intent="informational")

    assert created.id is not None  # flush で採番される
    assert created.status == KeywordStatus.DISCOVERED  # モデル default

    session.rollback()
    assert repo.get_by_keyword("python 入門") is None  # commit していない証拠


def test_get_by_id_and_get_by_keyword(session: Session) -> None:
    repo = KeywordRepository(session)
    created = repo.create(keyword="seo 対策")

    assert repo.get_by_id(created.id) is created
    assert repo.get_by_keyword("seo 対策") is created
    assert repo.get_by_id(999999) is None
    assert repo.get_by_keyword("未登録") is None


def test_list_supports_limit_and_offset(session: Session) -> None:
    repo = KeywordRepository(session)
    created = [repo.create(keyword=f"kw-{index}") for index in range(5)]

    page = repo.list(limit=2, offset=1)

    assert [item.id for item in page] == [created[1].id, created[2].id]


def test_update_sets_attributes_and_flushes(session: Session) -> None:
    repo = KeywordRepository(session)
    created = repo.create(keyword="更新対象")

    repo.update(created, {"intent": "commercial", "category": "finance"})

    reloaded = repo.get_by_id(created.id)
    assert reloaded is not None
    assert reloaded.intent == "commercial"
    assert reloaded.category == "finance"


def test_delete_removes_row(session: Session) -> None:
    repo = KeywordRepository(session)
    created = repo.create(keyword="削除対象")

    repo.delete(created)

    assert session.get(Keyword, created.id) is None
