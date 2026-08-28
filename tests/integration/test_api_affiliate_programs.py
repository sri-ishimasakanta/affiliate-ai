"""AffiliateProgram REST API (/api/v1/affiliate-programs) の統合テスト。"""

from fastapi import status
from fastapi.testclient import TestClient

_URL = "/api/v1/affiliate-programs"


def _assert_error_shape(body: dict, code: str) -> None:
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str)
    assert body["error"]["message"]


def _create(client: TestClient, name: str, **extra: object) -> dict:
    resp = client.post(_URL, json={"name": name, **extra})
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    return resp.json()


def test_post_returns_201(api_client: TestClient) -> None:
    resp = api_client.post(
        _URL,
        json={
            "name": "  Example AI Tool ",
            "provider": "example",
            "category": "ai",
            "commission_type": "fixed",
            "commission_value": 3000,
            "currency": "jpy",
            "match_terms": ["AI", "生成AI", "AI"],
            "status": "active",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] > 0
    assert body["name"] == "Example AI Tool"
    assert body["currency"] == "JPY"
    assert body["match_terms"] == ["AI", "生成AI"]
    assert body["status"] == "active"
    assert body["created_at"] and body["updated_at"]


def test_post_duplicate_returns_409(api_client: TestClient) -> None:
    _create(api_client, "Dup", provider="a8")
    resp = api_client.post(_URL, json={"name": "Dup", "provider": "a8"})
    assert resp.status_code == 409
    _assert_error_shape(resp.json(), "duplicate_entity")


def test_post_validation_422(api_client: TestClient) -> None:
    assert api_client.post(_URL, json={}).status_code == 422
    assert api_client.post(_URL, json={"name": "   "}).status_code == 422
    assert api_client.post(_URL, json={"name": "x", "currency": "JPYX"}).status_code == 422
    assert (
        api_client.post(_URL, json={"name": "x", "commission_value": -1}).status_code
        == 422
    )
    assert api_client.post(_URL, json={"name": "x", "status": "bogus"}).status_code == 422


def test_get_detail_and_404(api_client: TestClient) -> None:
    created = _create(api_client, "Detail")
    assert api_client.get(f"{_URL}/{created['id']}").json()["name"] == "Detail"

    resp = api_client.get(f"{_URL}/999999")
    assert resp.status_code == 404
    _assert_error_shape(resp.json(), "entity_not_found")


def test_list_and_filters(api_client: TestClient) -> None:
    _create(api_client, "L1", provider="a8", category="ai")
    _create(api_client, "L2", provider="a8", category="finance", status="paused")
    _create(api_client, "L3", provider="moshimo", category="ai")

    assert {p["name"] for p in api_client.get(_URL).json()} == {"L1", "L2", "L3"}
    assert {
        p["name"] for p in api_client.get(_URL, params={"provider": "a8"}).json()
    } == {"L1", "L2"}
    assert {
        p["name"] for p in api_client.get(_URL, params={"category": "ai"}).json()
    } == {"L1", "L3"}
    assert {
        p["name"] for p in api_client.get(_URL, params={"status": "paused"}).json()
    } == {"L2"}
    assert api_client.get(_URL, params={"status": "bogus"}).status_code == 422


def test_patch_updates_fields(api_client: TestClient) -> None:
    created = _create(api_client, "Patch", currency="JPY")

    resp = api_client.patch(
        f"{_URL}/{created['id']}",
        json={"category": "ai", "currency": "usd", "match_terms": [" a ", "a", "b"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["category"] == "ai"
    assert body["currency"] == "USD"
    assert body["match_terms"] == ["a", "b"]
    assert body["name"] == "Patch"  # 変更していないフィールドは維持


def test_patch_unknown_field_422(api_client: TestClient) -> None:
    created = _create(api_client, "PatchBad")
    resp = api_client.patch(f"{_URL}/{created['id']}", json={"nope": 1})
    assert resp.status_code == 422


def test_patch_404(api_client: TestClient) -> None:
    resp = api_client.patch(f"{_URL}/999999", json={"category": "ai"})
    assert resp.status_code == 404
    _assert_error_shape(resp.json(), "entity_not_found")


def test_delete_returns_204_then_404(api_client: TestClient) -> None:
    created = _create(api_client, "DeleteMe")

    resp = api_client.delete(f"{_URL}/{created['id']}")
    assert resp.status_code == 204
    assert resp.content == b""

    assert api_client.get(f"{_URL}/{created['id']}").status_code == 404
    assert api_client.delete(f"{_URL}/{created['id']}").status_code == 404


def test_tracking_url_round_trips_but_stays_a_normal_field(api_client: TestClient) -> None:
    created = _create(
        api_client, "WithTracking", tracking_url="https://example.test/aff/redirect"
    )
    assert created["tracking_url"] == "https://example.test/aff/redirect"
    fetched = api_client.get(f"{_URL}/{created['id']}").json()
    assert fetched["tracking_url"] == "https://example.test/aff/redirect"
