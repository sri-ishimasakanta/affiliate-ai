"""app/affiliate/click_export_client.py — prepare + execute (mocked transport)。

実ネットワークなし。exact GET target / query-bound HMAC / redirect 拒否 /
安全なエラー写像 / no-retry / secret 非露出 を確認する。
"""

from __future__ import annotations

import httpx
import pytest

from app.affiliate.click_export_client import (
    CLICK_EXPORT_ENDPOINT_PATH,
    ClickExportPage,
    execute_click_export,
    prepare_click_export,
    signature_of,
    verify_prepared_signature,
)
from app.affiliate.projection_signing import (
    HEADER_CONTENT_SHA256,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
)
from app.exceptions import AffiliateClickImportError

_SECRET = "synthetic-test-secret-not-a-real-one"
_BASE = "https://runtime.example.test"
_TS = 1_760_000_000
_EMPTY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_TOK = "tokAAAAAAAAAAAAAAAAA"


def _prepared(*, since_id=0, limit=1000):
    return prepare_click_export(
        base_url=_BASE,
        shared_secret=_SECRET,
        since_id=since_id,
        limit=limit,
        now=_TS,
    )


def _ok_page(*, count, limit, next_since_id, rows):
    return {
        "schema_version": 1,
        "count": count,
        "limit": limit,
        "next_since_id": next_since_id,
        "rows": rows,
    }


# ==================== prepare ========================================
def test_prepare_signed_target_orders_limit_before_since_id() -> None:
    p = _prepared(since_id=42, limit=500)
    assert p.signed_target == (
        f"{CLICK_EXPORT_ENDPOINT_PATH}?limit=500&since_id=42"
    )
    assert p.url == _BASE + p.signed_target
    assert p.method == "GET"
    assert p.endpoint_path == CLICK_EXPORT_ENDPOINT_PATH


def test_prepare_body_hash_is_empty_body_and_signature_verifies() -> None:
    p = _prepared()
    assert p.body_sha256 == _EMPTY_SHA
    assert p.headers()[HEADER_CONTENT_SHA256] == _EMPTY_SHA
    assert p.headers()[HEADER_TIMESTAMP] == str(_TS)
    assert verify_prepared_signature(p, shared_secret=_SECRET)


def test_prepare_signature_changes_with_cursor_and_limit() -> None:
    base = signature_of(_prepared(since_id=0, limit=1000))
    assert signature_of(_prepared(since_id=1, limit=1000)) != base
    assert signature_of(_prepared(since_id=0, limit=999)) != base


def test_prepare_requires_secret() -> None:
    with pytest.raises(AffiliateClickImportError, match="secret"):
        prepare_click_export(
            base_url=_BASE, shared_secret="", since_id=0, limit=10, now=_TS
        )


@pytest.mark.parametrize(
    ("base", "needle"),
    [
        ("http://runtime.example.test", "https"),
        ("runtime.example.test", "https"),
        ("https://user@runtime.example.test", "userinfo"),
        ("https://", "no host"),
        ("https://runtime.example.test/?a=1", "query or fragment"),
        ("https://runtime.example.test/#f", "query or fragment"),
        ("https://runtime.example.test/wp-json/x", "origin only"),
    ],
)
def test_prepare_rejects_unsafe_base_url(base, needle) -> None:
    with pytest.raises(AffiliateClickImportError, match=needle):
        prepare_click_export(
            base_url=base, shared_secret=_SECRET, since_id=0, limit=10, now=_TS
        )


@pytest.mark.parametrize("limit", [0, -1, 1001, True, 1.5, "10"])
def test_prepare_rejects_bad_limit(limit) -> None:
    with pytest.raises(AffiliateClickImportError):
        prepare_click_export(
            base_url=_BASE, shared_secret=_SECRET, since_id=0, limit=limit, now=_TS
        )


@pytest.mark.parametrize("since_id", [-1, True, 1.5, "0"])
def test_prepare_rejects_bad_since_id(since_id) -> None:
    with pytest.raises(AffiliateClickImportError):
        prepare_click_export(
            base_url=_BASE, shared_secret=_SECRET, since_id=since_id, limit=10, now=_TS
        )


# ==================== execute: happy ================================
def test_execute_sends_exactly_one_get_with_prepared_target() -> None:
    p = _prepared(since_id=5, limit=1000)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=_ok_page(
                count=1,
                limit=1000,
                next_since_id=6,
                rows=[{"id": 6, "token": _TOK, "clicked_at": "2026-09-01 10:00:00"}],
            ),
        )

    page = execute_click_export(p, transport=httpx.MockTransport(handler))

    assert len(seen) == 1
    req = seen[0]
    assert req.method == "GET"
    assert str(req.url) == p.url
    assert req.content == b""
    assert req.headers[HEADER_SIGNATURE.lower()] == signature_of(p)
    assert isinstance(page, ClickExportPage)
    assert page.count == 1
    assert page.next_since_id == 6
    assert page.rows[0].source_click_id == 6
    assert page.has_more is False


