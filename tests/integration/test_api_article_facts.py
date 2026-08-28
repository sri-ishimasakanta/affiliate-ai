"""Source / ArticleFact / FactPack API の検証。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models import Article, ArticleAffiliateProgram
from app.models.enums import AffiliateProgramStatus
from app.repositories.affiliate_program_repository import AffiliateProgramRepository

NOW = datetime.now(UTC)
FRESH = (NOW - timedelta(days=3)).isoformat()

_REQUIRED = {
    "official_product_name": ("verified", "Make"),
    "official_url": ("verified", "https://www.make.com/"),
    "primary_use_cases": ("verified", ["自動化"]),
    "key_features": ("verified", ["シナリオ", "連携"]),
    "pricing_summary": ("verified", "Freeプランあり"),
    "free_plan_available": ("verified", True),
}


def _article(session: Session) -> Article:
    a = Article(title="t", slug="a", keyword_id=None)
    session.add(a)
    session.flush()
    session.commit()
    return a


def _link(session: Session, article_id: int, name: str) -> int:
    p = AffiliateProgramRepository(session).create(
        name=name, provider="direct", status=AffiliateProgramStatus.ACTIVE
    )
    session.add(ArticleAffiliateProgram(article_id=article_id, affiliate_program_id=p.id))
    session.commit()
    return p.id


def _post_source(client, article_id: int, **over):
    body = dict(
        source_type="official_pricing",
        source_url="https://www.make.com/en/pricing",
        title="Make Pricing",
        checked_at=FRESH,
    )
    body.update(over)
    return client.post(f"/api/v1/articles/{article_id}/sources", json=body)


# -- source API -------------------------------------------------
def test_source_crud(api_client, session: Session) -> None:
    art = _article(session)
    r = _post_source(api_client, art.id)
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert r.json()["source_url"] == "https://www.make.com/en/pricing"

    assert api_client.get(f"/api/v1/articles/{art.id}/sources").json()[0]["id"] == sid
    assert api_client.get(f"/api/v1/articles/{art.id}/sources/{sid}").status_code == 200

    r = api_client.delete(f"/api/v1/articles/{art.id}/sources/{sid}")
    assert r.status_code == 204
    assert api_client.get(f"/api/v1/articles/{art.id}/sources").json() == []


def test_source_unsafe_url_422(api_client, session: Session) -> None:
    art = _article(session)
    r = _post_source(api_client, art.id, source_url="http://make.com/pricing")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "fact_validation_error"


def test_source_article_not_found_404(api_client) -> None:
    r = _post_source(api_client, 99999)
    assert r.status_code == 404


# -- fact API + fact-pack ------------------------------------
def test_fact_post_get_and_history(api_client, session: Session) -> None:
    art = _article(session)
    sid = _post_source(api_client, art.id).json()["id"]

    r = api_client.post(
        f"/api/v1/articles/{art.id}/facts",
        json={
            "subject_ref": "Make",
            "fact_key": "key_features",
            "fact_value": ["a", "b"],
            "value_status": "verified",
            "source_id": sid,
            "checked_at": FRESH,
        },
    )
    assert r.status_code == 201, r.text
    fid = r.json()["id"]
    assert api_client.get(f"/api/v1/articles/{art.id}/facts/{fid}").json()["fact_value"] == [
        "a",
        "b",
    ]
    later = (NOW - timedelta(days=1)).isoformat()
    api_client.post(
        f"/api/v1/articles/{art.id}/facts",
        json={
            "subject_ref": "Make", "fact_key": "key_features",
            "fact_value": ["a", "b", "c"], "value_status": "verified",
            "source_id": sid, "checked_at": later,
        },
    )
    hist = api_client.get(
        f"/api/v1/articles/{art.id}/facts?subject_ref=Make&fact_key=key_features"
    ).json()
    assert len(hist) == 2
    latest = api_client.get(
        f"/api/v1/articles/{art.id}/facts?fact_key=key_features&latest=true"
    ).json()
    assert len(latest) == 1 and latest[0]["fact_value"] == ["a", "b", "c"]


def test_fact_validation_error_422(api_client, session: Session) -> None:
    art = _article(session)
    r = api_client.post(
        f"/api/v1/articles/{art.id}/facts",
        json={
            "subject_ref": "Make", "fact_key": "official_url",
            "fact_value": "https://x/", "value_status": "verified",
            "checked_at": FRESH,  # source なし -> verified 不可
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "fact_validation_error"


def test_source_delete_referenced_409(api_client, session: Session) -> None:
    art = _article(session)
    sid = _post_source(api_client, art.id).json()["id"]
    api_client.post(
        f"/api/v1/articles/{art.id}/facts",
        json={
            "subject_ref": "Make", "fact_key": "official_url",
            "fact_value": "https://x/", "value_status": "verified",
            "source_id": sid, "checked_at": FRESH,
        },
    )
    r = api_client.delete(f"/api/v1/articles/{art.id}/sources/{sid}")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "entity_in_use"


def test_fact_pack_endpoint(api_client, session: Session) -> None:
    art = _article(session)
    _link(session, art.id, "Make")
    sid = _post_source(api_client, art.id).json()["id"]
    for key, (status, value) in _REQUIRED.items():
        api_client.post(
            f"/api/v1/articles/{art.id}/facts",
            json={
                "subject_ref": "Make", "fact_key": key, "fact_value": value,
                "value_status": status, "source_id": sid, "checked_at": FRESH,
            },
        )
    r = api_client.get(f"/api/v1/articles/{art.id}/fact-pack")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["readiness"]["drafting_allowed"] is True
    assert body["tool_facts"][0]["subject_ref"] == "Make"
    assert "official_url" in body["tool_facts"][0]["usable_claims"]
    assert "tracking" not in r.text.lower()


def test_fact_pack_not_found_404(api_client) -> None:
    r = api_client.get("/api/v1/articles/99999/fact-pack")
    assert r.status_code == 404
