"""canonical_request_target — click-export の cursor を署名文字列へ束縛する。"""

from __future__ import annotations

from app.affiliate.projection_signing import (
    PROJECTION_ENDPOINT_METHOD,
    body_sha256,
    canonical_request_target,
    compute_signature,
)

_BASE = "/wp-json/affiliate-ai/v1/outbound-clicks"
_SECRET = "synthetic-test-secret"
_TS = 1_760_000_000
_EMPTY_BODY_SHA = body_sha256(b"")


def test_canonical_request_target_is_key_sorted_and_deterministic() -> None:
    a = canonical_request_target(_BASE, {"since_id": 5, "limit": 100})
    b = canonical_request_target(_BASE, {"limit": 100, "since_id": 5})
    assert a == b == f"{_BASE}?limit=100&since_id=5"


def test_empty_params_returns_base_path() -> None:
    assert canonical_request_target(_BASE, {}) == _BASE


def test_matches_php_reconstruction_shape() -> None:
    # PHP rebuilds exactly: ROUTE . '?limit=' . $limit . '&since_id=' . $since_id
    assert (
        canonical_request_target(_BASE, {"limit": 1000, "since_id": 0})
        == f"{_BASE}?limit=1000&since_id=0"
    )


def _sig(target: str) -> str:
    return compute_signature(
        shared_secret=_SECRET,
        method="GET",
        path=target,
        timestamp=_TS,
        body_sha256_hex=_EMPTY_BODY_SHA,
    )


def test_signature_changes_when_cursor_changes() -> None:
    base = _sig(canonical_request_target(_BASE, {"limit": 100, "since_id": 5}))
    assert base != _sig(canonical_request_target(_BASE, {"limit": 100, "since_id": 6}))
    assert base != _sig(canonical_request_target(_BASE, {"limit": 200, "since_id": 5}))
    # an attacker changing ?since_id= without re-signing cannot match
    assert base == _sig(f"{_BASE}?limit=100&since_id=5")


def test_projection_endpoint_method_is_post() -> None:
    assert PROJECTION_ENDPOINT_METHOD == "POST"
