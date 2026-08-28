"""Article REST API (/api/v1/articles) の統合テスト。"""

from fastapi import status
from fastapi.testclient import TestClient


def _assert_error_shape(body: dict, code: str) -> None:
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str)
    assert body["error"]["message"]


def _create_keyword(client: TestClient, keyword: str = "kw") -> int:
    resp = client.post("/api/v1/keywords", json={"keyword": keyword})
    assert resp.status_code == 201
    return resp.json()["id"]


def _create_article(client: TestClient, *, title: str, slug: str, **extra: object) -> dict:
    resp = client.post(
        "/api/v1/articles", json={"title": title, "slug": slug, **extra}
    )
    assert resp.status_code == status.HTTP_201_CREATED
    return resp.json()


def _advance(client: TestClient, article_id: int, *statuses: str) -> None:
    for target in statuses:
        resp = client.patch(
            f"/api/v1/articles/{article_id}/status", json={"status": target}
        )
        assert resp.status_code == 200, resp.text


# -- POST ---------------------------------------------------------------------
def test_create_article_returns_201(api_client: TestClient) -> None:
    keyword_id = _create_keyword(api_client)

    resp = api_client.post(
        "/api/v1/articles",
        json={"keyword_id": keyword_id, "title": "記事タイトル", "slug": "kiji-title"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] > 0
    assert body["keyword_id"] == keyword_id
    assert body["slug"] == "kiji-title"
    assert body["status"] == "idea"
    assert body["draft_content"] is None
    assert body["published_url"] is None
    assert body["wordpress_id"] is None
    assert body["published_at"] is None


def test_create_article_without_keyword_id_is_allowed(api_client: TestClient) -> None:
    resp = api_client.post(
        "/api/v1/articles", json={"title": "キーワードなし", "slug": "no-keyword"}
    )

    assert resp.status_code == 201
    assert resp.json()["keyword_id"] is None


def test_create_article_duplicate_slug_returns_409(api_client: TestClient) -> None:
    _create_article(api_client, title="A", slug="dup-slug")

    resp = api_client.post("/api/v1/articles", json={"title": "B", "slug": "dup-slug"})

    assert resp.status_code == 409
    _assert_error_shape(resp.json(), "duplicate_entity")


def test_create_article_nonexistent_keyword_returns_404(api_client: TestClient) -> None:
    resp = api_client.post(
        "/api/v1/articles",
        json={"keyword_id": 987654, "title": "T", "slug": "ghost-keyword"},
    )

    assert resp.status_code == 404
    _assert_error_shape(resp.json(), "entity_not_found")
    assert api_client.get("/api/v1/articles").json() == []


def test_create_article_invalid_payload_returns_422(api_client: TestClient) -> None:
    # slug 欠落 / title 欠落 / 空文字
    assert api_client.post("/api/v1/articles", json={"title": "T"}).status_code == 422
    assert api_client.post("/api/v1/articles", json={"slug": "s"}).status_code == 422
    assert (
        api_client.post("/api/v1/articles", json={"title": "T", "slug": ""}).status_code == 422
    )


# -- GET detail -------------------------------------------------------------
def test_get_article_returns_200(api_client: TestClient) -> None:
    created = _create_article(api_client, title="取得", slug="get-slug")

    resp = api_client.get(f"/api/v1/articles/{created['id']}")

    assert resp.status_code == 200
    assert resp.json()["slug"] == "get-slug"


def test_get_article_not_found_returns_404(api_client: TestClient) -> None:
    resp = api_client.get("/api/v1/articles/999999")

    assert resp.status_code == 404
    _assert_error_shape(resp.json(), "entity_not_found")


# -- GET list -------------------------------------------------------------
def test_list_articles_pagination(api_client: TestClient) -> None:
    for index in range(5):
        _create_article(api_client, title=f"T{index}", slug=f"slug-{index}")

    resp = api_client.get("/api/v1/articles", params={"limit": 2, "offset": 1})

    assert resp.status_code == 200
    assert [item["slug"] for item in resp.json()] == ["slug-1", "slug-2"]


def test_list_articles_invalid_pagination_returns_422(api_client: TestClient) -> None:
    assert api_client.get("/api/v1/articles", params={"limit": 0}).status_code == 422
    assert api_client.get("/api/v1/articles", params={"limit": 101}).status_code == 422
    assert api_client.get("/api/v1/articles", params={"offset": -1}).status_code == 422


# -- PATCH --------------------------------------------------------------------
def test_patch_article_partial_update(api_client: TestClient) -> None:
    created = _create_article(api_client, title="旧タイトル", slug="old-slug")

    resp = api_client.patch(
        f"/api/v1/articles/{created['id']}", json={"draft_content": "# 下書き"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["draft_content"] == "# 下書き"
    assert body["title"] == "旧タイトル"


def test_patch_article_duplicate_slug_returns_409(api_client: TestClient) -> None:
    _create_article(api_client, title="A", slug="slug-a")
    other = _create_article(api_client, title="B", slug="slug-b")

    resp = api_client.patch(f"/api/v1/articles/{other['id']}", json={"slug": "slug-a"})

    assert resp.status_code == 409
    _assert_error_shape(resp.json(), "duplicate_entity")


def test_patch_article_not_found_returns_404(api_client: TestClient) -> None:
    resp = api_client.patch("/api/v1/articles/999999", json={"title": "x"})

    assert resp.status_code == 404
    _assert_error_shape(resp.json(), "entity_not_found")


# -- DELETE --------------------------------------------------------------------
def test_delete_article_returns_204_without_body(api_client: TestClient) -> None:
    created = _create_article(api_client, title="T", slug="del-slug")

    resp = api_client.delete(f"/api/v1/articles/{created['id']}")

    assert resp.status_code == 204
    assert resp.content == b""
    assert api_client.get(f"/api/v1/articles/{created['id']}").status_code == 404


def test_delete_article_not_found_returns_404(api_client: TestClient) -> None:
    resp = api_client.delete("/api/v1/articles/999999")

    assert resp.status_code == 404
    _assert_error_shape(resp.json(), "entity_not_found")


# -- PATCH /status ----------------------------------------------------------
def test_change_article_status_valid_and_sets_published_at(api_client: TestClient) -> None:
    created = _create_article(api_client, title="T", slug="flow-slug")

    _advance(api_client, created["id"], "planned", "drafting", "review", "approved")
    resp = api_client.patch(
        f"/api/v1/articles/{created['id']}/status", json={"status": "published"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "published"
    assert body["published_at"] is not None


def test_change_article_status_invalid_transition_returns_409(api_client: TestClient) -> None:
    created = _create_article(api_client, title="T", slug="bad-flow")

    resp = api_client.patch(
        f"/api/v1/articles/{created['id']}/status", json={"status": "review"}
    )

    assert resp.status_code == 409
    _assert_error_shape(resp.json(), "invalid_status_transition")
    assert api_client.get(f"/api/v1/articles/{created['id']}").json()["status"] == "idea"


def test_change_article_status_invalid_enum_returns_422(api_client: TestClient) -> None:
    created = _create_article(api_client, title="T", slug="bad-enum")

    resp = api_client.patch(
        f"/api/v1/articles/{created['id']}/status", json={"status": "not-a-status"}
    )

    assert resp.status_code == 422
