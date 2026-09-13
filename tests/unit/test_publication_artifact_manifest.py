"""app.wordpress.publication_artifact — manifest 構築 / artifact_hash / 純粋関数 (DB非依存)。"""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from app.wordpress.publication_artifact import (
    ARTIFACT_SCHEMA_VERSION,
    GO_BASE_URL,
    MANIFEST_ENTRY_FIELDS,
    PublicationArtifactError,
    build_substitution_manifest,
    compute_artifact_hash,
    compute_tracked_html_hash,
    expected_replacement_href,
    is_hex64,
    parse_manifest,
    serialize_manifest,
)

_TOKEN_A = "AAAAAAAAAAAAAAAAAAAAAA"  # 22 文字, token_urlsafe(16) と同じ長さ
_TOKEN_B = "BBBBBBBBBBBBBBBBBBBBBB"


def _entry(
    *,
    ordinal: int = 0,
    identity_hash: str = "a" * 64,
    mapping_id: int = 1,
    target_id: int = 1,
    token: str = _TOKEN_A,
    version: int = 1,
    original_href: str = "https://official.example.test/x",
    replacement_href: str | None = None,
) -> dict:
    return {
        "occurrence_ordinal": ordinal,
        "occurrence_identity_hash": identity_hash,
        "mapping_id": mapping_id,
        "affiliate_link_target_id": target_id,
        "token": token,
        "target_projection_version": version,
        "original_href": original_href,
        "replacement_href": replacement_href or expected_replacement_href(token),
        "rel_before": "nofollow",
        "rel_after": "sponsored nofollow",
    }


# ==================== is_hex64 / expected_replacement_href ===============
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a" * 64, True),
        ("A" * 64, False),  # 大文字は不可
        ("a" * 63, False),
        ("a" * 65, False),
        ("g" * 64, False),
        ("", False),
        (None, False),
        (12345, False),
    ],
)
def test_is_hex64(value, expected) -> None:
    assert is_hex64(value) is expected


def test_expected_replacement_href_exact_format() -> None:
    assert expected_replacement_href("tok123") == f"{GO_BASE_URL}/go/tok123"


# ==================== build_substitution_manifest =========================
def test_manifest_entry_field_set_matches_contract() -> None:
    entry = _entry()
    assert set(entry) == set(MANIFEST_ENTRY_FIELDS)


def test_build_manifest_sorts_by_ordinal() -> None:
    entries = [
        _entry(ordinal=2, identity_hash="c" * 64, mapping_id=3, token=_TOKEN_A),
        _entry(ordinal=0, identity_hash="a" * 64, mapping_id=1, token=_TOKEN_A),
        _entry(ordinal=1, identity_hash="b" * 64, mapping_id=2, token=_TOKEN_A),
    ]
    manifest = build_substitution_manifest(entries)
    assert [e["occurrence_ordinal"] for e in manifest] == [0, 1, 2]


def test_build_manifest_rejects_duplicate_ordinal() -> None:
    entries = [
        _entry(ordinal=0, identity_hash="a" * 64, mapping_id=1),
        _entry(ordinal=0, identity_hash="b" * 64, mapping_id=2),
    ]
    with pytest.raises(PublicationArtifactError, match="duplicate occurrence_ordinal"):
        build_substitution_manifest(entries)


def test_build_manifest_rejects_duplicate_occurrence_identity_hash() -> None:
    entries = [
        _entry(ordinal=0, identity_hash="a" * 64, mapping_id=1),
        _entry(ordinal=1, identity_hash="a" * 64, mapping_id=2),
    ]
    with pytest.raises(
        PublicationArtifactError, match="duplicate occurrence_identity_hash"
    ):
        build_substitution_manifest(entries)


def test_build_manifest_empty_list_ok() -> None:
    assert build_substitution_manifest([]) == []


@pytest.mark.parametrize("missing_field", MANIFEST_ENTRY_FIELDS)
def test_build_manifest_rejects_missing_field(missing_field) -> None:
    entry = _entry()
    del entry[missing_field]
    with pytest.raises(PublicationArtifactError, match="unexpected shape"):
        build_substitution_manifest([entry])


