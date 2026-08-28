"""affiliate_opportunity 導出 API の統合テスト (外部通信なし)。"""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.enums import AffiliateProgramStatus
from app.repositories.affiliate_program_repository import AffiliateProgramRepository

_URL = "/api/v1/keywords/{kid}/signals/affiliate-opportunity"


def _assert_error_shape(body: dict, code: str) -> None:
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    assert body["error"]["message"]


def _new_keyword(client: TestClient, keyword: str = "AI 議事録 おすすめ") -> int:
    resp = client.post("/api/v1/keywords", json={"keyword": keyword})
    assert resp.status_code == 201
    return resp.json()["id"]


def _seed(session: Session) -> None:
    repo = AffiliateProgramRepository(session)
    repo.create(
        name="Meeting AI B",
        provider="direct",
        commission_type="percentage",
        commission_value=30,
        match_terms=["議事録", "AI 議事録"],
        tracking_url="https://aff.example.test/r?token=SUPER_SECRET_TRACK_ID",
        status=AffiliateProgramStatus.ACTIVE,
    )
    repo.create(
        name="Meeting AI C",
        provider="Impact",
        commission_type="percentage",
        commission_value=10,
        match_terms=["議事録"],
        status=AffiliateProgramStatus.ACTIVE,
    )
    session.commit()


def test_derive_returns_201_and_body(api_client: TestClient, session: Session) -> None:
    _seed(session)
    keyword_id = _new_keyword(api_client)

    resp = api_client.post(_URL.format(kid=keyword_id))

    assert resp.status_code == 201
    body = resp.json()
    assert body["keyword_id"] == keyword_id
    assert body["component"] == "affiliate_opportunity"
    assert body["provider"] == "affiliate_catalog"
    assert 0.0 <= body["normalized_value"] <= 100.0
    raw = body["raw_data"]
    assert raw["matched_program_count"] == 2
    assert raw["commission_score"] == 75.0
    assert raw["market_evidence_available"] is True
    assert raw["normalizer"] == {"name": "affiliate_opportunity", "version": "v1"}
    assert body["source_reference"] == "affiliate-catalog:local:v1"
    # secret は raw_data に出ない
    assert "SUPER_SECRET_TRACK_ID" not in resp.text
    assert "tracking_url" not in resp.text


def test_zero_match_returns_201_with_value_zero(
    api_client: TestClient, session: Session
) -> None:
    _seed(session)
    keyword_id = _new_keyword(api_client, "ChatGPT 料金")

    resp = api_client.post(_URL.format(kid=keyword_id))

    assert resp.status_code == 201
    body = resp.json()
    assert body["normalized_value"] == 0.0
    assert body["raw_data"]["market_evidence_available"] is False


def test_persisted_listed_and_latest(api_client: TestClient, session: Session) -> None:
    _seed(session)
    keyword_id = _new_keyword(api_client)

    api_client.post(_URL.format(kid=keyword_id))

    listed = api_client.get(
        f"/api/v1/keywords/{keyword_id}/signals?component=affiliate_opportunity"
    ).json()
    assert len(listed) == 1
    latest = api_client.get(
        f"/api/v1/keywords/{keyword_id}/signals/affiliate_opportunity/latest"
    ).json()
    assert latest["provider"] == "affiliate_catalog"


def test_keyword_not_found_returns_404(api_client: TestClient, session: Session) -> None:
    _seed(session)
    resp = api_client.post(_URL.format(kid=999999))
    assert resp.status_code == 404
    _assert_error_shape(resp.json(), "entity_not_found")


def test_does_not_disturb_other_signals(api_client: TestClient, session: Session) -> None:
    _seed(session)
    keyword_id = _new_keyword(api_client)

    for component, value in (
        ("search_demand", 55.0),
        ("commercial_intent", 88.0),
        ("trend", 61.0),
        ("site_relevance", 90.0),
    ):
        r = api_client.post(
            f"/api/v1/keywords/{keyword_id}/signals",
            json={
                "component": component,
                "normalized_value": value,
                "provider": "manual",
                "observed_at": "2026-01-01T00:00:00Z",
            },
        )
        assert r.status_code == 201

    api_client.post(_URL.format(kid=keyword_id))

    signals = api_client.get(f"/api/v1/keywords/{keyword_id}/signals").json()
    by_component = {s["component"]: s["normalized_value"] for s in signals}
    assert set(by_component) == {
        "search_demand",
        "commercial_intent",
        "trend",
        "site_relevance",
        "affiliate_opportunity",
    }
    assert by_component["search_demand"] == 55.0
    assert by_component["commercial_intent"] == 88.0
    assert by_component["trend"] == 61.0
    assert by_component["site_relevance"] == 90.0


def test_from_signals_still_incomplete_after_affiliate_opportunity(
    api_client: TestClient, session: Session
) -> None:
    _seed(session)
    keyword_id = _new_keyword(api_client)

    for component in ("search_demand", "commercial_intent", "trend", "site_relevance"):
        api_client.post(
            f"/api/v1/keywords/{keyword_id}/signals",
            json={
                "component": component,
                "normalized_value": 50.0,
                "provider": "manual",
                "observed_at": "2026-01-01T00:00:00Z",
            },
        )
    api_client.post(_URL.format(kid=keyword_id))  # 5/7 目

    resp = api_client.post(f"/api/v1/keywords/{keyword_id}/scores/from-signals")
    assert resp.status_code == 409
    _assert_error_shape(resp.json(), "incomplete_signal_set")
    message = resp.json()["error"]["message"]
    assert "competition_ease" in message
    assert "originality" in message
