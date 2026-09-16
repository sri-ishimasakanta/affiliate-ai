"""app.wordpress.link_occurrence — occurrence_identity_hash 純関数 (D-D2)。"""

from __future__ import annotations

import hashlib
import json

import pytest

from app.wordpress.link_occurrence import (
    OCCURRENCE_SCHEMA_VERSION,
    compute_occurrence_identity_hash,
    extract_original_host,
)

_BASE_KWARGS = {
    "canonical_body_hash": "b" * 64,
    "renderer_version": "wordpress_html_v1",
    "occurrence_ordinal": 3,
    "original_href": "https://official.example.test/golden",
}

_GOLDEN_IDENTITY_HASH = (
    "7d836a0948f333ad9d5ca3db7d369fdfe9d926a72a77715e1e8d6be3f0a2acfa"
)


def test_same_inputs_produce_same_identity() -> None:
    a = compute_occurrence_identity_hash(**_BASE_KWARGS)
    b = compute_occurrence_identity_hash(**_BASE_KWARGS)
    assert a == b


def test_body_hash_change_changes_identity() -> None:
    base = compute_occurrence_identity_hash(**_BASE_KWARGS)
    changed = compute_occurrence_identity_hash(
        **{**_BASE_KWARGS, "canonical_body_hash": "c" * 64}
    )
    assert base != changed


def test_renderer_version_change_changes_identity() -> None:
    base = compute_occurrence_identity_hash(**_BASE_KWARGS)
    changed = compute_occurrence_identity_hash(
        **{**_BASE_KWARGS, "renderer_version": "wordpress_html_v2"}
    )
    assert base != changed


def test_schema_version_change_changes_identity() -> None:
    base = compute_occurrence_identity_hash(**_BASE_KWARGS)
    changed = compute_occurrence_identity_hash(
        **_BASE_KWARGS, occurrence_schema_version=OCCURRENCE_SCHEMA_VERSION + 1
    )
    assert base != changed


def test_ordinal_change_changes_identity() -> None:
    base = compute_occurrence_identity_hash(**_BASE_KWARGS)
    changed = compute_occurrence_identity_hash(
        **{**_BASE_KWARGS, "occurrence_ordinal": 4}
    )
    assert base != changed


def test_original_href_change_changes_identity() -> None:
    base = compute_occurrence_identity_hash(**_BASE_KWARGS)
    changed = compute_occurrence_identity_hash(
        **{**_BASE_KWARGS, "original_href": "https://official.example.test/other"}
    )
    assert base != changed


def test_same_href_twice_at_different_ordinals_produces_different_identities() -> None:
    href = "https://official.example.test/x"
    at_3 = compute_occurrence_identity_hash(
        canonical_body_hash="b" * 64, renderer_version="wordpress_html_v1",
        occurrence_ordinal=3, original_href=href,
    )
    at_7 = compute_occurrence_identity_hash(
        canonical_body_hash="b" * 64, renderer_version="wordpress_html_v1",
        occurrence_ordinal=7, original_href=href,
    )
    assert at_3 != at_7


# ==================== golden vector =========================================
def test_golden_identity_vector_exact_key_set_and_hash() -> None:
    """承認された payload key set:
    occurrence_schema_version / canonical_body_hash / renderer_version /
    occurrence_ordinal / original_href。別名・省略形は一切許可されない。"""

    approved_payload = {
        "occurrence_schema_version": OCCURRENCE_SCHEMA_VERSION,
        "canonical_body_hash": _BASE_KWARGS["canonical_body_hash"],
        "renderer_version": _BASE_KWARGS["renderer_version"],
        "occurrence_ordinal": _BASE_KWARGS["occurrence_ordinal"],
        "original_href": _BASE_KWARGS["original_href"],
    }
    assert set(approved_payload) == {
        "occurrence_schema_version",
        "canonical_body_hash",
        "renderer_version",
        "occurrence_ordinal",
        "original_href",
    }
    canonical = json.dumps(
        approved_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    hand_built_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert hand_built_hash == _GOLDEN_IDENTITY_HASH

    actual = compute_occurrence_identity_hash(**_BASE_KWARGS)
    assert actual == hand_built_hash
    assert actual == _GOLDEN_IDENTITY_HASH


@pytest.mark.parametrize(
    "wrong_payload",
    [
        {"key": "schema_version", "value_key": "occurrence_schema_version"},
        {"key": "ordinal", "value_key": "occurrence_ordinal"},
    ],
)
def test_wrong_key_names_do_not_match_the_golden_hash(wrong_payload) -> None:
    bad_payload = {
        "canonical_body_hash": _BASE_KWARGS["canonical_body_hash"],
        "renderer_version": _BASE_KWARGS["renderer_version"],
        "original_href": _BASE_KWARGS["original_href"],
        wrong_payload["key"]: _BASE_KWARGS.get(
            wrong_payload["value_key"], OCCURRENCE_SCHEMA_VERSION
        ),
    }
    # 承認されていないキー名を含むため元の 5 キー contract とは異なる形になる。
    canonical = json.dumps(
        bad_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )
    bad_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert bad_hash != _GOLDEN_IDENTITY_HASH


# ==================== extract_original_host =================================
@pytest.mark.parametrize(
    ("href", "expected_host"),
    [
        ("https://Official.Example.TEST/x", "official.example.test"),
        ("https://official.example.test:8443/x", "official.example.test"),
        ("http://sub.example.test/y?a=1", "sub.example.test"),
        ("not-a-url", None),
    ],
)
def test_extract_original_host(href, expected_host) -> None:
    assert extract_original_host(href) == expected_host


def test_extract_original_host_does_not_mutate_caller_string() -> None:
    href = "https://Official.Example.TEST/Tool-A?x=1"
    host = extract_original_host(href)
    assert host == "official.example.test"
    assert href == "https://Official.Example.TEST/Tool-A?x=1"  # 無改変
