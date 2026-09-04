"""app/wordpress/draft_request.py の pure テスト。"""

from __future__ import annotations

import hashlib
import json

from app.wordpress.draft_request import (
    ENDPOINT_PATH,
    METHOD,
    build_wordpress_draft_request,
    canonical_payload_json,
)

_BASE = dict(
    article_id=1,
    source_promotion_id=1,
    title="業務効率化ツールおすすめ｜選び方と目的別に比較",
    content="<p>本文</p>\n<h2>見出し</h2>\n",
    excerpt="メタ説明です。",
    slug="業務効率化-ツール-おすすめ-roundup",
    canonical_body_hash="a" * 64,
    canonical_meta_hash="b" * 64,
    renderer_version="wordpress_html_v1",
    rendered_content_hash="c" * 64,
)


def _b(**over):
    return build_wordpress_draft_request(**{**_BASE, **over})


def test_method_and_endpoint_fixed() -> None:
    r = _b()
    assert r.method == METHOD == "POST"
    assert r.endpoint_path == ENDPOINT_PATH == "/wp-json/wp/v2/posts"


def test_payload_keys_exactly_five_and_status_draft() -> None:
    p = _b().payload_dict()
    assert sorted(p) == ["content", "excerpt", "slug", "status", "title"]
    assert p["status"] == "draft"


def test_no_publish_path_exists() -> None:
    # builder は status を引数に取らない -> publish を組む経路が無い
    for _ in range(3):
        assert _b().payload_dict()["status"] == "draft"


def test_payload_matches_inputs_exactly() -> None:
    p = _b().payload_dict()
    assert p["title"] == _BASE["title"]
    assert p["content"] == _BASE["content"]
    assert p["excerpt"] == _BASE["excerpt"]
    assert p["slug"] == _BASE["slug"]


def test_deterministic_same_inputs_same_hashes() -> None:
    a, b = _b(), _b()
    assert a.payload_hash == b.payload_hash
    assert a.request_identity_hash == b.request_identity_hash
    assert len(a.payload_hash) == 64 and len(a.request_identity_hash) == 64


def test_canonical_json_insertion_order_independent() -> None:
    # payload dict の構築順に依存せず canonical JSON hash は不変
    r = _b()
    reordered = {
        "status": "draft", "slug": _BASE["slug"], "excerpt": _BASE["excerpt"],
        "content": _BASE["content"], "title": _BASE["title"],
    }
    canon = json.dumps(
        reordered, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False,
    )
    assert hashlib.sha256(canon.encode("utf-8")).hexdigest() == r.payload_hash


def test_one_char_title_change_changes_payload_hash() -> None:
    assert _b(title=_BASE["title"] + "X").payload_hash != _b().payload_hash


def test_one_char_content_change_changes_payload_hash() -> None:
    assert _b(content=_BASE["content"] + " ").payload_hash != _b().payload_hash


def test_one_char_excerpt_change_changes_payload_hash() -> None:
    assert _b(excerpt=_BASE["excerpt"] + "。").payload_hash != _b().payload_hash


def test_one_char_slug_change_changes_payload_hash() -> None:
    assert _b(slug=_BASE["slug"] + "-x").payload_hash != _b().payload_hash


def test_identity_hash_depends_on_each_binding_field() -> None:
    base = _b().request_identity_hash
    assert _b(article_id=2).request_identity_hash != base
    assert _b(source_promotion_id=2).request_identity_hash != base
    assert _b(canonical_body_hash="d" * 64).request_identity_hash != base
    assert _b(canonical_meta_hash="d" * 64).request_identity_hash != base
    assert _b(renderer_version="wordpress_html_v2").request_identity_hash != base
    assert _b(rendered_content_hash="d" * 64).request_identity_hash != base
    # payload の変化も identity に伝播 (payload_hash 経由)
    assert _b(title="別タイトル").request_identity_hash != base


def test_japanese_utf8_deterministic() -> None:
    hs = {_b().payload_hash for _ in range(5)}
    assert len(hs) == 1


def test_canonical_payload_json_file_bytes_hash_equals_payload_hash() -> None:
    r = _b()
    cj = canonical_payload_json(r)
    assert hashlib.sha256(cj.encode("utf-8")).hexdigest() == r.payload_hash
    # 末尾改行や BOM が無い exact なエンコーディング
    assert not cj.startswith("﻿")
    assert not cj.endswith("\n")


def test_payload_is_immutable_mapping() -> None:
    r = _b()
    try:
        r.payload["status"] = "publish"  # type: ignore[index]
        raised = False
    except TypeError:
        raised = True
    assert raised
    assert r.payload_dict()["status"] == "draft"