def test_build_manifest_rejects_extra_field() -> None:
    entry = _entry()
    entry["extra_field"] = "x"
    with pytest.raises(PublicationArtifactError, match="unexpected shape"):
        build_substitution_manifest([entry])


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "destination_url",
        "destination",
        "destination_host",
        "shared_secret",
        "secret",
        "signature",
        "headers",
        "ip",
        "user_agent",
        "email",
    ],
)
def test_build_manifest_rejects_forbidden_keys(forbidden_key) -> None:
    """禁止キーを追加した entry は常に拒否される。10 フィールド contract は
    forbidden keys と重複しないため、追加された禁止キーは shape チェック
    (unexpected extra field) の側で捕捉される — それでも manifest には
    絶対に混入しないことが検証できていれば十分。"""

    entry = _entry()
    entry[forbidden_key] = "leak"
    with pytest.raises(PublicationArtifactError, match="unexpected shape"):
        build_substitution_manifest([entry])


def test_build_manifest_rejects_bad_occurrence_identity_hash() -> None:
    entry = _entry(identity_hash="not-a-hash")
    with pytest.raises(PublicationArtifactError, match="hex64"):
        build_substitution_manifest([entry])


@pytest.mark.parametrize("bad_ordinal", [-1, "0", 1.5, True])
def test_build_manifest_rejects_bad_ordinal_type(bad_ordinal) -> None:
    entry = _entry()
    entry["occurrence_ordinal"] = bad_ordinal
    with pytest.raises(PublicationArtifactError, match="occurrence_ordinal"):
        build_substitution_manifest([entry])


@pytest.mark.parametrize("bad_id", [0, -1, "1", True])
def test_build_manifest_rejects_bad_mapping_id(bad_id) -> None:
    entry = _entry()
    entry["mapping_id"] = bad_id
    with pytest.raises(PublicationArtifactError, match="mapping_id"):
        build_substitution_manifest([entry])


@pytest.mark.parametrize("bad_id", [0, -1, "1", True])
def test_build_manifest_rejects_bad_target_id(bad_id) -> None:
    entry = _entry()
    entry["affiliate_link_target_id"] = bad_id
    with pytest.raises(PublicationArtifactError, match="affiliate_link_target_id"):
        build_substitution_manifest([entry])


def test_build_manifest_rejects_malformed_token() -> None:
    entry = _entry()
    entry["token"] = "short"
    entry["replacement_href"] = expected_replacement_href("short")
    with pytest.raises(PublicationArtifactError, match="token is not well-formed"):
        build_substitution_manifest([entry])


@pytest.mark.parametrize("bad_version", [0, 3, "1", True, None])
def test_build_manifest_rejects_bad_projection_version(bad_version) -> None:
    entry = _entry()
    entry["target_projection_version"] = bad_version
    with pytest.raises(
        PublicationArtifactError, match="target_projection_version"
    ):
        build_substitution_manifest([entry])


def test_build_manifest_rejects_empty_original_href() -> None:
    entry = _entry(original_href="")
    with pytest.raises(PublicationArtifactError, match="original_href"):
        build_substitution_manifest([entry])


# ==================== replacement_href exact-match validation =============
@pytest.mark.parametrize(
    "bad_replacement",
    [
        f"{GO_BASE_URL}/go/{_TOKEN_A}?ref=1",  # query 付与は拒否
        f"{GO_BASE_URL}/go/{_TOKEN_A}#frag",  # fragment 付与は拒否
        f"http://bizfluxlab.com/go/{_TOKEN_A}",  # scheme 違い (https でない)
        f"https://evil.example.test/go/{_TOKEN_A}",  # host 違い
        f"{GO_BASE_URL}/go2/{_TOKEN_A}",  # path prefix 違い
        f"{GO_BASE_URL}/go/{_TOKEN_B}",  # token 不一致 (別 entry の token)
        f"{GO_BASE_URL}/go/{_TOKEN_A}/",  # 末尾スラッシュ付与
        _TOKEN_A,  # 完全に別物
    ],
)
def test_build_manifest_rejects_replacement_href_mismatch(bad_replacement) -> None:
    entry = _entry(replacement_href=bad_replacement)
    with pytest.raises(PublicationArtifactError, match="replacement_href"):
        build_substitution_manifest([entry])


# ==================== serialize/parse round-trip ===========================
def test_serialize_parse_round_trip() -> None:
    entries = build_substitution_manifest(
        [
            _entry(ordinal=1, identity_hash="b" * 64, mapping_id=2, token=_TOKEN_B),
            _entry(ordinal=0, identity_hash="a" * 64, mapping_id=1, token=_TOKEN_A),
        ]
    )
    blob = serialize_manifest(entries)
    assert isinstance(blob, str)
    parsed = parse_manifest(blob)
    assert parsed == entries


