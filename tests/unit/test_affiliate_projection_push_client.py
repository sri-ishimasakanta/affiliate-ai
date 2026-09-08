"""app/affiliate/projection_push_client.py — prepare + execute (mocked transport)。

実ネットワークなし。exact body-byte / HMAC / レスポンス検証 / 安全なエラー写像 /
no-retry / secret 非露出 を確認する。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import httpx
import pytest

from app.affiliate.projection import (
    build_projection_entry,
    build_projection_snapshot,
)
from app.affiliate.projection_push_client import (
    PreparedProjectionPush,
    execute_projection_push,
    prepare_projection_push,
    signature_of,
    verify_prepared_signature,
)
from app.affiliate.projection_signing import (
    HEADER_CONTENT_SHA256,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    PROJECTION_ENDPOINT_PATH,
)
from app.exceptions import AffiliateProjectionPushError

_SECRET = "synthetic-test-secret-not-a-real-one"
_BASE = "https://runtime.example.test"
_TS = 1_760_000_000


def _empty_snapshot():
    return build_projection_snapshot([])


def _one_target_snapshot():
    entry = build_projection_entry(
        token="AAAA0000tokenone00000",
        destination_url="https://aff.example.test/track?a8mat=SECRETTRACKID123#f",
        destination_host="aff.example.test",
        link_identity_hash="1" * 64,
        alt_status="active",
        activated_at=datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC),
        disabled_at=None,
    )
    return build_projection_snapshot([entry])


def _prepared(snapshot=None):
    return prepare_projection_push(
        snapshot or _empty_snapshot(),
        base_url=_BASE,
        shared_secret=_SECRET,
        now=_TS,
    )


def _ok_body(prepared: PreparedProjectionPush, **over) -> dict:
    body = {
        "schema_version": 1,
        "projection_snapshot_hash": prepared.projection_snapshot_hash,
        "received_count": prepared.target_count,
        "inserted_count": 0,
        "updated_count": 0,
        "unchanged_count": 0,
    }
    body.update(over)
    return body


# ==================== prepare ======================================
def test_prepare_requires_https_base_url() -> None:
    with pytest.raises(AffiliateProjectionPushError, match="https"):
        prepare_projection_push(
            _empty_snapshot(), base_url="http://runtime.example.test", shared_secret=_SECRET
        )


def test_prepare_requires_secret() -> None:
    with pytest.raises(AffiliateProjectionPushError, match="secret"):
        prepare_projection_push(_empty_snapshot(), base_url=_BASE, shared_secret="")


def test_prepare_body_hash_is_over_exact_bytes_and_signature_verifies() -> None:
    p = _prepared()
    assert p.method == "POST"
    assert p.url == _BASE + PROJECTION_ENDPOINT_PATH
    assert p.body_sha256 == hashlib.sha256(p.body_bytes()).hexdigest()
    assert p.headers()[HEADER_CONTENT_SHA256] == p.body_sha256
    assert p.headers()[HEADER_TIMESTAMP] == str(_TS)
    assert verify_prepared_signature(p, shared_secret=_SECRET)
    # wire body is parseable + carries the same snapshot hash
    parsed = json.loads(p.body_bytes())
    assert parsed["schema_version"] == 1
    assert parsed["projection_snapshot_hash"] == p.projection_snapshot_hash


def test_prepare_empty_snapshot_hash_is_the_known_constant() -> None:
    p = _prepared()
    assert p.target_count == 0
    assert (
        p.projection_snapshot_hash
        == "019ac81aeaceee4153c5e477492bb27965210e24764d1ba1cbe50ac67e617b4d"
    )


# ==================== execute: happy ==============================
def test_execute_sends_exactly_the_prepared_bytes_and_headers() -> None:
    p = _prepared()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_ok_body(p))

    result = execute_projection_push(p, transport=httpx.MockTransport(handler))

    assert len(seen) == 1  # exactly one request, no retry
    req = seen[0]
    assert req.method == "POST"
    assert str(req.url) == p.url
    assert req.content == p.body_bytes()  # exact bytes, no json= reserialization
    assert req.headers["content-type"] == "application/json"
    assert req.headers[HEADER_CONTENT_SHA256.lower()] == p.body_sha256
    assert req.headers[HEADER_SIGNATURE.lower()] == signature_of(p)
    # server-echoed body hash must equal sha256 of exactly what was sent
    assert hashlib.sha256(req.content).hexdigest() == req.headers[HEADER_CONTENT_SHA256.lower()]

    assert result.http_status == 200
    assert result.received_count == 0
    assert result.inserted_count == result.updated_count == result.unchanged_count == 0


def test_execute_non_empty_success_unchanged_count_is_not_an_error() -> None:
    snap = _one_target_snapshot()
    p = _prepared(snap)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_ok_body(p, received_count=1, unchanged_count=1)
        )

    result = execute_projection_push(p, transport=httpx.MockTransport(handler))
    assert result.received_count == 1 and result.unchanged_count == 1


def test_execute_disables_redirect_following() -> None:
    p = _prepared()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/x"})

    with pytest.raises(AffiliateProjectionPushError, match="redirect"):
        execute_projection_push(p, transport=httpx.MockTransport(handler))


# ==================== execute: bad 200 responses ==================
@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"schema_version": 2}, "schema_version"),
        ({"projection_snapshot_hash": "0" * 64}, "different projection_snapshot_hash"),
        ({"received_count": 5}, "different target count"),
        ({"inserted_count": -1}, "invalid inserted_count"),
        ({"updated_count": "x"}, "invalid updated_count"),
        ({"inserted_count": 1}, "non-zero write counts"),  # empty snapshot
    ],
)
def test_execute_rejects_inconsistent_200(over: dict, needle: str) -> None:
    p = _prepared()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body(p, **over))

    with pytest.raises(AffiliateProjectionPushError, match=needle):
        execute_projection_push(p, transport=httpx.MockTransport(handler))


def test_execute_rejects_non_json_200() -> None:
    p = _prepared()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>nope</html>")

    with pytest.raises(AffiliateProjectionPushError, match="non-JSON"):
        execute_projection_push(p, transport=httpx.MockTransport(handler))


def test_execute_rejects_non_object_200() -> None:
    p = _prepared()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    with pytest.raises(AffiliateProjectionPushError, match="shape"):
        execute_projection_push(p, transport=httpx.MockTransport(handler))


# ==================== execute: server errors =====================
@pytest.mark.parametrize(
    ("status", "code"),
    [
        (403, "secret_not_configured"),
        (401, "signature_mismatch"),
        (401, "timestamp_outside_window"),
        (409, "snapshot_hash_mismatch"),
        (409, "conflict_immutable_identity_drift"),
        (422, "bad_token"),
        (500, "persist_failed"),
    ],
)
def test_execute_maps_known_server_error_codes(status: int, code: str) -> None:
    p = _prepared()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": code}})

    with pytest.raises(AffiliateProjectionPushError) as exc:
        execute_projection_push(p, transport=httpx.MockTransport(handler))
    assert exc.value.http_status == status
    assert exc.value.server_code == code
    assert str(status) in str(exc.value)


def test_execute_swallows_unknown_error_code_and_raw_body() -> None:
    p = _prepared()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"error": {"code": "some_internal_detail"}, "stack": "SECRET LEAK"},
        )

    with pytest.raises(AffiliateProjectionPushError) as exc:
        execute_projection_push(p, transport=httpx.MockTransport(handler))
    assert exc.value.server_code is None
    assert "SECRET LEAK" not in str(exc.value)
    assert "some_internal_detail" not in str(exc.value)


# ==================== execute: network failure (no retry) ========
def test_execute_timeout_is_ambiguous_no_retry() -> None:
    p = _prepared()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectTimeout("slow", request=request)

    with pytest.raises(AffiliateProjectionPushError, match="timed out; the WordPress"):
        execute_projection_push(p, transport=httpx.MockTransport(handler))
    assert calls["n"] == 1  # no automatic retry


def test_execute_connection_error_is_ambiguous_no_retry() -> None:
    p = _prepared()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(AffiliateProjectionPushError, match="connection failed"):
        execute_projection_push(p, transport=httpx.MockTransport(handler))
    assert calls["n"] == 1


# ==================== secret / signature non-leakage =============
def test_secret_and_signature_not_in_repr() -> None:
    p = _prepared(_one_target_snapshot())
    r = repr(p)
    assert _SECRET not in r
    assert signature_of(p) not in r
    assert "SECRETTRACKID123" not in r  # destination not retained anywhere printable
    assert p.body_bytes() is not None  # body reachable only via explicit accessor


def test_error_strings_never_contain_secret_or_body() -> None:
    p = _prepared(_one_target_snapshot())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": {"code": "conflict_hash_mismatch"}})

    with pytest.raises(AffiliateProjectionPushError) as exc:
        execute_projection_push(p, transport=httpx.MockTransport(handler))
    msg = str(exc.value)
    assert _SECRET not in msg
    assert signature_of(p) not in msg
    assert "SECRETTRACKID123" not in msg
    assert "a8mat" not in msg


# ==================== D-C2-A.1: success count conservation =======
def _n_target_snapshot(n: int):
    entries = [
        build_projection_entry(
            token=f"TKN{i:017d}",
            destination_url=f"https://aff.example.test/t{i}?x=1",
            destination_host="aff.example.test",
            link_identity_hash=f"{i:064d}",
            alt_status="active",
            activated_at=datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC),
            disabled_at=None,
        )
        for i in range(1, n + 1)
    ]
    return build_projection_snapshot(entries)


@pytest.mark.parametrize(
    ("n", "ins", "upd", "unch", "ok"),
    [
        (3, 1, 1, 1, True),   # sums to 3
        (3, 1, 0, 0, False),  # under-count: 1 != 3
        (1, 2, 0, 0, False),  # over-count: 2 != 1
        (2, 0, 1, 1, True),   # sums to 2
        (0, 0, 0, 0, True),   # empty all zero
    ],
)
def test_execute_enforces_count_conservation(n, ins, upd, unch, ok) -> None:
    snap = _n_target_snapshot(n)
    p = _prepared(snap)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "projection_snapshot_hash": p.projection_snapshot_hash,
                "received_count": n,
                "inserted_count": ins,
                "updated_count": upd,
                "unchanged_count": unch,
            },
        )

    if ok:
        result = execute_projection_push(p, transport=httpx.MockTransport(handler))
        assert (
            result.inserted_count + result.updated_count + result.unchanged_count
            == result.received_count
            == n
        )
    else:
        with pytest.raises(AffiliateProjectionPushError, match="do not sum") as exc:
            execute_projection_push(p, transport=httpx.MockTransport(handler))
        assert exc.value.http_status == 200


@pytest.mark.parametrize("bad", [True, False])
def test_execute_rejects_bool_counters(bad) -> None:
    p = _prepared()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body(p, inserted_count=bad))

    with pytest.raises(AffiliateProjectionPushError, match="invalid inserted_count"):
        execute_projection_push(p, transport=httpx.MockTransport(handler))


# ==================== D-C2-A.1: HTTPS-only execution boundary ====
def test_prepare_accepts_plain_https_origin() -> None:
    p = prepare_projection_push(
        _empty_snapshot(),
        base_url="https://runtime.example.test",
        shared_secret=_SECRET,
        now=_TS,
    )
    assert p.url == "https://runtime.example.test" + PROJECTION_ENDPOINT_PATH


@pytest.mark.parametrize(
    ("base", "needle"),
    [
        ("http://runtime.example.test", "https"),
        ("//runtime.example.test", "https"),
        ("runtime.example.test", "https"),
        ("ftp://runtime.example.test", "https"),
        ("https://user@runtime.example.test", "userinfo"),
        ("https://user:pass@runtime.example.test", "userinfo"),
        ("https://", "no host"),
        ("https://runtime.example.test/?a=1", "query or fragment"),
        ("https://runtime.example.test/#frag", "query or fragment"),
        ("https://runtime.example.test/wp-json/other", "origin only"),
    ],
)
def test_prepare_rejects_unsafe_base_url(base, needle) -> None:
    with pytest.raises(AffiliateProjectionPushError, match=needle):
        prepare_projection_push(
            _empty_snapshot(), base_url=base, shared_secret=_SECRET, now=_TS
        )


def test_prepare_endpoint_path_is_fixed_regardless_of_base_trailing_slash() -> None:
    a = prepare_projection_push(
        _empty_snapshot(), base_url="https://runtime.example.test", shared_secret=_SECRET, now=_TS
    )
    b = prepare_projection_push(
        _empty_snapshot(), base_url="https://runtime.example.test/", shared_secret=_SECRET, now=_TS
    )
    assert a.url == b.url == "https://runtime.example.test" + PROJECTION_ENDPOINT_PATH
    assert a.endpoint_path == PROJECTION_ENDPOINT_PATH