def test_execute_valid_empty_page() -> None:
    p = _prepared(since_id=9, limit=1000)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_ok_page(count=0, limit=1000, next_since_id=9, rows=[])
        )

    page = execute_click_export(p, transport=httpx.MockTransport(handler))
    assert page.count == 0
    assert page.rows == ()
    assert page.next_since_id == 9
    assert page.has_more is False


def test_execute_full_page_sets_has_more() -> None:
    p = _prepared(since_id=0, limit=2)
    rows = [
        {"id": 1, "token": _TOK, "clicked_at": "2026-09-01 10:00:00"},
        {"id": 2, "token": _TOK, "clicked_at": "2026-09-01 10:00:01"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_ok_page(count=2, limit=2, next_since_id=2, rows=rows)
        )

    page = execute_click_export(p, transport=httpx.MockTransport(handler))
    assert page.count == 2
    assert page.has_more is True


# ==================== execute: rejects ==============================
def test_execute_rejects_redirect() -> None:
    p = _prepared()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/x"})

    with pytest.raises(AffiliateClickImportError, match="redirect"):
        execute_click_export(p, transport=httpx.MockTransport(handler))


def test_execute_rejects_non_json_200() -> None:
    p = _prepared()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>nope</html>")

    with pytest.raises(AffiliateClickImportError, match="non-JSON") as exc:
        execute_click_export(p, transport=httpx.MockTransport(handler))
    assert exc.value.http_status == 200


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (403, "secret_not_configured"),
        (401, "signature_mismatch"),
        (401, "timestamp_outside_window"),
        (400, "bad_cursor"),
        (400, "bad_limit"),
    ],
)
def test_execute_maps_known_server_codes(status, code) -> None:
    p = _prepared()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": code}})

    with pytest.raises(AffiliateClickImportError) as exc:
        execute_click_export(p, transport=httpx.MockTransport(handler))
    assert exc.value.http_status == status
    assert exc.value.server_code == code


def test_execute_redacts_unknown_server_code_and_body() -> None:
    p = _prepared()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"error": {"code": "internal_detail"}, "trace": "SECRET LEAK"},
        )

    with pytest.raises(AffiliateClickImportError) as exc:
        execute_click_export(p, transport=httpx.MockTransport(handler))
    assert exc.value.server_code is None
    assert "SECRET LEAK" not in str(exc.value)
    assert "internal_detail" not in str(exc.value)


def test_execute_timeout_is_safe_no_retry() -> None:
    p = _prepared()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectTimeout("slow", request=request)

    with pytest.raises(AffiliateClickImportError, match="re-run from the same since_id"):
        execute_click_export(p, transport=httpx.MockTransport(handler))
    assert calls["n"] == 1


def test_execute_transport_error_is_safe_no_retry() -> None:
    p = _prepared()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(AffiliateClickImportError, match="re-run from the same since_id"):
        execute_click_export(p, transport=httpx.MockTransport(handler))
    assert calls["n"] == 1


def test_execute_propagates_validation_error_from_bad_page() -> None:
    p = _prepared(since_id=0, limit=1000)

    def handler(request: httpx.Request) -> httpx.Response:
        # descending ids
        return httpx.Response(
            200,
            json=_ok_page(
                count=2,
                limit=1000,
                next_since_id=1,
                rows=[
                    {"id": 5, "token": _TOK, "clicked_at": "2026-09-01 10:00:00"},
                    {"id": 1, "token": _TOK, "clicked_at": "2026-09-01 10:00:01"},
                ],
            ),
        )

    with pytest.raises(AffiliateClickImportError, match="ascending id order"):
        execute_click_export(p, transport=httpx.MockTransport(handler))


# ==================== secret / signature non-leakage ================
def test_secret_and_signature_not_in_repr() -> None:
    p = _prepared()
    r = repr(p)
    assert _SECRET not in r
    assert signature_of(p) not in r


def test_error_strings_never_contain_secret_or_signature() -> None:
    p = _prepared()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "signature_mismatch"}})

    with pytest.raises(AffiliateClickImportError) as exc:
        execute_click_export(p, transport=httpx.MockTransport(handler))
    msg = str(exc.value)
    assert _SECRET not in msg
    assert signature_of(p) not in msg