def test_serialize_manifest_is_canonical_json_deterministic() -> None:
    entries = build_substitution_manifest([_entry()])
    assert serialize_manifest(entries) == serialize_manifest(copy.deepcopy(entries))


def test_parse_manifest_rejects_non_array() -> None:
    with pytest.raises(PublicationArtifactError, match="JSON array"):
        parse_manifest('{"not": "an array"}')


def test_parse_manifest_rejects_non_object_entry() -> None:
    with pytest.raises(PublicationArtifactError, match="not an object"):
        parse_manifest("[1, 2, 3]")


def test_parse_manifest_rejects_invalid_entry_shape() -> None:
    bad_entry = _entry()
    del bad_entry["token"]
    blob = serialize_manifest([bad_entry])
    with pytest.raises(PublicationArtifactError, match="unexpected shape"):
        parse_manifest(blob)


# ==================== compute_artifact_hash ================================
def _hash_kwargs(**overrides) -> dict:
    base = {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "article_id": 1,
        "canonical_body_hash": "d" * 64,
        "renderer_version": "v1",
        "manifest": build_substitution_manifest([_entry()]),
    }
    base.update(overrides)
    return base


def test_compute_artifact_hash_is_hex64() -> None:
    h = compute_artifact_hash(**_hash_kwargs())
    assert is_hex64(h)


def test_compute_artifact_hash_deterministic_for_same_inputs() -> None:
    kwargs = _hash_kwargs()
    assert compute_artifact_hash(**kwargs) == compute_artifact_hash(**copy.deepcopy(kwargs))


def test_compute_artifact_hash_deterministic_regardless_of_manifest_list_identity() -> None:
    manifest = build_substitution_manifest([_entry()])
    h1 = compute_artifact_hash(**_hash_kwargs(manifest=manifest))
    h2 = compute_artifact_hash(**_hash_kwargs(manifest=list(manifest)))
    assert h1 == h2


@pytest.mark.parametrize(
    "field",
    ["artifact_schema_version", "article_id", "canonical_body_hash", "renderer_version"],
)
def test_compute_artifact_hash_changes_when_scalar_input_changes(field) -> None:
    base = compute_artifact_hash(**_hash_kwargs())
    changed_value = {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION + 1,
        "article_id": 2,
        "canonical_body_hash": "e" * 64,
        "renderer_version": "v2",
    }[field]
    changed = compute_artifact_hash(**_hash_kwargs(**{field: changed_value}))
    assert base != changed


def test_compute_artifact_hash_changes_when_manifest_changes() -> None:
    base = compute_artifact_hash(**_hash_kwargs())
    other_manifest = build_substitution_manifest(
        [_entry(identity_hash="f" * 64, mapping_id=99, token=_TOKEN_B)]
    )
    changed = compute_artifact_hash(**_hash_kwargs(manifest=other_manifest))
    assert base != changed


def test_compute_artifact_hash_is_a_pure_function_of_arguments_only() -> None:
    """artifact_hash は与えられた引数のみの関数 — mapping/target/projection の
    現在状態を一切参照しない (D-D0.1 §5/§6)。ここでは同じ引数を 2 回呼んでも
    グローバル状態に依存せず同じ結果になることのみを確認する (pure module には
    グローバルな可変状態自体が存在しない)。"""

    kwargs = _hash_kwargs()
    results = {compute_artifact_hash(**kwargs) for _ in range(5)}
    assert len(results) == 1


# ==================== D-D0.1 hash contract: golden vector ==================
# 承認された payload key set (D-D0.1):
#   artifact_schema_version / article_id / canonical_body_hash /
#   renderer_version / substitution_manifest
# 省略形 (schema_version / manifest) は一切許可されない。このセクションは
# 固定 fixture に対する既知の sha256 を hardcode し、将来の accidental な
# key rename が artifact identity を黙って変えてしまうことを防ぐ (D-D1.1)。

_GOLDEN_ENTRY = {
    "occurrence_ordinal": 0,
    "occurrence_identity_hash": "a" * 64,
    "mapping_id": 1,
    "affiliate_link_target_id": 1,
    "token": "GOLDENVECTORTOKEN00001",
    "target_projection_version": 1,
    "original_href": "https://official.example.test/golden",
    "replacement_href": "https://bizfluxlab.com/go/GOLDENVECTORTOKEN00001",
    "rel_before": "nofollow",
    "rel_after": "sponsored nofollow",
}

