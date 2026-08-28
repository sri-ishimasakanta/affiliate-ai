"""KeywordService の責務 (ビジネスロジック / トランザクション制御) を検証する。"""

import pytest
from sqlalchemy.orm import Session

from app.exceptions import (
    DuplicateEntityError,
    EntityNotFoundError,
    InvalidStatusTransitionError,
)
from app.keyword.schemas import KeywordCreate, KeywordUpdate
from app.models.enums import KeywordStatus
from app.services.keyword_service import KeywordService


def _service(session: Session) -> KeywordService:
    return KeywordService(session)


def test_create_keyword_persists_and_maps_fields(session: Session) -> None:
    service = _service(session)

    read = service.create_keyword(
        KeywordCreate(keyword="nisa 始め方", search_intent="commercial", category="投資")
    )

    session.rollback()  # commit 済みなら消えない

    assert read.id is not None
    assert read.keyword == "nisa 始め方"
    assert read.search_intent == "commercial"
    assert read.category == "投資"
    assert read.status == KeywordStatus.DISCOVERED
    assert read.opportunity_score is None
    assert service.get_keyword(read.id).keyword == "nisa 始め方"


def test_get_keyword_missing_raises(session: Session) -> None:
    with pytest.raises(EntityNotFoundError):
        _service(session).get_keyword(4242)


def test_list_keywords_pagination(session: Session) -> None:
    service = _service(session)
    for index in range(4):
        service.create_keyword(KeywordCreate(keyword=f"kw-{index}"))

    page = service.list_keywords(limit=2, offset=2)

    assert [item.keyword for item in page] == ["kw-2", "kw-3"]


def test_update_keyword_is_partial(session: Session) -> None:
    service = _service(session)
    read = service.create_keyword(
        KeywordCreate(keyword="ふるさと納税", search_intent="informational", category="税")
    )

    updated = service.update_keyword(read.id, KeywordUpdate(category="節税"))

    assert updated.category == "節税"
    assert updated.search_intent == "informational"  # 未指定は据え置き


def test_update_keyword_missing_raises(session: Session) -> None:
    with pytest.raises(EntityNotFoundError):
        _service(session).update_keyword(1, KeywordUpdate(category="x"))


def test_delete_keyword(session: Session) -> None:
    service = _service(session)
    read = service.create_keyword(KeywordCreate(keyword="消す"))

    service.delete_keyword(read.id)

    with pytest.raises(EntityNotFoundError):
        service.get_keyword(read.id)


def test_duplicate_keyword_raises_application_error(session: Session) -> None:
    service = _service(session)
    service.create_keyword(KeywordCreate(keyword="重複 kw"))

    with pytest.raises(DuplicateEntityError):
        service.create_keyword(KeywordCreate(keyword="重複 kw"))


def test_valid_status_transition_chain(session: Session) -> None:
    service = _service(session)
    read = service.create_keyword(KeywordCreate(keyword="遷移 kw"))

    analyzed = service.change_status(read.id, KeywordStatus.ANALYZED)
    assert analyzed.status == KeywordStatus.ANALYZED

    selected = service.change_status(read.id, KeywordStatus.SELECTED)
    assert selected.status == KeywordStatus.SELECTED

    assigned = service.change_status(read.id, KeywordStatus.ASSIGNED)
    assert assigned.status == KeywordStatus.ASSIGNED

    # 同一 status は許可
    assert service.change_status(read.id, KeywordStatus.ASSIGNED).status == KeywordStatus.ASSIGNED


def test_invalid_status_transition_raises(session: Session) -> None:
    service = _service(session)
    read = service.create_keyword(KeywordCreate(keyword="不正遷移 kw"))

    with pytest.raises(InvalidStatusTransitionError):
        service.change_status(read.id, KeywordStatus.ASSIGNED)  # discovered -> assigned

    assert service.get_keyword(read.id).status == KeywordStatus.DISCOVERED  # 変更されない


def test_change_status_missing_raises(session: Session) -> None:
    with pytest.raises(EntityNotFoundError):
        _service(session).change_status(1, KeywordStatus.ANALYZED)
