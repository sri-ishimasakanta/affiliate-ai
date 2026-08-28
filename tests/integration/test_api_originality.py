"""originality 導出 API の統合テスト (外部通信なし)。"""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Article, Keyword


def _assert_error_shape(body: dict, code: str) -> None:
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    assert body["error"]["message"]


_URL = "/api/v1/keywords/{kid}/signals/originality"


def _new_keyword(client: TestClient, keyword: str = "AI 議事録 おすすめ") -> int:
    resp = client.post("/api/v1/keywords", json={"keyword": keyword})
    assert resp.status_code == 201
    return resp.json()["id"]


def test_empty_corpus_returns_201_value_100(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)

    resp = api_client.post(_URL.format(kid=keyword_id))

    assert resp.status_code == 201
    body = resp.json()
    assert body["keyword_id"] == keyword_id
    assert body["component"] == "originality"
    assert body["provider"] == "internal_corpus"
    assert body["normalized_value"] == 100.0
    assert body["source_reference"] == "internal-corpus:v1"
    raw = body["raw_data"]
    assert raw["corpus_available"] is False
    assert raw["evidence_coverage"] == 0.0
    assert raw["normalizer"] == {"name": "originality", "version": "v1"}


def test_normal_corpus_returns_201(api_client: TestClient, session: Session) -> None:
    other = Keyword(keyword="AI 議事録 比較")
    other.status = "selected"
    session.add(other)
    session.commit()

    keyword_id = _new_keyword(api_client, "AI 議事録 おすすめ")
    resp = api_client.post(_URL.format(kid=keyword_id))

    assert resp.status_code == 201
    body = resp.json()
    assert 0.0 <= body["normalized_value"] <= 100.0
    assert body["raw_data"]["corpus_available"] is True
    assert body["raw_data"]["most_similar_kind"] == "keyword"


def test_body_and_url_never_in_response(api_client: TestClient, session: Session) -> None:
    other = Keyword(keyword="AI 文字起こし ツール")
    other.status = "analyzed"
    session.add(other)
    session.flush()
    article = Article(title="AI 議事録 おすすめ 記事", slug="s1", keyword_id=other.id)
    article.status = "published"
    article.body = "SUPER_SECRET_BODY_TEXT"
    article.published_url = "https://wp.example.test/secret-post"
    session.add(article)
    session.commit()

    keyword_id = _new_keyword(api_client, "AI 議事録 おすすめ")
    resp = api_client.post(_URL.format(kid=keyword_id))

    assert resp.status_code == 201
    assert "SUPER_SECRET_BODY_TEXT" not in resp.text
    assert "wp.example.test" not in resp.text


def test_keyword_not_found_returns_404(api_client: TestClient) -> None:
    resp = api_client.post(_URL.format(kid=999999))
    assert resp.status_code == 404
    _assert_error_shape(resp.json(), "entity_not_found")


def test_persisted_listed_latest(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)
    api_client.post(_URL.format(kid=keyword_id))

    listed = api_client.get(
        f"/api/v1/keywords/{keyword_id}/signals?component=originality"
    ).json()
    assert len(listed) == 1
    latest = api_client.get(
        f"/api/v1/keywords/{keyword_id}/signals/originality/latest"
    ).json()
    assert latest["provider"] == "internal_corpus"
    assert latest["normalized_value"] == 100.0


def test_does_not_disturb_other_signals(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)

    for component, value in (
        ("search_demand", 55.0),
        ("commercial_intent", 88.0),
        ("trend", 61.0),
        ("site_relevance", 90.0),
        ("affiliate_opportunity", 42.0),
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
        "originality",
    }
    assert by_component["search_demand"] == 55.0
    assert by_component["affiliate_opportunity"] == 42.0


def test_from_signals_missing_only_competition_ease(api_client: TestClient) -> None:
    keyword_id = _new_keyword(api_client)

    for component in (
        "search_demand",
        "commercial_intent",
        "trend",
        "site_relevance",
        "affiliate_opportunity",
    ):
        api_client.post(
            f"/api/v1/keywords/{keyword_id}/signals",
            json={
                "component": component,
                "normalized_value": 50.0,
                "provider": "manual",
                "observed_at": "2026-01-01T00:00:00Z",
            },
        )
    api_client.post(_URL.format(kid=keyword_id))  # 6/7 目

    resp = api_client.post(f"/api/v1/keywords/{keyword_id}/scores/from-signals")
    assert resp.status_code == 409
    _assert_error_shape(resp.json(), "incomplete_signal_set")
    message = resp.json()["error"]["message"]
    assert "competition_ease" in message
    assert "originality" not in message  # originality は揃った
