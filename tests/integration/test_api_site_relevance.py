"""site_relevance ローカル導出 API の統合テスト (外部通信なし)。"""

from fastapi.testclient import TestClient

_URL = "/api/v1/keywords/{kid}/signals/site-relevance"


def _assert_error_shape(body: dict, code: str) -> None:
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    assert body["error"]["message"]


def _new_keyword(client: TestClient, keyword: str = "AI 議事録 おすすめ") -> int:
    resp = client.post("/api/v1/keywords", json={"keyword": keyword})
    assert resp.status_code == 201
    return resp.json()["id"]


def test_derive_returns_201_and_signal_body(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client, "AI 議事録 おすすめ")

    resp = api_client.post(_URL.format(kid=keyword_id))

    assert resp.status_code == 201
    body = resp.json()
    assert body["keyword_id"] == keyword_id
    assert body["component"] == "site_relevance"
    assert body["provider"] == "site_profile"
    assert body["normalized_value"] == 90.0
    assert body["period_start"] is None
    assert body["period_end"] is None
    assert body["source_reference"] == "site-profile:ai-business-automation:v1"
    raw = body["raw_data"]
    assert set(raw["matched_groups"]) == {"CORE_THEME", "ADJACENT_USE_CASE"}
    assert raw["multi_group_bonus"] == 10.0
    assert raw["profile_name"] == "ai_business_automation"
    assert raw["normalizer"] == {"name": "site_relevance", "version": "v1"}


def test_derive_out_of_scope_keyword_scores_zero(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client, "東京 観光")
    resp = api_client.post(_URL.format(kid=keyword_id))
    assert resp.status_code == 201
    assert resp.json()["normalized_value"] == 0.0


def test_derived_signal_is_persisted_listed_and_latest(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client, "AI 業務効率化")

    api_client.post(_URL.format(kid=keyword_id))

    listed = api_client.get(
        f"/api/v1/keywords/{keyword_id}/signals?component=site_relevance"
    ).json()
    assert len(listed) == 1
    assert listed[0]["component"] == "site_relevance"
    assert listed[0]["normalized_value"] == 100.0

    latest = api_client.get(
        f"/api/v1/keywords/{keyword_id}/signals/site_relevance/latest"
    ).json()
    assert latest["provider"] == "site_profile"
    assert latest["normalized_value"] == 100.0


def test_derive_keyword_not_found_returns_404(api_client: TestClient) -> None:
    resp = api_client.post(_URL.format(kid=999999))
    assert resp.status_code == 404
    _assert_error_shape(resp.json(), "entity_not_found")


def test_derive_ignores_request_body(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    resp = api_client.post(_URL.format(kid=keyword_id), json={"unexpected": "value"})
    assert resp.status_code == 201


def test_repeated_derivation_appends_history(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client, "AI 議事録 おすすめ")

    first = api_client.post(_URL.format(kid=keyword_id)).json()
    second = api_client.post(_URL.format(kid=keyword_id)).json()
    assert first["id"] != second["id"]

    history = api_client.get(
        f"/api/v1/keywords/{keyword_id}/signals?component=site_relevance"
    ).json()
    assert len(history) == 2


def test_site_relevance_does_not_disturb_other_signals(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client, "AI 議事録 おすすめ")

    # 既存の手動 Signal (search_demand / commercial_intent / trend)
    for component, value in (
        ("search_demand", 55.0),
        ("commercial_intent", 88.0),
        ("trend", 61.0),
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
    by_component = {s["component"]: s for s in signals}
    assert set(by_component) == {
        "search_demand",
        "commercial_intent",
        "trend",
        "site_relevance",
    }
    assert by_component["search_demand"]["normalized_value"] == 55.0
    assert by_component["commercial_intent"]["normalized_value"] == 88.0
    assert by_component["trend"]["normalized_value"] == 61.0


def test_from_signals_still_incomplete_with_site_relevance(api_client: TestClient) -> None:
    # 自動 Signal は sd / ci / trend / site_relevance の 4 つ。残り 3 不足で 409 のまま。
    keyword_id = _new_keyword(api_client, "AI 業務効率化")

    for component, value in (
        ("search_demand", 60.0),
        ("commercial_intent", 70.0),
        ("trend", 50.0),
    ):
        api_client.post(
            f"/api/v1/keywords/{keyword_id}/signals",
            json={
                "component": component,
                "normalized_value": value,
                "provider": "manual",
                "observed_at": "2026-01-01T00:00:00Z",
            },
        )
    api_client.post(_URL.format(kid=keyword_id))

    resp = api_client.post(f"/api/v1/keywords/{keyword_id}/scores/from-signals")
    assert resp.status_code == 409
    _assert_error_shape(resp.json(), "incomplete_signal_set")
    missing = resp.json()["error"]["message"]
    assert "affiliate_opportunity" in missing
    assert "competition_ease" in missing
    assert "originality" in missing
