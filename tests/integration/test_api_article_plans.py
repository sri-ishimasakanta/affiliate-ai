"""Article Plan API (GET plan / POST approve) の検証。"""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models import Keyword
from app.models.enums import AffiliateProgramStatus
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.repositories.keyword_signal_repository import KeywordSignalRepository

_ALL7 = (
    "search_demand", "commercial_intent", "affiliate_opportunity",
    "competition_ease", "trend", "originality", "site_relevance",
)


def _kw(session: Session) -> Keyword:
    k = Keyword(keyword="業務効率化 ツール おすすめ")
    k.status = "analyzed"
    k.opportunity_score = 68.81
    session.add(k)
    session.flush()
    session.commit()
    return k


def _sig(session: Session, kid: int, comp: str, val: float, raw: dict | None = None):
    KeywordSignalRepository(session).create(
        keyword_id=kid, component=comp, normalized_value=val, provider="test",
        observed_at=datetime(2026, 1, 1, tzinfo=UTC), raw_data=raw or {},
        source_reference="test",
    )
    session.commit()


def _catalog(session: Session) -> dict[str, int]:
    repo = AffiliateProgramRepository(session)
    return {
        "Make": repo.create(
            name="Make", provider="direct", commission_type="percentage",
            commission_value=35.0, match_terms=["業務効率化"],
            status=AffiliateProgramStatus.ACTIVE).id,
        "ClickUp": repo.create(
            name="ClickUp", provider="direct", match_terms=["業務効率化"],
            status=AffiliateProgramStatus.ACTIVE).id,
    }


def _complete(session: Session, originality: float = 27.27) -> tuple[Keyword, dict]:
    ids = _catalog(session)
    k = _kw(session)
    _sig(session, k.id, "search_demand", 30.0)
    _sig(session, k.id, "commercial_intent", 80.0)
    _sig(session, k.id, "trend", 77.0)
    _sig(session, k.id, "site_relevance", 80.0)
    _sig(session, k.id, "competition_ease", 100.0)
    _sig(session, k.id, "affiliate_opportunity", 86.0,
         {"matched_program_ids": [ids["Make"], ids["ClickUp"]]})
    _sig(session, k.id, "originality", originality,
         {"corpus_available": True, "most_similar_keyword_text": "業務効率化 ツール 無料"})
    return k, ids


def test_get_plan_200(api_client, session: Session) -> None:
    k, _ids = _complete(session)
    r = api_client.get(f"/api/v1/keywords/{k.id}/article-plan")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["article_type"] == "recommendation_roundup"
    assert body["readiness"]["complete"] is True
    assert body["proposed_slug"] == "業務効率化-ツール-おすすめ-roundup"
    assert [c["name"] for c in body["affiliate_candidates"]] == ["Make", "ClickUp"]
    assert body["cannibalization"]["acknowledgment_required"] is True
    assert "tracking" not in r.text.lower()


def test_get_plan_keyword_not_found_404(api_client) -> None:
    r = api_client.get("/api/v1/keywords/999999/article-plan")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "entity_not_found"


def test_get_plan_incomplete_still_200(api_client, session: Session) -> None:
    _catalog(session)
    k = _kw(session)
    _sig(session, k.id, "search_demand", 30.0)
    r = api_client.get(f"/api/v1/keywords/{k.id}/article-plan")
    assert r.status_code == 200
    assert r.json()["readiness"]["complete"] is False
    assert "competition_ease" in r.json()["readiness"]["missing_components"]


def test_approve_201(api_client, session: Session) -> None:
    k, ids = _complete(session)
    r = api_client.post(
        f"/api/v1/keywords/{k.id}/article-plan/approve",
        json={
            "title": "業務効率化ツールおすすめ",
            "slug": "gyoumu-osusume",
            "primary_affiliate_program_id": ids["Make"],
            "secondary_affiliate_program_ids": [ids["ClickUp"]],
            "acknowledge_cannibalization": True,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "planned"
    assert body["keyword_id"] == k.id
    art_id = body["id"]

    links = api_client.get(f"/api/v1/articles/{art_id}/affiliate-programs").json()
    assert {x["affiliate_program_id"] for x in links} == {ids["Make"], ids["ClickUp"]}
    assert sum(1 for x in links if x["is_primary"]) == 1


def test_approve_cannibalization_gate_409(api_client, session: Session) -> None:
    k, _ids = _complete(session)
    r = api_client.post(
        f"/api/v1/keywords/{k.id}/article-plan/approve",
        json={"title": "t", "slug": "s"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "plan_approval_rejected"


def test_approve_incomplete_gate_409(api_client, session: Session) -> None:
    _catalog(session)
    k = _kw(session)
    _sig(session, k.id, "search_demand", 30.0)
    r = api_client.post(
        f"/api/v1/keywords/{k.id}/article-plan/approve",
        json={"title": "t", "slug": "s"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "plan_approval_rejected"


def test_approve_double_409(api_client, session: Session) -> None:
    k, _ids = _complete(session)
    payload = {"title": "t", "slug": "s1", "acknowledge_cannibalization": True}
    assert api_client.post(
        f"/api/v1/keywords/{k.id}/article-plan/approve", json=payload
    ).status_code == 201
    r = api_client.post(
        f"/api/v1/keywords/{k.id}/article-plan/approve",
        json={"title": "t", "slug": "s2", "acknowledge_cannibalization": True},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "duplicate_entity"


def test_approve_invalid_affiliate_candidate_409(api_client, session: Session) -> None:
    k, _ids = _complete(session)
    r = api_client.post(
        f"/api/v1/keywords/{k.id}/article-plan/approve",
        json={
            "title": "t", "slug": "s",
            "primary_affiliate_program_id": 987654,
            "acknowledge_cannibalization": True,
        },
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "plan_approval_rejected"


def test_approve_extra_field_422(api_client, session: Session) -> None:
    k, _ids = _complete(session)
    r = api_client.post(
        f"/api/v1/keywords/{k.id}/article-plan/approve",
        json={"title": "t", "slug": "s", "unexpected": 1},
    )
    assert r.status_code == 422
