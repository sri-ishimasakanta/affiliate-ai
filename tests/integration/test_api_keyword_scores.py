"""Keyword Opportunity Score API (/api/v1/keywords/{id}/scores...) の統合テスト。"""

from fastapi import status
from fastapi.testclient import TestClient

_KNOWN_BODY = {
    "search_demand": 75,
    "commercial_intent": 95,
    "affiliate_opportunity": 90,
    "competition_ease": 55,
    "trend": 90,
    "originality": 80,
    "site_relevance": 100,
}
_EXPECTED_TOTAL = 82.25

_SIMPLE_BODY = dict.fromkeys(_KNOWN_BODY, 10)


def _assert_error_shape(body: dict, code: str) -> None:
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    assert body["error"]["message"]


def _new_keyword(client: TestClient, keyword: str = "kw") -> int:
    resp = client.post("/api/v1/keywords", json={"keyword": keyword})
    assert resp.status_code == 201
    return resp.json()["id"]


# -- POST /scores -----------------------------------------------------------
def test_post_score_returns_201(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)

    resp = api_client.post(f"/api/v1/keywords/{keyword_id}/scores", json=_KNOWN_BODY)

    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] > 0
    assert body["keyword_id"] == keyword_id
    assert body["total_score"] == _EXPECTED_TOTAL
    assert body["score_version"] == "v1"
    assert body["input_source"] == "manual"
    assert body["search_demand"] == 75
    assert body["created_at"]


def test_post_score_keyword_not_found_returns_404(api_client: TestClient) -> None:
    resp = api_client.post("/api/v1/keywords/999999/scores", json=_KNOWN_BODY)

    assert resp.status_code == 404
    _assert_error_shape(resp.json(), "entity_not_found")


def test_post_score_component_below_zero_returns_422(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    body = {**_KNOWN_BODY, "trend": -1}

    resp = api_client.post(f"/api/v1/keywords/{keyword_id}/scores", json=body)

    assert resp.status_code == 422


def test_post_score_component_above_hundred_returns_422(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    body = {**_KNOWN_BODY, "competition_ease": 101}

    resp = api_client.post(f"/api/v1/keywords/{keyword_id}/scores", json=body)

    assert resp.status_code == 422


def test_post_score_missing_component_returns_422(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    body = {k: v for k, v in _KNOWN_BODY.items() if k != "site_relevance"}

    resp = api_client.post(f"/api/v1/keywords/{keyword_id}/scores", json=body)

    assert resp.status_code == 422


def test_post_score_rejects_client_total_score(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    body = {**_KNOWN_BODY, "total_score": 5}

    resp = api_client.post(f"/api/v1/keywords/{keyword_id}/scores", json=body)

    assert resp.status_code == 422


def test_post_score_rejects_client_score_version(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    body = {**_KNOWN_BODY, "score_version": "v2"}

    resp = api_client.post(f"/api/v1/keywords/{keyword_id}/scores", json=body)

    assert resp.status_code == 422


# -- GET /scores/latest ---------------------------------------------------
def test_get_latest_returns_200(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    api_client.post(f"/api/v1/keywords/{keyword_id}/scores", json=_SIMPLE_BODY)
    api_client.post(f"/api/v1/keywords/{keyword_id}/scores", json=_KNOWN_BODY)

    resp = api_client.get(f"/api/v1/keywords/{keyword_id}/scores/latest")

    assert resp.status_code == 200
    assert resp.json()["total_score"] == _EXPECTED_TOTAL


def test_get_latest_keyword_not_found_returns_404(api_client: TestClient) -> None:
    resp = api_client.get("/api/v1/keywords/999999/scores/latest")

    assert resp.status_code == 404
    _assert_error_shape(resp.json(), "entity_not_found")


def test_get_latest_without_score_returns_404(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)

    resp = api_client.get(f"/api/v1/keywords/{keyword_id}/scores/latest")

    assert resp.status_code == 404
    _assert_error_shape(resp.json(), "entity_not_found")


# -- GET /scores (history) ----------------------------------------------
def test_get_history_empty(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)

    resp = api_client.get(f"/api/v1/keywords/{keyword_id}/scores")

    assert resp.status_code == 200
    assert resp.json() == []


def test_get_history_multiple_newest_first(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    for _ in range(3):
        api_client.post(f"/api/v1/keywords/{keyword_id}/scores", json=_SIMPLE_BODY)
    api_client.post(f"/api/v1/keywords/{keyword_id}/scores", json=_KNOWN_BODY)

    resp = api_client.get(f"/api/v1/keywords/{keyword_id}/scores")

    body = resp.json()
    assert len(body) == 4
    assert body[0]["total_score"] == _EXPECTED_TOTAL


def test_get_history_limit_and_offset(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    for _ in range(5):
        api_client.post(f"/api/v1/keywords/{keyword_id}/scores", json=_SIMPLE_BODY)

    resp = api_client.get(
        f"/api/v1/keywords/{keyword_id}/scores", params={"limit": 2, "offset": 1}
    )

    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_get_history_invalid_pagination_returns_422(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)

    assert (
        api_client.get(
            f"/api/v1/keywords/{keyword_id}/scores", params={"limit": 0}
        ).status_code
        == 422
    )
    assert (
        api_client.get(
            f"/api/v1/keywords/{keyword_id}/scores", params={"limit": 101}
        ).status_code
        == 422
    )
    assert (
        api_client.get(
            f"/api/v1/keywords/{keyword_id}/scores", params={"offset": -1}
        ).status_code
        == 422
    )


# -- integration: score -> keyword reflects cache & status ---------------
def test_scoring_reflects_on_keyword_and_moves_status(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)

    before = api_client.get(f"/api/v1/keywords/{keyword_id}").json()
    assert before["opportunity_score"] is None
    assert before["status"] == "discovered"

    post = api_client.post(f"/api/v1/keywords/{keyword_id}/scores", json=_KNOWN_BODY)
    assert post.status_code == status.HTTP_201_CREATED

    after = api_client.get(f"/api/v1/keywords/{keyword_id}").json()
    assert after["opportunity_score"] == _EXPECTED_TOTAL
    assert after["opportunity_score"] == post.json()["total_score"]
    assert after["status"] == "analyzed"


def test_rescoring_does_not_change_analyzed_status(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    api_client.post(f"/api/v1/keywords/{keyword_id}/scores", json=_KNOWN_BODY)
    api_client.post(f"/api/v1/keywords/{keyword_id}/scores", json=_SIMPLE_BODY)

    after = api_client.get(f"/api/v1/keywords/{keyword_id}").json()
    assert after["status"] == "analyzed"
    assert after["opportunity_score"] == 10.0
