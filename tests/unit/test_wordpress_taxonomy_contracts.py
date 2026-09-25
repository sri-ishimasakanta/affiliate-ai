"""W2: WordPressClient のカテゴリの書き込みの exact 契約 (httpx.MockTransport。通信しない)。

- カテゴリの作成は ``POST /categories`` を 1 回だけ、body はちょうど ``{"name","slug","parent"}``。
  説明文・meta などは送れない。slug は ASCII、親は指定の ID でなければ送らない。
- 記事のカテゴリは ``POST /posts/{id}`` を 1 回だけ、body はちょうど ``{"categories": [...]}``。
  本文・タイトル・slug・状態・タグなどは送れない。空・重複・0 以下は送らない。
- 通信が途中で切れたら「結果が不明」 (再送しない)。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.config.settings import Settings
from app.exceptions import WordPressAmbiguousOutcomeError
from app.wordpress.client import WordPressClient

_BASE = "https://wp.example.test"


def _client(handler) -> WordPressClient:
    settings = Settings(
        _env_file=None,
        wordpress_base_url=_BASE,
        wordpress_username="u",
        wordpress_app_password="p",
    )
    return WordPressClient(settings, transport=httpx.MockTransport(handler))


def _never(request):  # pragma: no cover - must not be called
    raise AssertionError("no request may be sent")


def test_create_category_sends_the_exact_body_once() -> None:
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            201,
            json={
                "id": 50,
                "name": "AI・生成AI",
                "slug": "ai-generative-ai",
                "parent": 4,
                "count": 0,
            },
        )

    payload = json.dumps(
        {"name": "AI・生成AI", "slug": "ai-generative-ai", "parent": 4}, ensure_ascii=False
    )
    created = _client(handler).create_category_exact(payload, expected_parent_id=4)
    assert created["id"] == 50
    assert len(seen) == 1 and seen[0].method == "POST"
    assert seen[0].url.path == "/wp-json/wp/v2/categories"
    assert seen[0].content == payload.encode("utf-8")


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "AI・生成AI", "slug": "ai-generative-ai", "parent": 4, "description": "x"},
        {"name": "AI・生成AI", "slug": "ai-generative-ai", "parent": 4, "meta": {}},
        {"name": "AI・生成AI", "slug": "ai-generative-ai"},
        {"name": "", "slug": "ai-generative-ai", "parent": 4},
        {"name": "AI・生成AI", "slug": "", "parent": 4},
        {"name": "AI・生成AI", "slug": "生成ai", "parent": 4},
        {"name": "AI・生成AI", "slug": "AI-Generative", "parent": 4},
        {"name": "AI・生成AI", "slug": "ai-generative-ai", "parent": 1},
        {"name": "AI・生成AI", "slug": "ai-generative-ai", "parent": True},
    ],
)
def test_create_category_refuses_anything_but_the_exact_payload(payload) -> None:
    with pytest.raises(ValueError):
        _client(_never).create_category_exact(
            json.dumps(payload, ensure_ascii=False), expected_parent_id=4
        )


def test_set_post_categories_sends_the_exact_body_once() -> None:
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"id": 64, "categories": [4, 55]})

    payload = json.dumps({"categories": [4, 55]})
    _client(handler).set_post_categories_exact(64, payload)
    assert len(seen) == 1 and seen[0].method == "POST"
    assert seen[0].url.path == "/wp-json/wp/v2/posts/64"
    assert seen[0].content == payload.encode("utf-8")


@pytest.mark.parametrize(
    "payload",
    [
        {"categories": [4, 55], "title": "x"},
        {"categories": [4, 55], "content": "x"},
        {"categories": [4, 55], "slug": "x"},
        {"categories": [4, 55], "status": "draft"},
        {"categories": [4, 55], "tags": [1]},
        {"categories": [4, 55], "excerpt": "x"},
        {"categories": [4, 55], "date": "2026-01-01"},
        {"categories": []},
        {"categories": [4, 4]},
        {"categories": [4, 0]},
        {"categories": [4, "55"]},
        {"categories": [4, True]},
        {"tags": [4]},
    ],
)
def test_set_post_categories_refuses_anything_but_categories(payload) -> None:
    with pytest.raises(ValueError):
        _client(_never).set_post_categories_exact(64, json.dumps(payload))


@pytest.mark.parametrize("method", ["create", "set"])
def test_a_timeout_is_an_ambiguous_outcome_and_is_not_retried(method) -> None:
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("slow", request=request)

    client = _client(handler)
    with pytest.raises(WordPressAmbiguousOutcomeError):
        if method == "create":
            client.create_category_exact(
                json.dumps({"name": "n", "slug": "s", "parent": 4}), expected_parent_id=4
            )
        else:
            client.set_post_categories_exact(64, json.dumps({"categories": [4, 55]}))
    assert len(calls) == 1
