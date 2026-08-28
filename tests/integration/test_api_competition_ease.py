"""competition_ease manual API + 7/7 signals から Opportunity Score 生成の統合テスト。"""

from fastapi import status
from fastapi.testclient import TestClient

_URL = "/api/v1/keywords/{kid}/signals/competition-ease/manual"


def _assert_error_shape(body: dict, code: str) -> None:
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    assert body["error"]["message"]


def _new_keyword(client: TestClient, keyword: str = "AI 議事録 おすすめ") -> int:
    resp = client.post("/api/v1/keywords", json={"keyword": keyword})
    assert resp.status_code == 201
    return resp.json()["id"]


def test_manual_returns_201_and_converts_difficulty(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)

    resp = api_client.post(
        _URL.format(kid=keyword_id),
        json={
            "keyword_difficulty": 32,
            "source_name": "example_free_seo_tool",
            "source_reference": None,
            "observed_at": None,
        },
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["component"] == "competition_ease"
    assert body["provider"] == "manual_keyword_difficulty"
    assert body["normalized_value"] == 68.0
    assert body["raw_data"]["difficulty_scale"] == "0_easy_100_hard"
    assert body["raw_data"]["collection_method"] == "manual"
    assert body["raw_data"]["source_name"] == "example_free_seo_tool"
    assert body["source_reference"] == "manual-keyword-difficulty:v1"


def test_manual_boundaries(api_client: TestClient) -> None:
    kid = _new_keyword(api_client, "kw-a")
    assert (
        api_client.post(
            _URL.format(kid=kid), json={"keyword_difficulty": 0, "source_name": "t"}
        ).json()["normalized_value"]
        == 100.0
    )
    kid2 = _new_keyword(api_client, "kw-b")
    assert (
        api_client.post(
            _URL.format(kid=kid2), json={"keyword_difficulty": 100, "source_name": "t"}
        ).json()["normalized_value"]
        == 0.0
    )


def test_manual_validation_422(api_client: TestClient) -> None:
    kid = _new_keyword(api_client)
    for bad in (
        {"keyword_difficulty": -1, "source_name": "t"},
        {"keyword_difficulty": 101, "source_name": "t"},
        {"keyword_difficulty": True, "source_name": "t"},
        {"keyword_difficulty": 50},  # source_name 欠落
        {"keyword_difficulty": 50, "source_name": "   "},  # blank
        {"source_name": "t"},  # difficulty 欠落
    ):
        assert api_client.post(_URL.format(kid=kid), json=bad).status_code == 422


def test_manual_keyword_not_found_404(api_client: TestClient) -> None:
    resp = api_client.post(
        _URL.format(kid=999999),
        json={"keyword_difficulty": 50, "source_name": "t"},
    )
    assert resp.status_code == 404
    _assert_error_shape(resp.json(), "entity_not_found")


def test_manual_persist_list_latest(api_client: TestClient) -> None:
    kid = _new_keyword(api_client)
    api_client.post(
        _URL.format(kid=kid), json={"keyword_difficulty": 40, "source_name": "t"}
    )
    listed = api_client.get(
        f"/api/v1/keywords/{kid}/signals?component=competition_ease"
    ).json()
    assert len(listed) == 1
    latest = api_client.get(
        f"/api/v1/keywords/{kid}/signals/competition_ease/latest"
    ).json()
    assert latest["provider"] == "manual_keyword_difficulty"
    assert latest["normalized_value"] == 60.0


def test_no_credentials_in_response(api_client: TestClient) -> None:
    kid = _new_keyword(api_client)
    resp = api_client.post(
        _URL.format(kid=kid),
        json={
            "keyword_difficulty": 25,
            "source_name": "example_free_seo_tool",
            "source_reference": "public-report-2026",
        },
    )
    text = resp.text
    for forbidden in ("api_key", "apikey", "password", "account_id", "token", "competition_index"):
        assert forbidden not in text


# -- 7/7 signals -> scores/from-signals SUCCESS -------------------
_KNOWN = {
    "search_demand": 75,
    "commercial_intent": 95,
    "affiliate_opportunity": 90,
    "trend": 90,
    "originality": 80,
    "site_relevance": 100,
}
_EXPECTED_TOTAL = 82.25  # competition_ease=55 (difficulty 45) を含む既存既知値


def test_seven_components_complete_from_signals_success(api_client: TestClient) -> None:
    kid = _new_keyword(api_client)

    for component, value in _KNOWN.items():
        r = api_client.post(
            f"/api/v1/keywords/{kid}/signals",
            json={
                "component": component,
                "normalized_value": value,
                "provider": "manual",
                "observed_at": "2026-01-01T00:00:00Z",
            },
        )
        assert r.status_code == 201

    # competition_ease だけ今回の manual route で作成 (difficulty 45 -> ease 55)
    ce = api_client.post(
        _URL.format(kid=kid),
        json={"keyword_difficulty": 45, "source_name": "example_free_seo_tool"},
    )
    assert ce.status_code == 201
    assert ce.json()["normalized_value"] == 55.0

    score = api_client.post(f"/api/v1/keywords/{kid}/scores/from-signals")
    assert score.status_code == status.HTTP_201_CREATED, score.text
    body = score.json()
    assert body["competition_ease"] == 55.0
    assert body["total_score"] == _EXPECTED_TOTAL
    assert body["score_version"] == "v1"
    assert body["input_source"] == "signals"

    # provenance link: 7 Signal すべて
    prov = api_client.get(
        f"/api/v1/keywords/{kid}/scores/{body['id']}/signals"
    ).json()
    assert len(prov) == 7
    assert {row["component"] for row in prov} == {"competition_ease", *_KNOWN}
    providers = {row["component"]: row["provider"] for row in prov}
    assert providers["competition_ease"] == "manual_keyword_difficulty"

    # Keyword.opportunity_score cache
    kw = api_client.get(f"/api/v1/keywords/{kid}").json()
    assert kw["opportunity_score"] == _EXPECTED_TOTAL
    assert kw["status"] == "analyzed"


def test_from_signals_incomplete_without_competition_ease(api_client: TestClient) -> None:
    kid = _new_keyword(api_client)
    for component, value in _KNOWN.items():
        api_client.post(
            f"/api/v1/keywords/{kid}/signals",
            json={
                "component": component,
                "normalized_value": value,
                "provider": "manual",
                "observed_at": "2026-01-01T00:00:00Z",
            },
        )
    resp = api_client.post(f"/api/v1/keywords/{kid}/scores/from-signals")
    assert resp.status_code == 409
    _assert_error_shape(resp.json(), "incomplete_signal_set")
    assert "competition_ease" in resp.json()["error"]["message"]
