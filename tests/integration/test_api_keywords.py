"""Keyword REST API (/api/v1/keywords) の統合テスト。"""

from fastapi import status
from fastapi.testclient import TestClient


def _assert_error_shape(body: dict, code: str) -> None:
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str)
    assert body["error"]["message"]


def _create(client: TestClient, keyword: str, **extra: object) -> dict:
    resp = client.post("/api/v1/keywords", json={"keyword": keyword, **extra})
    assert resp.status_code == status.HTTP_201_CREATED
    return resp.json()


# -- POST ---------------------------------------------------------------------
def test_create_keyword_returns_201(api_client: TestClient) -> None:
    resp = api_client.post(
        "/api/v1/keywords",
        json={"keyword": "nisa 始め方", "search_intent": "commercial", "category": "投資"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] > 0
    assert body["keyword"] == "nisa 始め方"
    assert body["search_intent"] == "commercial"
    assert body["category"] == "投資"
    assert body["status"] == "discovered"
    assert body["opportunity_score"] is None
    assert body["created_at"] and body["updated_at"]


def test_create_duplicate_keyword_returns_409(api_client: TestClient) -> None:
    _create(api_client, "重複 kw")

    resp = api_client.post("/api/v1/keywords", json={"keyword": "重複 kw"})

    assert resp.status_code == 409
    _assert_error_shape(resp.json(), "duplicate_entity")


def test_create_invalid_payload_returns_422(api_client: TestClient) -> None:
    # keyword 欠落 / 空文字 / 型不正
    assert api_client.post("/api/v1/keywords", json={"search_intent": "x"}).status_code == 422
    assert api_client.post("/api/v1/keywords", json={"keyword": ""}).status_code == 422
    assert api_client.post("/api/v1/keywords", json={"keyword": ["a"]}).status_code == 422


# -- GET detail -------------------------------------------------------------
def test_get_keyword_returns_200(api_client: TestClient) -> None:
    created = _create(api_client, "取得 kw")

    resp = api_client.get(f"/api/v1/keywords/{created['id']}")

    assert resp.status_code == 200
    assert resp.json()["keyword"] == "取得 kw"


def test_get_keyword_not_found_returns_404(api_client: TestClient) -> None:
    resp = api_client.get("/api/v1/keywords/999999")

    assert resp.status_code == 404
    _assert_error_shape(resp.json(), "entity_not_found")


# -- GET list -------------------------------------------------------------
def test_list_keywords_returns_all(api_client: TestClient) -> None:
    for index in range(3):
        _create(api_client, f"kw-{index}")

    resp = api_client.get("/api/v1/keywords")

    assert resp.status_code == 200
    assert [item["keyword"] for item in resp.json()] == ["kw-0", "kw-1", "kw-2"]


def test_list_keywords_limit_and_offset(api_client: TestClient) -> None:
    for index in range(5):
        _create(api_client, f"kw-{index}")

    resp = api_client.get("/api/v1/keywords", params={"limit": 2, "offset": 1})

    assert resp.status_code == 200
    assert [item["keyword"] for item in resp.json()] == ["kw-1", "kw-2"]


def test_list_keywords_invalid_pagination_returns_422(api_client: TestClient) -> None:
    assert api_client.get("/api/v1/keywords", params={"limit": 0}).status_code == 422
    assert api_client.get("/api/v1/keywords", params={"limit": 101}).status_code == 422
    assert api_client.get("/api/v1/keywords", params={"offset": -1}).status_code == 422


# -- PATCH --------------------------------------------------------------------
def test_patch_keyword_partial_update(api_client: TestClient) -> None:
    created = _create(api_client, "更新 kw", search_intent="informational", category="税")

    resp = api_client.patch(
        f"/api/v1/keywords/{created['id']}", json={"category": "節税"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["category"] == "節税"
    assert body["search_intent"] == "informational"


def test_patch_keyword_not_found_returns_404(api_client: TestClient) -> None:
    resp = api_client.patch("/api/v1/keywords/999999", json={"category": "x"})

    assert resp.status_code == 404
    _assert_error_shape(resp.json(), "entity_not_found")


# -- DELETE --------------------------------------------------------------------
def test_delete_keyword_returns_204_without_body(api_client: TestClient) -> None:
    created = _create(api_client, "削除 kw")

    resp = api_client.delete(f"/api/v1/keywords/{created['id']}")

    assert resp.status_code == 204
    assert resp.content == b""
    assert api_client.get(f"/api/v1/keywords/{created['id']}").status_code == 404


def test_delete_keyword_not_found_returns_404(api_client: TestClient) -> None:
    resp = api_client.delete("/api/v1/keywords/999999")

    assert resp.status_code == 404
    _assert_error_shape(resp.json(), "entity_not_found")


# -- PATCH /status ----------------------------------------------------------
def test_change_status_valid_transition(api_client: TestClient) -> None:
    created = _create(api_client, "遷移 kw")

    resp = api_client.patch(
        f"/api/v1/keywords/{created['id']}/status", json={"status": "analyzed"}
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "analyzed"


def test_change_status_invalid_transition_returns_409(api_client: TestClient) -> None:
    created = _create(api_client, "不正遷移 kw")

    resp = api_client.patch(
        f"/api/v1/keywords/{created['id']}/status", json={"status": "assigned"}
    )

    assert resp.status_code == 409
    _assert_error_shape(resp.json(), "invalid_status_transition")
    assert api_client.get(f"/api/v1/keywords/{created['id']}").json()["status"] == "discovered"


def test_change_status_invalid_enum_returns_422(api_client: TestClient) -> None:
    created = _create(api_client, "不正enum kw")

    resp = api_client.patch(
        f"/api/v1/keywords/{created['id']}/status", json={"status": "banana"}
    )

    assert resp.status_code == 422


def test_change_status_not_found_returns_404(api_client: TestClient) -> None:
    resp = api_client.patch("/api/v1/keywords/999999/status", json={"status": "analyzed"})

    assert resp.status_code == 404
    _assert_error_shape(resp.json(), "entity_not_found")
