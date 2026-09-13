"""ArticlePublicationArtifact の凍結 substitution manifest + artifact hash (pure)。

DB / network 非依存。D-D0/D-D0.1 で確定した契約を実装する:

- manifest は **request 側の凍結事実** であり、``occurrence_ordinal`` 昇順で
  canonical 化する。重複 ordinal / 重複 occurrence_identity_hash は拒否する。
- ``replacement_href`` は必ず ``https://bizfluxlab.com/go/{token}`` と厳密一致
  (query/fragment/host/scheme/path のどれか 1 つでもズレたら reject)。
- ``artifact_hash`` は ``(artifact_schema_version, article_id, canonical_body_hash,
  renderer_version, manifest)`` のみの純関数 — 現在の mapping/target/projection
  acknowledgement 状態を一切参照しない (D-D0.1 §5/§6 の historical reproducibility)。
- manifest には ``token`` を含めてよい (公開される値) が、``destination_url`` /
  credential / HMAC 値 / PII は一切含めない。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.affiliate.token import is_well_formed_token
from app.article.draft_input_canonical import canonical_json
from app.article.draft_promotion_canonical import compute_text_hash

ARTIFACT_SCHEMA_VERSION = 1

# 承認された唯一の runtime origin + /go プレフィックス (D-D0 §9)。
GO_BASE_URL = "https://bizfluxlab.com"
GO_PATH_PREFIX = "/go/"

_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")

MANIFEST_ENTRY_FIELDS = (
    "occurrence_ordinal",
    "occurrence_identity_hash",
    "mapping_id",
    "affiliate_link_target_id",
    "token",
    "target_projection_version",
    "original_href",
    "replacement_href",
    "rel_before",
    "rel_after",
)

# manifest エントリに絶対に含めてはいけないキー (defense-in-depth の明示チェック)。
_FORBIDDEN_MANIFEST_KEYS = frozenset(
    {
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
    }
)


class PublicationArtifactError(ValueError):
    """substitution manifest / artifact 生成の入力が不正。メッセージに token/href
    本体は含めない。"""


def is_hex64(value: object) -> bool:
    return isinstance(value, str) and bool(_HEX64.match(value))


def expected_replacement_href(token: str) -> str:
    return f"{GO_BASE_URL}{GO_PATH_PREFIX}{token}"


def _validate_entry(entry: Mapping[str, Any]) -> None:
    keys = set(entry)
    if keys != set(MANIFEST_ENTRY_FIELDS):
        missing = sorted(set(MANIFEST_ENTRY_FIELDS) - keys)
        extra = sorted(keys - set(MANIFEST_ENTRY_FIELDS))
        raise PublicationArtifactError(
            f"manifest entry has an unexpected shape (missing={missing}, extra={extra})"
        )
    if not _FORBIDDEN_MANIFEST_KEYS.isdisjoint(keys):
        raise PublicationArtifactError("manifest entry contains a forbidden field")

    ordinal = entry["occurrence_ordinal"]
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
        raise PublicationArtifactError("occurrence_ordinal must be a non-negative int")

    if not is_hex64(entry["occurrence_identity_hash"]):
        raise PublicationArtifactError("occurrence_identity_hash is not a hex64 hash")

    mapping_id = entry["mapping_id"]
    if isinstance(mapping_id, bool) or not isinstance(mapping_id, int) or mapping_id <= 0:
        raise PublicationArtifactError("mapping_id must be a positive int")

    target_id = entry["affiliate_link_target_id"]
    if isinstance(target_id, bool) or not isinstance(target_id, int) or target_id <= 0:
        raise PublicationArtifactError(
            "affiliate_link_target_id must be a positive int"
        )

    token = entry["token"]
    if not is_well_formed_token(token):
        raise PublicationArtifactError("token is not well-formed")

    version = entry["target_projection_version"]
    if isinstance(version, bool) or version not in (1, 2):
        raise PublicationArtifactError("target_projection_version must be 1 or 2")

    if not isinstance(entry["original_href"], str) or not entry["original_href"]:
        raise PublicationArtifactError("original_href must be a non-empty string")

    replacement_href = entry["replacement_href"]
    if replacement_href != expected_replacement_href(token):
        raise PublicationArtifactError(
            "replacement_href does not exactly match https://bizfluxlab.com/go/{token}"
        )

    for field in ("rel_before", "rel_after"):
        if not isinstance(entry[field], str):
            raise PublicationArtifactError(f"{field} must be a string")


def build_substitution_manifest(
    entries: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """未整列の候補エントリ群 -> ordinal 昇順の canonical manifest。

    重複 ordinal / 重複 occurrence_identity_hash は拒否する。各エントリの形/値も
    検証する (fail closed — 部分的に不正な manifest は絶対に成立させない)。
    """

    materialized = [dict(e) for e in entries]
    for entry in materialized:
        _validate_entry(entry)

    materialized.sort(key=lambda e: e["occurrence_ordinal"])

    ordinals = [e["occurrence_ordinal"] for e in materialized]
    if len(ordinals) != len(set(ordinals)):
        raise PublicationArtifactError("duplicate occurrence_ordinal in manifest")

    identity_hashes = [e["occurrence_identity_hash"] for e in materialized]
    if len(identity_hashes) != len(set(identity_hashes)):
        raise PublicationArtifactError(
            "duplicate occurrence_identity_hash in manifest"
        )

    return materialized


def serialize_manifest(entries: Sequence[Mapping[str, Any]]) -> str:
    """canonical JSON 文字列 (呼び出し側が既に ordinal でソート済みの前提)。"""

    return canonical_json(list(entries))


def parse_manifest(manifest_json: str) -> list[dict[str, Any]]:
    data = json.loads(manifest_json)
    if not isinstance(data, list):
        raise PublicationArtifactError("stored substitution_manifest_json is not a JSON array")
    for entry in data:
        if not isinstance(entry, dict):
            raise PublicationArtifactError("stored manifest entry is not an object")
        _validate_entry(entry)
    return data


def compute_artifact_hash(
    *,
    artifact_schema_version: int,
    article_id: int,
    canonical_body_hash: str,
    renderer_version: str,
    manifest: Sequence[Mapping[str, Any]],
) -> str:
    """historical に再現可能な artifact identity。現在の mutable な control-plane
    状態 (mapping.status / target.status / projection acknowledgement) は一切
    参照しない — 引数はすべて呼び出し側が既に凍結した値。
    """

    payload = {
        "artifact_schema_version": artifact_schema_version,
        "article_id": article_id,
        "canonical_body_hash": canonical_body_hash,
        "renderer_version": renderer_version,
        "substitution_manifest": list(manifest),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def compute_tracked_html_hash(tracked_html: str) -> str:
    """既存の text-hash 規約 (``compute_text_hash``) をそのまま再利用する。"""

    return compute_text_hash(tracked_html)
