"""Cross-runtime golden fixtures の内部整合性 + drift 防止。

WordPress (PHP) 側の canonical JSON / hash / HMAC 再計算が一致すべき「正解」。
実 PHP 実行での一致は test_wp_affiliate_runtime_php_harness.py が検証する。
ここは Python 側生成の deterministic 性と on-disk との不一致 (drift) を防ぐ。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.affiliate.projection import build_projection_snapshot
from app.affiliate.projection_signing import body_sha256, compute_signature
from app.article.draft_input_canonical import canonical_json
from scripts.gen_affiliate_projection_fixtures import build_all

_FIX_DIR = (
    Path(__file__).resolve().parents[2]
    / "wordpress"
    / "mu-plugins"
    / "bizfluxlab-affiliate-runtime.fixtures"
)
_SNAPSHOT_NAMES = (
    "empty_snapshot.json",
    "active_v1_snapshot.json",
    "disabled_v2_snapshot.json",
    "two_target_snapshot.json",
    "unicode_path_snapshot.json",
)
_ALL_NAMES = _SNAPSHOT_NAMES + ("canonical_primitives.json", "hmac_vectors.json")


def test_all_fixture_files_exist() -> None:
    for name in _ALL_NAMES:
        assert (_FIX_DIR / name).is_file(), name


def test_on_disk_fixtures_match_regenerated_output() -> None:
    regenerated = build_all()
    assert set(regenerated) == set(_ALL_NAMES)
    for name, payload in regenerated.items():
        on_disk = json.loads((_FIX_DIR / name).read_text(encoding="utf-8"))
        assert on_disk == payload, (
            f"{name} is stale; run scripts/gen_affiliate_projection_fixtures.py"
        )


@pytest.mark.parametrize("name", _SNAPSHOT_NAMES)
def test_snapshot_fixture_internal_hash_consistency(name: str) -> None:
    fx = json.loads((_FIX_DIR / name).read_text(encoding="utf-8"))
    body = fx["request_body"]
    exp = fx["expected"]

    assert body["schema_version"] == 1
    assert body["projection_snapshot_hash"] == exp["projection_snapshot_hash"]
    assert len(body["projection_snapshot_hash"]) == 64
    assert [t["token"] for t in body["targets"]] == sorted(
        t["token"] for t in body["targets"]
    )

    for wire, expect in zip(body["targets"], exp["entries"], strict=True):
        assert wire["token"] == expect["token"]
        assert wire["projection_entry_hash"] == expect["projection_entry_hash"]
        assert (wire["status"], wire["projection_version"]) in {
            ("active", 1),
            ("disabled", 2),
        }
        if wire["status"] == "active":
            assert wire["disabled_at"] is None
        else:
            assert isinstance(wire["disabled_at"], str) and wire["disabled_at"]
        # activated_at is the canonical UTC contract shape
        assert wire["activated_at"].endswith("+00:00")


def test_canonical_primitives_fixture_is_self_consistent() -> None:
    fx = json.loads((_FIX_DIR / "canonical_primitives.json").read_text(encoding="utf-8"))
    names = {c["name"] for c in fx["cases"]}
    # the D-C0.1 correction cases must be present
    assert {"u2028_line_separator", "u2029_paragraph_separator", "cjk_raw_utf8"} <= names
    assert {"empty_object", "empty_list", "key_sort", "nested"} <= names
    for c in fx["cases"]:
        cj = canonical_json(c["value"])
        assert cj == c["canonical_json"], c["name"]
        assert (
            hashlib.sha256(cj.encode("utf-8")).hexdigest() == c["canonical_json_sha256"]
        ), c["name"]
    # U+2028/U+2029 are emitted RAW (not  ) with ensure_ascii=False
    ls = next(c for c in fx["cases"] if c["name"] == "u2028_line_separator")
    assert " " in ls["canonical_json"]
    assert "\\u2028" not in ls["canonical_json"]


def test_hmac_vectors_fixture_is_self_consistent() -> None:
    fx = json.loads((_FIX_DIR / "hmac_vectors.json").read_text(encoding="utf-8"))
    assert fx["max_skew_seconds"] == 300
    secret = fx["shared_secret"]
    assert "synthetic" in secret and "production" not in secret
    for v in fx["vectors"]:
        body = v["body_utf8"].encode("utf-8")
        assert v["body_sha256"] == body_sha256(body)
        assert v["signature"] == compute_signature(
            shared_secret=secret,
            method=v["method"],
            path=v["signed_path"],
            timestamp=v["timestamp"],
            body_sha256_hex=v["body_sha256"],
        )


def test_fixtures_are_synthetic_only() -> None:
    blob = "\n".join((_FIX_DIR / n).read_text(encoding="utf-8") for n in _ALL_NAMES)
    for real in ("a8.net", "moshimo.com", "amazon.", "rakuten.", "bizfluxlab.com"):
        assert real not in blob
    assert "example.test" in blob and "SYNTH1" in blob


def test_empty_snapshot_hash_is_the_known_constant() -> None:
    fx = json.loads((_FIX_DIR / "empty_snapshot.json").read_text(encoding="utf-8"))
    assert (
        fx["expected"]["projection_snapshot_hash"]
        == build_projection_snapshot([]).projection_snapshot_hash
        == "019ac81aeaceee4153c5e477492bb27965210e24764d1ba1cbe50ac67e617b4d"
    )
