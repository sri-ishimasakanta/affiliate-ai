"""KeywordSignalService の検証 (Keyword 存在確認 / commit / rollback)。"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.exceptions import EntityNotFoundError
from app.keyword.schemas import KeywordSignalCreate
from app.models import Keyword
from app.repositories.keyword_signal_repository import KeywordSignalRepository
from app.services.keyword_signal_service import KeywordSignalService

_OBSERVED = datetime(2026, 8, 1, tzinfo=UTC)


def _payload(
    component: str = "trend",
    normalized_value: float = 42.0,
    provider: str = "google_trends",
    **extra: object,
) -> KeywordSignalCreate:
    return KeywordSignalCreate(
        component=component,
        normalized_value=normalized_value,
        provider=provider,
        observed_at=_OBSERVED,
        **extra,
    )


def _make_keyword(session: Session, keyword: str = "kw") -> int:
    entity = Keyword(keyword=keyword)
    session.add(entity)
    session.flush()
    session.commit()
    return entity.id


def test_create_signal_persists(session: Session) -> None:
    keyword_id = _make_keyword(session)
    service = KeywordSignalService(session)

    read = service.create_signal(
        keyword_id, _payload(raw_data={"competition_index": 33}, source_reference="http://x")
    )

    session.rollback()  # commit 済みなら消えない

    assert read.id is not None
    assert read.keyword_id == keyword_id
    assert read.component == "trend"
    assert read.provider == "google_trends"
    assert read.raw_data == {"competition_index": 33}
    assert read.source_reference == "http://x"
    assert KeywordSignalRepository(session).get_by_id(read.id) is not None


def test_create_signal_nonexistent_keyword_raises(session: Session) -> None:
    service = KeywordSignalService(session)
    with pytest.raises(EntityNotFoundError):
        service.create_signal(999999, _payload())


def test_get_latest_signal(session: Session) -> None:
    keyword_id = _make_keyword(session)
    service = KeywordSignalService(session)
    service.create_signal(keyword_id, _payload("trend", 10))
    service.create_signal(keyword_id, _payload("trend", 20))
    newest = service.create_signal(keyword_id, _payload("trend", 30))

    latest = service.get_latest_signal(keyword_id, "trend")
    assert latest.id == newest.id
    assert latest.normalized_value == 30


def test_get_latest_signal_missing_component_raises(session: Session) -> None:
    keyword_id = _make_keyword(session)
    service = KeywordSignalService(session)
    service.create_signal(keyword_id, _payload("trend"))

    with pytest.raises(EntityNotFoundError):
        service.get_latest_signal(keyword_id, "originality")


def test_get_latest_signal_nonexistent_keyword_raises(session: Session) -> None:
    service = KeywordSignalService(session)
    with pytest.raises(EntityNotFoundError):
        service.get_latest_signal(999999, "trend")


def test_list_signals_all_and_filtered(session: Session) -> None:
    keyword_id = _make_keyword(session)
    service = KeywordSignalService(session)
    service.create_signal(keyword_id, _payload("trend"))
    service.create_signal(keyword_id, _payload("trend"))
    service.create_signal(keyword_id, _payload("originality"))

    assert len(service.list_signals(keyword_id)) == 3
    assert len(service.list_signals(keyword_id, component="trend")) == 2
    assert len(service.list_signals(keyword_id, component="originality")) == 1


def test_list_signals_pagination_newest_first(session: Session) -> None:
    keyword_id = _make_keyword(session)
    service = KeywordSignalService(session)
    ids = [service.create_signal(keyword_id, _payload("trend")).id for _ in range(5)]

    page = service.list_signals(keyword_id, limit=2, offset=1)
    assert [s.id for s in page] == [ids[3], ids[2]]


def test_get_signal_scoped_to_keyword(session: Session) -> None:
    kw_a = _make_keyword(session, "a")
    kw_b = _make_keyword(session, "b")
    service = KeywordSignalService(session)
    signal = service.create_signal(kw_a, _payload())

    assert service.get_signal(kw_a, signal.id).id == signal.id
    with pytest.raises(EntityNotFoundError):
        service.get_signal(kw_b, signal.id)


def test_create_signal_rolls_back_on_failure(session: Session, monkeypatch) -> None:
    keyword_id = _make_keyword(session)
    service = KeywordSignalService(session)

    def _boom() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(session, "commit", _boom)
    with pytest.raises(RuntimeError):
        service.create_signal(keyword_id, _payload())

    monkeypatch.undo()
    assert service.list_signals(keyword_id) == []
