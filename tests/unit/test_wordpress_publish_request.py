"""app/wordpress/publish_request.py の pure テスト (hashing の決定性 / 感度)。"""

from __future__ import annotations

import hashlib

from app.wordpress.publish_request import (
    METHOD,
    build_publish_payload_json,
    build_wordpress_publish_request,
    compute_publication_request_identity_hash,
    compute_publish_payload_hash,
    compute_target_publication_request_identity_hash,
    endpoint_path_for_post,
)

_BODY_H = "5fc9d713f262daa7660505018f3a784faa23978158e95a380287d1a16513756a"
_META_H = "944797134e7d330040fd2097b7a57de043539df554e7a6a33c7529c977f23e0a"
_RAW_H = "ee300c1dd3728a2a5e247ad0fdc610653e53f1612c1e4029633db8483b2f8f33"
_TARGET = "https://wp.example.test"


def _idkw(**over):
    base = dict(
        article_id=1,
        source_wordpress_draft_run_id=1,
        wordpress_post_id="25",
        method=METHOD,
        endpoint_path="/wp-json/wp/v2/posts/25",
        publish_payload_hash=compute_publish_payload_hash(build_publish_payload_json()),
        canonical_body_hash=_BODY_H,
        canonical_meta_hash=_META_H,
        wordpress_raw_content_hash=_RAW_H,
        expected_pre_publish_status="draft",
    )
    base.update(over)
    return base


def test_publish_payload_json_is_exact_canonical() -> None:
    js = build_publish_payload_json()
    assert js == '{"status":"publish"}'
    assert js.encode("utf-8") == b'{"status":"publish"}'
    assert "\n" not in js and " " not in js


def test_publish_payload_hash_is_deterministic_and_matches_sha256() -> None:
    js = build_publish_payload_json()
    h1 = compute_publish_payload_hash(js)
    h2 = compute_publish_payload_hash(js)
    assert h1 == h2
    assert h1 == hashlib.sha256(b'{"status":"publish"}').hexdigest()
    assert len(h1) == 64


def test_endpoint_path_for_post() -> None:
    assert endpoint_path_for_post("25") == "/wp-json/wp/v2/posts/25"
    assert endpoint_path_for_post(25) == "/wp-json/wp/v2/posts/25"


def test_publication_request_identity_hash_deterministic() -> None:
    a = compute_publication_request_identity_hash(**_idkw())
    b = compute_publication_request_identity_hash(**_idkw())
    assert a == b and len(a) == 64


def test_publication_request_identity_hash_changes_per_component() -> None:
    base = compute_publication_request_identity_hash(**_idkw())
    for field, value in (
        ("article_id", 2),
        ("source_wordpress_draft_run_id", 2),
        ("wordpress_post_id", "26"),
        ("method", "PUT"),
        ("endpoint_path", "/wp-json/wp/v2/posts/26"),
        ("publish_payload_hash", "0" * 64),
        ("canonical_body_hash", "0" * 64),
        ("canonical_meta_hash", "0" * 64),
        ("wordpress_raw_content_hash", "0" * 64),
        ("expected_pre_publish_status", "pending"),
    ):
        assert compute_publication_request_identity_hash(**_idkw(**{field: value})) != base


def test_target_publication_request_identity_hash_binds_target() -> None:
    pri = compute_publication_request_identity_hash(**_idkw())
    a = compute_target_publication_request_identity_hash(
        publication_request_identity_hash=pri, target_base_url=_TARGET
    )
    b = compute_target_publication_request_identity_hash(
        publication_request_identity_hash=pri, target_base_url=_TARGET
    )
    assert a == b and len(a) == 64
    assert (
        compute_target_publication_request_identity_hash(
            publication_request_identity_hash=pri,
            target_base_url="https://other.example",
        )
        != a
    )
    assert (
        compute_target_publication_request_identity_hash(
            publication_request_identity_hash="0" * 64, target_base_url=_TARGET
        )
        != a
    )


def test_build_wordpress_publish_request_wires_everything() -> None:
    pr = build_wordpress_publish_request(
        article_id=1,
        source_wordpress_draft_run_id=1,
        wordpress_post_id="25",
        target_base_url=_TARGET,
        canonical_body_hash=_BODY_H,
        canonical_meta_hash=_META_H,
        wordpress_raw_content_hash=_RAW_H,
    )
    assert pr.method == "POST"
    assert pr.endpoint_path == "/wp-json/wp/v2/posts/25"
    assert pr.publish_payload_json == '{"status":"publish"}'
    assert pr.publish_payload_hash == compute_publish_payload_hash(pr.publish_payload_json)
    assert pr.publication_request_identity_hash == compute_publication_request_identity_hash(
        **_idkw(publish_payload_hash=pr.publish_payload_hash)
    )
    assert (
        pr.target_publication_request_identity_hash
        == compute_target_publication_request_identity_hash(
            publication_request_identity_hash=pr.publication_request_identity_hash,
            target_base_url=_TARGET,
        )
    )
