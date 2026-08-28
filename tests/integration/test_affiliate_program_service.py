"""AffiliateProgramService の検証 (transaction ownership は Service)。"""

import pytest
from sqlalchemy.orm import Session

from app.affiliate.schemas import AffiliateProgramCreate, AffiliateProgramUpdate
from app.exceptions import DuplicateEntityError, EntityNotFoundError
from app.models.enums import AffiliateProgramStatus
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.services.affiliate_program_service import AffiliateProgramService


def _svc(session: Session) -> AffiliateProgramService:
    return AffiliateProgramService(session)


def test_create_and_get(session: Session) -> None:
    svc = _svc(session)
    created = svc.create_program(
        AffiliateProgramCreate(
            name="Rimo Voice",
            provider="a8",
            commission_type="fixed",
            commission_value=3000,
            currency="jpy",
            match_terms=["  議事録 ", "議事録", "AI 議事録"],
        )
    )
    assert created.id > 0
    assert created.currency == "JPY"
    assert created.match_terms == ["議事録", "AI 議事録"]
    assert created.status is AffiliateProgramStatus.ACTIVE

    fetched = svc.get_program(created.id)
    assert fetched.name == "Rimo Voice"


def test_create_persists_committed(session: Session) -> None:
    svc = _svc(session)
    created = svc.create_program(AffiliateProgramCreate(name="Committed"))
    session.rollback()  # commit 済みなら残る
    assert AffiliateProgramRepository(session).get_by_id(created.id) is not None


def test_create_duplicate_name_provider_raises(session: Session) -> None:
    svc = _svc(session)
    svc.create_program(AffiliateProgramCreate(name="Dup", provider="a8"))

    with pytest.raises(DuplicateEntityError):
        svc.create_program(AffiliateProgramCreate(name="Dup", provider="a8"))

    # provider が違えば別案件として作れる
    other = svc.create_program(AffiliateProgramCreate(name="Dup", provider="moshimo"))
    assert other.id > 0


def test_get_not_found(session: Session) -> None:
    with pytest.raises(EntityNotFoundError):
        _svc(session).get_program(999999)


def test_list_with_filters(session: Session) -> None:
    svc = _svc(session)
    svc.create_program(AffiliateProgramCreate(name="A", provider="a8", category="ai"))
    svc.create_program(
        AffiliateProgramCreate(name="B", provider="a8", category="ai", status="paused")
    )
    svc.create_program(AffiliateProgramCreate(name="C", provider="moshimo", category="ai"))

    assert {p.name for p in svc.list_programs()} == {"A", "B", "C"}
    assert {p.name for p in svc.list_programs(provider="a8")} == {"A", "B"}
    assert {
        p.name for p in svc.list_programs(status=AffiliateProgramStatus.ACTIVE)
    } == {"A", "C"}


def test_update_normalizes_currency_and_match_terms(session: Session) -> None:
    svc = _svc(session)
    created = svc.create_program(AffiliateProgramCreate(name="U"))

    updated = svc.update_program(
        created.id,
        AffiliateProgramUpdate(
            currency="usd",
            match_terms=[" x ", "x", "y"],
            status="ended",
        ),
    )
    assert updated.currency == "USD"
    assert updated.match_terms == ["x", "y"]
    assert updated.status is AffiliateProgramStatus.ENDED

    session.expire_all()
    reloaded = svc.get_program(created.id)
    assert reloaded.currency == "USD"
    assert reloaded.status is AffiliateProgramStatus.ENDED


def test_update_not_found(session: Session) -> None:
    with pytest.raises(EntityNotFoundError):
        _svc(session).update_program(
            999999, AffiliateProgramUpdate(category="x")
        )


def test_update_noop_when_empty_payload(session: Session) -> None:
    svc = _svc(session)
    created = svc.create_program(AffiliateProgramCreate(name="NoOp"))
    result = svc.update_program(created.id, AffiliateProgramUpdate())
    assert result.name == "NoOp"


def test_delete(session: Session) -> None:
    svc = _svc(session)
    created = svc.create_program(AffiliateProgramCreate(name="Del"))
    svc.delete_program(created.id)
    with pytest.raises(EntityNotFoundError):
        svc.get_program(created.id)


def test_delete_not_found(session: Session) -> None:
    with pytest.raises(EntityNotFoundError):
        _svc(session).delete_program(999999)


def test_commit_failure_rolls_back(session: Session, monkeypatch) -> None:
    svc = _svc(session)

    def _boom() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(session, "commit", _boom)
    with pytest.raises(RuntimeError):
        svc.create_program(AffiliateProgramCreate(name="RollbackMe"))

    monkeypatch.undo()
    assert AffiliateProgramRepository(session).list() == []
