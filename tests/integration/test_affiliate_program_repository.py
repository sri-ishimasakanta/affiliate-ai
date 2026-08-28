"""AffiliateProgramRepository の検証 (DB アクセスのみ、commit しない)。"""

from sqlalchemy.orm import Session

from app.models import AffiliateProgram
from app.models.enums import AffiliateProgramStatus
from app.repositories.affiliate_program_repository import AffiliateProgramRepository


def _repo(session: Session) -> AffiliateProgramRepository:
    return AffiliateProgramRepository(session)


def test_create_flushes_without_commit(session: Session) -> None:
    repo = _repo(session)
    entity = repo.create(
        name="Prog A",
        provider="a8",
        category="ai",
        commission_type="fixed",
        commission_value=3000,
        currency="JPY",
        match_terms=["議事録", "AI 議事録"],
    )
    assert entity.id is not None  # flush で採番済み

    session.rollback()  # commit していないので消える
    assert repo.get_by_id(entity.id) is None


def test_get_by_id(session: Session) -> None:
    repo = _repo(session)
    entity = repo.create(name="Prog B")
    session.commit()
    assert repo.get_by_id(entity.id).name == "Prog B"
    assert repo.get_by_id(999999) is None


def test_get_by_name_and_provider(session: Session) -> None:
    repo = _repo(session)
    repo.create(name="Dup", provider="a8")
    repo.create(name="Dup", provider="moshimo")
    repo.create(name="NoProvider", provider=None)
    session.commit()

    assert repo.get_by_name_and_provider("Dup", "a8").provider == "a8"
    assert repo.get_by_name_and_provider("Dup", "rakuten") is None
    assert repo.get_by_name_and_provider("NoProvider", None).name == "NoProvider"
    assert repo.get_by_name_and_provider("NoProvider", "a8") is None


def test_list_and_filters(session: Session) -> None:
    repo = _repo(session)
    repo.create(name="P1", provider="a8", category="ai", status=AffiliateProgramStatus.ACTIVE)
    repo.create(name="P2", provider="a8", category="finance", status=AffiliateProgramStatus.PAUSED)
    repo.create(name="P3", provider="moshimo", category="ai", status=AffiliateProgramStatus.ACTIVE)
    session.commit()

    assert {p.name for p in repo.list()} == {"P1", "P2", "P3"}
    assert {p.name for p in repo.list(provider="a8")} == {"P1", "P2"}
    assert {p.name for p in repo.list(category="ai")} == {"P1", "P3"}
    assert {p.name for p in repo.list(status=AffiliateProgramStatus.ACTIVE)} == {"P1", "P3"}
    assert {p.name for p in repo.list(status="paused")} == {"P2"}
    assert {p.name for p in repo.list_active()} == {"P1", "P3"}


def test_list_pagination_ordered_by_id(session: Session) -> None:
    repo = _repo(session)
    for i in range(5):
        repo.create(name=f"Page{i}")
    session.commit()
    page = repo.list(limit=2, offset=2)
    assert [p.name for p in page] == ["Page2", "Page3"]


def test_update(session: Session) -> None:
    repo = _repo(session)
    entity = repo.create(name="Old", match_terms=["a"])
    session.commit()

    repo.update(entity, {"name": "New", "match_terms": ["b", "c"], "currency": "USD"})
    session.commit()
    session.expire_all()

    reloaded = repo.get_by_id(entity.id)
    assert reloaded.name == "New"
    assert reloaded.match_terms == ["b", "c"]
    assert reloaded.currency == "USD"


def test_delete(session: Session) -> None:
    repo = _repo(session)
    entity = repo.create(name="Gone")
    session.commit()
    program_id = entity.id

    repo.delete(entity)
    session.commit()
    assert repo.get_by_id(program_id) is None


def test_match_terms_roundtrip_json(session: Session) -> None:
    repo = _repo(session)
    terms = ["議事録", "AI 議事録", "文字起こし"]
    entity = repo.create(name="J", match_terms=terms)
    session.commit()
    session.expire_all()
    assert session.get(AffiliateProgram, entity.id).match_terms == terms
