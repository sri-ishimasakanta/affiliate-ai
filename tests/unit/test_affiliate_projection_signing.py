"""app/affiliate/projection_signing.py — HMAC-SHA256 request signing。"""

from __future__ import annotations

import hashlib

import pytest

from app.affiliate.projection_signing import (
    DEFAULT_MAX_SKEW_SECONDS,
    PROJECTION_ENDPOINT_METHOD,
    PROJECTION_ENDPOINT_PATH,
    body_sha256,
    build_signing_string,
    compute_signature,
    timestamp_within_window,
    verify_request,
    verify_signature,
)

_SECRET = "test-shared-secret-not-a-real-one"
_PATH = PROJECTION_ENDPOINT_PATH
_BODY = b'{"schema_version":1,"targets":[]}'
_TS = 1_760_000_000


def _sig(**kw):
    base = dict(
        shared_secret=_SECRET,
        method=PROJECTION_ENDPOINT_METHOD,
        path=_PATH,
        timestamp=_TS,
        body_sha256_hex=body_sha256(_BODY),
    )
    base.update(kw)
    return compute_signature(**base)


def test_body_sha256_is_exact_bytes() -> None:
    assert body_sha256(b"abc") == hashlib.sha256(b"abc").hexdigest()
    assert body_sha256(b"abc") != body_sha256(b"abc ")
    assert body_sha256(bytearray(b"xy")) == hashlib.sha256(b"xy").hexdigest()


def test_body_sha256_rejects_non_bytes() -> None:
    with pytest.raises(TypeError):
        body_sha256("not bytes")  # type: ignore[arg-type]


def test_signing_string_binds_all_fields() -> None:
    s = build_signing_string(
        method="post", path=_PATH, timestamp=_TS, body_sha256_hex="ABCD"
    )
    assert s.split("\n") == ["v1", "POST", _PATH, str(_TS), "abcd"]


def test_signature_is_deterministic() -> None:
    assert _sig() == _sig()
    assert len(_sig()) == 64


def test_signature_changes_with_each_signed_field() -> None:
    base = _sig()
    assert _sig(body_sha256_hex=body_sha256(b"different body")) != base
    assert _sig(timestamp=_TS + 1) != base
    assert _sig(path=_PATH + "x") != base
    assert _sig(method="GET") != base
    assert _sig(shared_secret="other-secret") != base


def test_verify_signature_constant_time_true_false() -> None:
    good = _sig()
    assert verify_signature(
        shared_secret=_SECRET,
        method=PROJECTION_ENDPOINT_METHOD,
        path=_PATH,
        timestamp=_TS,
        body_sha256_hex=body_sha256(_BODY),
        provided_signature=good,
    )
    # 大文字 hex も許容 (lowercase 正規化)
    assert verify_signature(
        shared_secret=_SECRET,
        method=PROJECTION_ENDPOINT_METHOD,
        path=_PATH,
        timestamp=_TS,
        body_sha256_hex=body_sha256(_BODY),
        provided_signature=good.upper(),
    )
    assert not verify_signature(
        shared_secret="wrong",
        method=PROJECTION_ENDPOINT_METHOD,
        path=_PATH,
        timestamp=_TS,
        body_sha256_hex=body_sha256(_BODY),
        provided_signature=good,
    )
    assert not verify_signature(
        shared_secret=_SECRET,
        method=PROJECTION_ENDPOINT_METHOD,
        path=_PATH,
        timestamp=_TS,
        body_sha256_hex=body_sha256(_BODY),
        provided_signature="",
    )


@pytest.mark.parametrize(
    ("delta", "ok"),
    [
        (0, True),
        (DEFAULT_MAX_SKEW_SECONDS, True),
        (-DEFAULT_MAX_SKEW_SECONDS, True),
        (DEFAULT_MAX_SKEW_SECONDS + 1, False),
        (-(DEFAULT_MAX_SKEW_SECONDS + 1), False),
    ],
)
def test_timestamp_window(delta: int, ok: bool) -> None:
    assert timestamp_within_window(timestamp=_TS + delta, now=_TS) is ok


def test_verify_request_happy_path() -> None:
    ok, reason = verify_request(
        shared_secret=_SECRET,
        method=PROJECTION_ENDPOINT_METHOD,
        path=_PATH,
        timestamp=_TS,
        now=_TS + 10,
        body=_BODY,
        provided_content_sha256=body_sha256(_BODY),
        provided_signature=_sig(),
    )
    assert ok and reason == "ok"


def test_verify_request_content_hash_mismatch() -> None:
    ok, reason = verify_request(
        shared_secret=_SECRET,
        method=PROJECTION_ENDPOINT_METHOD,
        path=_PATH,
        timestamp=_TS,
        now=_TS,
        body=_BODY,
        provided_content_sha256=body_sha256(b"tampered"),
        provided_signature=_sig(),
    )
    assert not ok and reason == "content_sha256_mismatch"


def test_verify_request_timestamp_outside_window() -> None:
    ok, reason = verify_request(
        shared_secret=_SECRET,
        method=PROJECTION_ENDPOINT_METHOD,
        path=_PATH,
        timestamp=_TS,
        now=_TS + DEFAULT_MAX_SKEW_SECONDS + 5,
        body=_BODY,
        provided_content_sha256=body_sha256(_BODY),
        provided_signature=_sig(),
    )
    assert not ok and reason == "timestamp_outside_window"


def test_verify_request_signature_mismatch() -> None:
    ok, reason = verify_request(
        shared_secret=_SECRET,
        method=PROJECTION_ENDPOINT_METHOD,
        path=_PATH,
        timestamp=_TS,
        now=_TS,
        body=_BODY,
        provided_content_sha256=body_sha256(_BODY),
        provided_signature="0" * 64,
    )
    assert not ok and reason == "signature_mismatch"


def test_replay_inside_window_still_verifies_projection_layer_makes_it_harmless() -> None:
    # 署名層は「窓内の再送」を通す。無害化は projection idempotency の責務。
    args = dict(
        shared_secret=_SECRET,
        method=PROJECTION_ENDPOINT_METHOD,
        path=_PATH,
        timestamp=_TS,
        body=_BODY,
        provided_content_sha256=body_sha256(_BODY),
        provided_signature=_sig(),
    )
    assert verify_request(now=_TS + 1, **args)[0]
    assert verify_request(now=_TS + 2, **args)[0]