_GOLDEN_HASH_KWARGS = {
    "artifact_schema_version": 1,
    "article_id": 42,
    "canonical_body_hash": "b" * 64,
    "renderer_version": "golden-renderer-v1",
}

# 承認された 5 キーちょうどで手動構築した canonical JSON の sha256
# (json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"),
# allow_nan=False) と同一の canonicalization を用いて独立に算出・固定した値)。
_GOLDEN_ARTIFACT_HASH = "d50ebc9e6ba41c8a710fff90f2cbfc4c88a1e35e7e513bcac07820c036033b97"


def test_artifact_hash_golden_vector_matches_fixed_synthetic_fixture() -> None:
    manifest = build_substitution_manifest([_GOLDEN_ENTRY])
    actual = compute_artifact_hash(manifest=manifest, **_GOLDEN_HASH_KWARGS)
    assert actual == _GOLDEN_ARTIFACT_HASH


def test_artifact_hash_golden_vector_exact_approved_key_set() -> None:
    """approved contract の key set を明示的に固定する (documentation-level pin)。
    ここで構築した payload を手動で canonical_json 化した sha256 が golden vector
    と一致することで、実装が別名/省略形のキーを一切使っていないと証明する。"""

    manifest = build_substitution_manifest([_GOLDEN_ENTRY])
    approved_payload = {
        "artifact_schema_version": _GOLDEN_HASH_KWARGS["artifact_schema_version"],
        "article_id": _GOLDEN_HASH_KWARGS["article_id"],
        "canonical_body_hash": _GOLDEN_HASH_KWARGS["canonical_body_hash"],
        "renderer_version": _GOLDEN_HASH_KWARGS["renderer_version"],
        "substitution_manifest": manifest,
    }
    assert set(approved_payload) == {
        "artifact_schema_version",
        "article_id",
        "canonical_body_hash",
        "renderer_version",
        "substitution_manifest",
    }
    canonical = json.dumps(
        approved_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    hand_built_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert hand_built_hash == _GOLDEN_ARTIFACT_HASH

    actual = compute_artifact_hash(manifest=manifest, **_GOLDEN_HASH_KWARGS)
    assert actual == hand_built_hash


@pytest.mark.parametrize(
    "wrong_payload",
    [
        # schema_version (省略形) を使う不承認の variant。
        {"key": "schema_version", "other_key": "substitution_manifest"},
        # manifest (省略形) を使う不承認の variant。
        {"key": "artifact_schema_version", "other_key": "manifest"},
    ],
)
def test_wrong_key_names_do_not_match_the_approved_golden_hash(wrong_payload) -> None:
    """schema_version / manifest のような省略形キーで構築した payload は、承認済み
    golden vector と異なる hash になる (=実装がこれらの別名を使っていないことの
    反証可能な証拠)。実装がこの形をサポートする必要はない — 一致しないことのみ確認する。"""

    manifest = build_substitution_manifest([_GOLDEN_ENTRY])
    bad_payload = {
        wrong_payload["key"]: _GOLDEN_HASH_KWARGS["artifact_schema_version"],
        "article_id": _GOLDEN_HASH_KWARGS["article_id"],
        "canonical_body_hash": _GOLDEN_HASH_KWARGS["canonical_body_hash"],
        "renderer_version": _GOLDEN_HASH_KWARGS["renderer_version"],
        wrong_payload["other_key"]: manifest,
    }
    canonical = json.dumps(
        bad_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    bad_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    assert bad_hash != _GOLDEN_ARTIFACT_HASH
    actual = compute_artifact_hash(manifest=manifest, **_GOLDEN_HASH_KWARGS)
    assert actual != bad_hash
    assert actual == _GOLDEN_ARTIFACT_HASH


# ==================== compute_tracked_html_hash =============================
def test_compute_tracked_html_hash_is_hex64_and_deterministic() -> None:
    html = "<p>hello <a href='https://bizfluxlab.com/go/tok'>world</a></p>"
    h1 = compute_tracked_html_hash(html)
    h2 = compute_tracked_html_hash(html)
    assert is_hex64(h1)
    assert h1 == h2


def test_compute_tracked_html_hash_changes_with_content() -> None:
    a = compute_tracked_html_hash("<p>a</p>")
    b = compute_tracked_html_hash("<p>b</p>")
    assert a != b
