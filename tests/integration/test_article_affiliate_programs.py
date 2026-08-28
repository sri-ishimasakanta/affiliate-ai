"""ArticleAffiliateProgramService / API の検証 (in-memory DB)。"""

import pytest
from sqlalchemy.orm import Session

from app.article.schemas import (
    ArticleAffiliateProgramCreate,
    ArticleAffiliateProgramUpdate,
)
from app.exceptions import DuplicateEntityError, EntityNotFoundError
from app.models import AffiliateProgram, Article
from app.models.enums import AffiliateProgramStatus
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.repositories.article_affiliate_program_repository import (
    ArticleAffiliateProgramRepository,
)
from app.services.article_affiliate_program_service import (
    ArticleAffiliateProgramService,
)


def _article(session: Session, slug: str = "a") -> Article:
    entity = Article(title="t", slug=slug, keyword_id=None)
    session.add(entity)
    session.flush()
    session.commit()
    return entity


def _program(session: Session, name: str, status: str = "active") -> AffiliateProgram:
    return AffiliateProgramRepository(session).create(
        name=name, provider="direct", status=status
    )


# -- service ---------------------------------------------------------
def test_attach_list_detach(session: Session) -> None:
    art = _article(session)
    p1 = _program(session, "P1")
    p2 = _program(session, "P2")
    svc = ArticleAffiliateProgramService(session)

    l1 = svc.attach(art.id, ArticleAffiliateProgramCreate(affiliate_program_id=p1.id))
    l2 = svc.attach(
        art.id,
        ArticleAffiliateProgramCreate(affiliate_program_id=p2.id, is_primary=True),
    )
    listed = svc.list_by_article(art.id)
    assert {x.affiliate_program_id for x in listed} == {p1.id, p2.id}
    assert l2.is_primary is True and l1.is_primary is False

    svc.detach(art.id, l1.id)
    assert [x.id for x in svc.list_by_article(art.id)] == [l2.id]


def test_attach_duplicate_rejected(session: Session) -> None:
    art = _article(session)
    p = _program(session, "P")
    svc = ArticleAffiliateProgramService(session)
    svc.attach(art.id, ArticleAffiliateProgramCreate(affiliate_program_id=p.id))
    with pytest.raises(DuplicateEntityError):
        svc.attach(art.id, ArticleAffiliateProgramCreate(affiliate_program_id=p.id))


def test_one_primary_rule_on_attach(session: Session) -> None:
    art = _article(session)
    pa, pb = _program(session, "A"), _program(session, "B")
    svc = ArticleAffiliateProgramService(session)
    la = svc.attach(
        art.id, ArticleAffiliateProgramCreate(affiliate_program_id=pa.id, is_primary=True)
    )
    lb = svc.attach(
        art.id, ArticleAffiliateProgramCreate(affiliate_program_id=pb.id, is_primary=True)
    )
    session.expire_all()
    repo = ArticleAffiliateProgramRepository(session)
    assert repo.get_by_id(la.id).is_primary is False
    assert repo.get_by_id(lb.id).is_primary is True
    assert repo.get_primary(art.id).id == lb.id


def test_set_primary_demotes_previous(session: Session) -> None:
    art = _article(session)
    pa, pb = _program(session, "A"), _program(session, "B")
    svc = ArticleAffiliateProgramService(session)
    la = svc.attach(
        art.id, ArticleAffiliateProgramCreate(affiliate_program_id=pa.id, is_primary=True)
    )
    lb = svc.attach(art.id, ArticleAffiliateProgramCreate(affiliate_program_id=pb.id))

    svc.set_primary(art.id, lb.id)
    session.expire_all()
    repo = ArticleAffiliateProgramRepository(session)
    assert repo.get_by_id(la.id).is_primary is False
    assert repo.get_by_id(lb.id).is_primary is True


