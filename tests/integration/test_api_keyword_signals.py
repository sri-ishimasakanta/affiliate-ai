"""Keyword Signal / score-from-signals API の統合テスト。"""

from fastapi.testclient import TestClient

_OBSERVED = "2026-08-01T00:00:00Z"

_KNOWN_VALUES = {
    "search_demand": 75,
    "commercial_intent": 95,
    "affiliate_opportunity": 90,
    "competition_ease": 55,
    "trend": 90,
    "originality": 80,
    "site_relevance": 100,
}
_EXPECTED_TOTAL = 82.25


def _assert_error_shape(body: dict, code: str) -> None:
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    assert body["error"]["message"]


def _new_keyword(client: TestClient, keyword: str = "kw") -> int:
    resp = client.post("/api/v1/keywords", json={"keyword": keyword})
    assert resp.status_code == 201
    return resp.json()["id"]


def _signal_body(component: str, normalized_value: float = 50.0, **extra: object) -> dict:
    return {
        "component": component,
        "normalized_value": normalized_value,
        "provider": "manual",
        "observed_at": _OBSERVED,
        **extra,
    }


def _post_signal(client: TestClient, keyword_id: int, component: str, value: float) -> dict:
    resp = client.post(
        f"/api/v1/keywords/{keyword_id}/signals",
        json=_signal_body(component, value),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _seed_full_set(client: TestClient, keyword_id: int, values: dict[str, float]) -> None:
    for component, value in values.items():
        _post_signal(client, keyword_id, component, value)


# -- POST /signals --------------------------------------------------------
def test_post_signal_returns_201(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)

    resp = api_client.post(
        f"/api/v1/keywords/{keyword_id}/signals",
        json=_signal_body(
            "trend", 42, provider="google_trends",
            raw_data={"interest": [1, 2, 3]}, source_reference="https://trends",
        ),
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] > 0
    assert body["keyword_id"] == keyword_id
    assert body["component"] == "trend"
    assert body["normalized_value"] == 42
    assert body["provider"] == "google_trends"
    assert body["raw_data"] == {"interest": [1, 2, 3]}
    assert body["source_reference"] == "https://trends"
    assert body["created_at"]


def test_post_signal_keyword_not_found_returns_404(api_client: TestClient) -> None:
    resp = api_client.post(
        "/api/v1/keywords/999999/signals", json=_signal_body("trend")
    )
    assert resp.status_code == 404
    _assert_error_shape(resp.json(), "entity_not_found")


def test_post_signal_normalized_out_of_range_returns_422(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    assert api_client.post(
        f"/api/v1/keywords/{keyword_id}/signals", json=_signal_body("trend", -1)
    ).status_code == 422
    assert api_client.post(
        f"/api/v1/keywords/{keyword_id}/signals", json=_signal_body("trend", 101)
    ).status_code == 422


def test_post_signal_invalid_component_returns_422(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    resp = api_client.post(
        f"/api/v1/keywords/{keyword_id}/signals", json=_signal_body("bogus")
    )
    assert resp.status_code == 422


def test_post_signal_extra_field_returns_422(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    body = _signal_body("trend")
    body["unexpected"] = 1
    resp = api_client.post(f"/api/v1/keywords/{keyword_id}/signals", json=body)
    assert resp.status_code == 422


def test_post_signal_missing_observed_at_returns_422(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    body = _signal_body("trend")
    del body["observed_at"]
    resp = api_client.post(f"/api/v1/keywords/{keyword_id}/signals", json=body)
    assert resp.status_code == 422


# -- GET /signals -------------------------------------------------------
def test_list_signals_all_newest_first(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    _post_signal(api_client, keyword_id, "trend", 10)
    _post_signal(api_client, keyword_id, "trend", 20)
    last = _post_signal(api_client, keyword_id, "originality", 30)

    resp = api_client.get(f"/api/v1/keywords/{keyword_id}/signals")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 3
    assert body[0]["id"] == last["id"]


def test_list_signals_component_filter(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    _post_signal(api_client, keyword_id, "trend", 10)
    _post_signal(api_client, keyword_id, "trend", 20)
    _post_signal(api_client, keyword_id, "originality", 30)

    resp = api_client.get(
        f"/api/v1/keywords/{keyword_id}/signals", params={"component": "trend"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert all(row["component"] == "trend" for row in body)


def test_list_signals_pagination(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    for _ in range(5):
        _post_signal(api_client, keyword_id, "trend", 10)

    resp = api_client.get(
        f"/api/v1/keywords/{keyword_id}/signals", params={"limit": 2, "offset": 1}
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_list_signals_invalid_pagination_returns_422(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    assert api_client.get(
        f"/api/v1/keywords/{keyword_id}/signals", params={"limit": 0}
    ).status_code == 422
    assert api_client.get(
        f"/api/v1/keywords/{keyword_id}/signals", params={"limit": 101}
    ).status_code == 422
    assert api_client.get(
        f"/api/v1/keywords/{keyword_id}/signals", params={"offset": -1}
    ).status_code == 422


def test_list_signals_invalid_component_query_returns_422(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    resp = api_client.get(
        f"/api/v1/keywords/{keyword_id}/signals", params={"component": "nope"}
    )
    assert resp.status_code == 422


# -- GET /signals/{component}/latest ----------------------------------
def test_get_latest_signal_ok(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    _post_signal(api_client, keyword_id, "trend", 10)
    newest = _post_signal(api_client, keyword_id, "trend", 88)

    resp = api_client.get(f"/api/v1/keywords/{keyword_id}/signals/trend/latest")
    assert resp.status_code == 200
    assert resp.json()["id"] == newest["id"]
    assert resp.json()["normalized_value"] == 88


def test_get_latest_signal_none_returns_404(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    resp = api_client.get(f"/api/v1/keywords/{keyword_id}/signals/trend/latest")
    assert resp.status_code == 404
    _assert_error_shape(resp.json(), "entity_not_found")


def test_get_latest_signal_keyword_not_found_returns_404(api_client: TestClient) -> None:
    resp = api_client.get("/api/v1/keywords/999999/signals/trend/latest")
    assert resp.status_code == 404
    _assert_error_shape(resp.json(), "entity_not_found")


# -- POST /scores/from-signals --------------------------------------
def test_score_from_signals_ok(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    _seed_full_set(api_client, keyword_id, _KNOWN_VALUES)

    resp = api_client.post(f"/api/v1/keywords/{keyword_id}/scores/from-signals")

    assert resp.status_code == 201
    body = resp.json()
    assert body["total_score"] == _EXPECTED_TOTAL
    assert body["input_source"] == "signals"
    assert body["search_demand"] == 75
    assert body["site_relevance"] == 100


def test_score_from_signals_updates_keyword_and_status(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    _seed_full_set(api_client, keyword_id, _KNOWN_VALUES)

    before = api_client.get(f"/api/v1/keywords/{keyword_id}").json()
    assert before["opportunity_score"] is None
    assert before["status"] == "discovered"

    api_client.post(f"/api/v1/keywords/{keyword_id}/scores/from-signals")

    after = api_client.get(f"/api/v1/keywords/{keyword_id}").json()
    assert after["opportunity_score"] == _EXPECTED_TOTAL
    assert after["status"] == "analyzed"


def test_score_from_signals_incomplete_returns_409(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    partial = {k: v for k, v in _KNOWN_VALUES.items() if k != "site_relevance"}
    _seed_full_set(api_client, keyword_id, partial)

    resp = api_client.post(f"/api/v1/keywords/{keyword_id}/scores/from-signals")

    assert resp.status_code == 409
    _assert_error_shape(resp.json(), "incomplete_signal_set")
    assert "site_relevance" in resp.json()["error"]["message"]

    # score は作られていない
    assert api_client.get(f"/api/v1/keywords/{keyword_id}/scores").json() == []
    assert api_client.get(f"/api/v1/keywords/{keyword_id}").json()["status"] == "discovered"


def test_score_from_signals_keyword_not_found_returns_404(api_client: TestClient) -> None:
    resp = api_client.post("/api/v1/keywords/999999/scores/from-signals")
    assert resp.status_code == 404
    _assert_error_shape(resp.json(), "entity_not_found")


# -- GET /scores/{score_id}/signals --------------------------------
def test_score_signals_provenance_returns_seven(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    _seed_full_set(api_client, keyword_id, _KNOWN_VALUES)
    score = api_client.post(
        f"/api/v1/keywords/{keyword_id}/scores/from-signals"
    ).json()

    resp = api_client.get(
        f"/api/v1/keywords/{keyword_id}/scores/{score['id']}/signals"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 7
    assert {row["component"] for row in body} == set(_KNOWN_VALUES)


def test_score_signals_manual_score_returns_empty(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    score = api_client.post(
        f"/api/v1/keywords/{keyword_id}/scores", json=_KNOWN_VALUES
    ).json()

    resp = api_client.get(
        f"/api/v1/keywords/{keyword_id}/scores/{score['id']}/signals"
    )
    assert resp.status_code == 200
    assert resp.json() == []


def test_score_signals_missing_score_returns_404(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    resp = api_client.get(f"/api/v1/keywords/{keyword_id}/scores/999999/signals")
    assert resp.status_code == 404
    _assert_error_shape(resp.json(), "entity_not_found")


def test_score_signals_wrong_keyword_returns_404(api_client: TestClient) -> None:
    kw_a = _new_keyword(api_client, "a")
    kw_b = _new_keyword(api_client, "b")
    _seed_full_set(api_client, kw_a, _KNOWN_VALUES)
    score = api_client.post(
        f"/api/v1/keywords/{kw_a}/scores/from-signals"
    ).json()

    resp = api_client.get(f"/api/v1/keywords/{kw_b}/scores/{score['id']}/signals")
    assert resp.status_code == 404
    _assert_error_shape(resp.json(), "entity_not_found")


# -- manual score compatibility (Phase 2A regression) -----------------
def test_manual_score_still_works(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    resp = api_client.post(
        f"/api/v1/keywords/{keyword_id}/scores", json=_KNOWN_VALUES
    )
    assert resp.status_code == 201
    assert resp.json()["total_score"] == _EXPECTED_TOTAL
    assert resp.json()["input_source"] == "manual"
