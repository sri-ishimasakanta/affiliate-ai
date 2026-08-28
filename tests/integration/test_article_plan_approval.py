"""ArticlePlanService.approve の検証: atomic / gate / rollback (in-memory DB)。"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.article.schemas import ArticlePlanApproveRequest
from app.exceptions import DuplicateEntityError, PlanApprovalError
from app.models import (
    AffiliateProgram,
    Article,
    ArticleAffiliateProgram,
    Keyword,
    KeywordSignal,
)
from app.models.enums import AffiliateProgramStatus, ArticleStatus
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.repositories.article_affiliate_program_repository import (
    ArticleAffiliateProgramRepository,
)
from app.repositories.keyword_signal_repository import KeywordSignalRepository
from app.services.article_plan_service import ArticlePlanService

_ALL7 = (
    "search_demand", "commercial_intent", "affiliate_opportunity",
    "competition_ease", "trend", "originality", "site_relevance",
)


def _keyword(session: Session, text: str = "業務効率化 ツール おすすめ") -> Keyword:
    k = Keyword(keyword=text)
    k.status = "analyzed"
    k.opportunity_score = 68.81
    session.add(k)
    session.flush()
    session.commit()
    return k


def _signal(session: Session, kid: int, component: str, value: float,
            *, raw: dict | None = None) -> None:
    KeywordSignalRepository(session).create(
        keyword_id=kid, component=component, normalized_value=value,
        provider="test", observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        raw_data=raw or {}, source_reference="test",
    )
    session.commit()


def _catalog(session: Session) -> dict[str, int]:
    repo = AffiliateProgramRepository(session)
    return {
        "Make": repo.create(
            name="Make", provider="direct", commission_type="percentage",
            commission_value=35.0, match_terms=["業務効率化"],
            status=AffiliateProgramStatus.ACTIVE).id,
        "HubSpot": repo.create(
            name="HubSpot", provider="Impact", commission_type="percentage",
            commission_value=30.0, match_terms=["業務効率化"],
            status=AffiliateProgramStatus.ACTIVE).id,
        "ClickUp": repo.create(
            name="ClickUp", provider="direct", match_terms=["業務効率化"],
            status=AffiliateProgramStatus.ACTIVE).id,
        "Paused": repo.create(
            name="Paused", provider="direct", match_terms=["業務効率化"],
            status=AffiliateProgramStatus.PAUSED).id,
    }


def _complete(session: Session, originality: float = 27.27) -> tuple[Keyword, dict]:
    ids = _catalog(session)
    k = _keyword(session)
    _signal(session, k.id, "search_demand", 30.0)
    _signal(session, k.id, "commercial_intent", 80.0)
    _signal(session, k.id, "trend", 77.0)
    _signal(session, k.id, "site_relevance", 80.0)
    _signal(session, k.id, "competition_ease", 100.0)
    _signal(session, k.id, "affiliate_opportunity", 86.0,
            raw={"matched_program_ids": [ids["Make"], ids["HubSpot"], ids["ClickUp"]]})
    _signal(session, k.id, "originality", originality,
            raw={"corpus_available": True, "most_similar_keyword_text": "業務効率化 ツール 無料"})
    return k, ids


def _req(**over) -> ArticlePlanApproveRequest:
    base = dict(
        title="業務効率化ツールおすすめ7選",
        slug="gyoumu-tool-osusume",
        primary_affiliate_program_id=None,
        secondary_affiliate_program_ids=[],
        acknowledge_cannibalization=False,
        acknowledge_incomplete_plan=False,
    )
    base.update(over)
    return ArticlePlanApproveRequest(**base)


# -- happy path ----------------------------------------------------
def test_approve_success_creates_planned_article_and_links(session: Session) -> None:
    k, ids = _complete(session)
    svc = ArticlePlanService(session)
    read = svc.approve(
        k.id,
        _req(
            primary_affiliate_program_id=ids["Make"],
            secondary_affiliate_program_ids=[ids["HubSpot"], ids["ClickUp"]],
            acknowledge_cannibalization=True,
        ),
    )
    assert read.status is ArticleStatus.PLANNED
    assert read.keyword_id == k.id
    assert read.draft_content is None
    assert read.slug == "gyoumu-tool-osusume"

    links = ArticleAffiliateProgramRepository(session).list_by_article(read.id)
    assert {x.affiliate_program_id for x in links} == {
        ids["Make"], ids["HubSpot"], ids["ClickUp"]
    }
    primary = [x for x in links if x.is_primary]
    assert len(primary) == 1 and primary[0].affiliate_program_id == ids["Make"]


def test_approve_without_affiliates_is_allowed(session: Session) -> None:
    k, _ids = _complete(session)
    read = ArticlePlanService(session).approve(
        k.id, _req(acknowledge_cannibalization=True)
    )
    assert read.status is ArticleStatus.PLANNED
    assert ArticleAffiliateProgramRepository(session).list_by_article(read.id) == []


# -- cannibalization gate ---------------------------------------
def test_cannibalization_gate_rejects_without_ack(session: Session) -> None:
    k, _ids = _complete(session, originality=27.27)
    with pytest.raises(PlanApprovalError, match="originality below threshold"):
        ArticlePlanService(session).approve(k.id, _req())
    assert session.scalar(select(func.count()).select_from(Article)) == 0


def test_cannibalization_gate_passes_with_ack(session: Session) -> None:
    k, _ids = _complete(session, originality=27.27)
    read = ArticlePlanService(session).approve(
        k.id, _req(acknowledge_cannibalization=True)
    )
    assert read.status is ArticleStatus.PLANNED


def test_cannibalization_gate_not_triggered_when_originality_high(session: Session) -> None:
    k, _ids = _complete(session, originality=80.0)
    read = ArticlePlanService(session).approve(k.id, _req())  # ack 不要
    assert read.status is ArticleStatus.PLANNED


# -- incomplete plan gate -------------------------------------
def test_incomplete_plan_rejected_by_default(session: Session) -> None:
    _catalog(session)
    k = _keyword(session)
    _signal(session, k.id, "search_demand", 30.0)
    with pytest.raises(PlanApprovalError, match="incomplete"):
        ArticlePlanService(session).approve(k.id, _req())
    assert session.scalar(select(func.count()).select_from(Article)) == 0


def test_incomplete_plan_allowed_with_explicit_override(session: Session) -> None:
    _catalog(session)
    k = _keyword(session)
    _signal(session, k.id, "search_demand", 30.0)
    read = ArticlePlanService(session).approve(
        k.id, _req(acknowledge_incomplete_plan=True)
    )
    assert read.status is ArticleStatus.PLANNED


# -- slug / duplicate ----------------------------------------
def test_slug_duplicate_rejected(session: Session) -> None:
    k, _ids = _complete(session)
    session.add(Article(title="x", slug="gyoumu-tool-osusume", keyword_id=None))
    session.commit()
    with pytest.raises(DuplicateEntityError, match="slug"):
        ArticlePlanService(session).approve(k.id, _req(acknowledge_cannibalization=True))
    assert session.scalar(
        select(func.count()).select_from(Article)
    ) == 1  # 既存の 1 件のみ


def test_double_approval_rejected(session: Session) -> None:
    k, _ids = _complete(session)
    svc = ArticlePlanService(session)
    svc.approve(k.id, _req(acknowledge_cannibalization=True))
    with pytest.raises(DuplicateEntityError, match="keyword_id"):
        svc.approve(
            k.id, _req(slug="another-slug", acknowledge_cannibalization=True)
        )
    assert session.scalar(select(func.count()).select_from(Article)) == 1


def test_archived_article_does_not_block_reapproval(session: Session) -> None:
    k, _ids = _complete(session)
    session.add(
        Article(title="old", slug="old", keyword_id=k.id, status=ArticleStatus.ARCHIVED)
    )
    session.commit()
    read = ArticlePlanService(session).approve(
        k.id, _req(acknowledge_cannibalization=True)
    )
    assert read.status is ArticleStatus.PLANNED
    assert session.scalar(select(func.count()).select_from(Article)) == 2


# -- affiliate validation ----------------------------------
def test_reject_primary_not_in_candidates(session: Session) -> None:
    k, ids = _complete(session)
    with pytest.raises(PlanApprovalError, match="not an active matched candidate"):
        ArticlePlanService(session).approve(
            k.id,
            _req(primary_affiliate_program_id=ids["Paused"],
                 acknowledge_cannibalization=True),
        )
    assert session.scalar(select(func.count()).select_from(Article)) == 0


def test_reject_secondary_duplicate_ids(session: Session) -> None:
    k, ids = _complete(session)
    with pytest.raises(PlanApprovalError, match="duplicates"):
        ArticlePlanService(session).approve(
            k.id,
            _req(secondary_affiliate_program_ids=[ids["Make"], ids["Make"]],
                 acknowledge_cannibalization=True),
        )


def test_reject_primary_also_in_secondary(session: Session) -> None:
    k, ids = _complete(session)
    with pytest.raises(PlanApprovalError, match="must not also appear"):
        ArticlePlanService(session).approve(
            k.id,
            _req(primary_affiliate_program_id=ids["Make"],
                 secondary_affiliate_program_ids=[ids["Make"]],
                 acknowledge_cannibalization=True),
        )


def test_reject_unmatched_secondary(session: Session) -> None:
    k, ids = _complete(session)
    with pytest.raises(PlanApprovalError, match="not active matched"):
        ArticlePlanService(session).approve(
            k.id,
            _req(secondary_affiliate_program_ids=[999999],
                 acknowledge_cannibalization=True),
        )


# -- atomicity -----------------------------------------------
def test_rollback_leaves_no_partial_state(session: Session, monkeypatch) -> None:
    k, ids = _complete(session)
    svc = ArticlePlanService(session)

    real_create = svc._links.create
    calls = {"n": 0}

    def _boom(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:  # primary は作れて 1 本目の secondary で失敗
            raise RuntimeError("boom")
        return real_create(*args, **kwargs)

    monkeypatch.setattr(svc._links, "create", _boom)
    with pytest.raises(RuntimeError):
        svc.approve(
            k.id,
            _req(
                primary_affiliate_program_id=ids["Make"],
                secondary_affiliate_program_ids=[ids["HubSpot"], ids["ClickUp"]],
                acknowledge_cannibalization=True,
            ),
        )
    session.rollback()
    assert session.scalar(select(func.count()).select_from(Article)) == 0
    assert session.scalar(
        select(func.count()).select_from(ArticleAffiliateProgram)
    ) == 0
    assert session.scalar(select(func.count()).select_from(KeywordSignal)) == 7
    assert session.scalar(select(func.count()).select_from(AffiliateProgram)) == 4