def test_update_link_toggle_primary(session: Session) -> None:
    art = _article(session)
    pa, pb = _program(session, "A"), _program(session, "B")
    svc = ArticleAffiliateProgramService(session)
    la = svc.attach(
        art.id, ArticleAffiliateProgramCreate(affiliate_program_id=pa.id, is_primary=True)
    )
    lb = svc.attach(art.id, ArticleAffiliateProgramCreate(affiliate_program_id=pb.id))
    svc.update_link(art.id, lb.id, ArticleAffiliateProgramUpdate(is_primary=True))
    session.expire_all()
    repo = ArticleAffiliateProgramRepository(session)
    assert repo.get_by_id(la.id).is_primary is False
    assert repo.get_by_id(lb.id).is_primary is True


def test_article_not_found(session: Session) -> None:
    p = _program(session, "P")
    with pytest.raises(EntityNotFoundError):
        ArticleAffiliateProgramService(session).attach(
            999, ArticleAffiliateProgramCreate(affiliate_program_id=p.id)
        )


def test_program_not_found(session: Session) -> None:
    art = _article(session)
    with pytest.raises(EntityNotFoundError):
        ArticleAffiliateProgramService(session).attach(
            art.id, ArticleAffiliateProgramCreate(affiliate_program_id=999)
        )


def test_detach_link_of_other_article_rejected(session: Session) -> None:
    a1, a2 = _article(session, "a1"), _article(session, "a2")
    p = _program(session, "P")
    svc = ArticleAffiliateProgramService(session)
    link = svc.attach(a1.id, ArticleAffiliateProgramCreate(affiliate_program_id=p.id))
    with pytest.raises(EntityNotFoundError):
        svc.detach(a2.id, link.id)


def test_paused_program_can_still_be_attached_directly(session: Session) -> None:
    # 中間モデル操作は status を問わない (approval 側で active 限定を強制する)。
    art = _article(session)
    paused = _program(session, "Paused", status=AffiliateProgramStatus.PAUSED)
    link = ArticleAffiliateProgramService(session).attach(
        art.id, ArticleAffiliateProgramCreate(affiliate_program_id=paused.id)
    )
    assert link.affiliate_program_id == paused.id


# -- API -----------------------------------------------------------
def test_affiliate_program_api_crud(api_client, session: Session) -> None:
    art = _article(session)
    p1, p2 = _program(session, "P1"), _program(session, "P2")

    r = api_client.post(
        f"/api/v1/articles/{art.id}/affiliate-programs",
        json={"affiliate_program_id": p1.id, "is_primary": True},
    )
    assert r.status_code == 201, r.text
    link1 = r.json()["id"]

    r = api_client.post(
        f"/api/v1/articles/{art.id}/affiliate-programs",
        json={"affiliate_program_id": p2.id},
    )
    assert r.status_code == 201
    link2 = r.json()["id"]

    r = api_client.get(f"/api/v1/articles/{art.id}/affiliate-programs")
    assert r.status_code == 200 and len(r.json()) == 2

    # duplicate -> 409
    r = api_client.post(
        f"/api/v1/articles/{art.id}/affiliate-programs",
        json={"affiliate_program_id": p1.id},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "duplicate_entity"

    # promote link2 -> demotes link1
    r = api_client.patch(
        f"/api/v1/articles/{art.id}/affiliate-programs/{link2}",
        json={"is_primary": True},
    )
    assert r.status_code == 200
    listed = {x["id"]: x["is_primary"] for x in
              api_client.get(f"/api/v1/articles/{art.id}/affiliate-programs").json()}
    assert listed[link1] is False and listed[link2] is True

    r = api_client.delete(f"/api/v1/articles/{art.id}/affiliate-programs/{link1}")
    assert r.status_code == 204
    assert len(api_client.get(f"/api/v1/articles/{art.id}/affiliate-programs").json()) == 1

    # unknown article -> 404
    r = api_client.get("/api/v1/articles/98765/affiliate-programs")
    assert r.status_code == 404
