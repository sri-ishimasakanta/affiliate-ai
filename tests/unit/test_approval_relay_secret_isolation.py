"""承認中継と affiliate runtime の信頼ドメイン分離 (C8.8.1)。

``/go/`` のリダイレクト経路と「人の承認を運ぶ経路」は別の信頼ドメインである。
片方の鍵が漏れても、もう片方の権限は渡らないようにする。

pin する契約:

- 承認中継は **専用の secret だけ** を使う。
- affiliate runtime の secret へ fallback しない (未設定なら fail closed)。
- どちらの鍵も、相手側の呼び出しを認証できない。
- secret は診断出力にも例外にも現れない (設定済みかどうかだけ)。
- 既存の affiliate runtime の挙動は変わらない。
"""

from __future__ import annotations

import time

import pytest

from app.affiliate.projection_signing import (
    PROJECTION_ENDPOINT_PATH,
    body_sha256,
    compute_signature,
    verify_request,
)
from app.approval.relay_client import ROUTE_SESSIONS, ApprovalRelayClient, RelayError

_APPROVAL_SECRET = "approval-relay-key-aaaaaaaaaaaaaaaaaaaaaaaa"
_AFFILIATE_SECRET = "affiliate-runtime-key-bbbbbbbbbbbbbbbbbbbbbb"
_BODY = b'{"relay_session_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'


class _Settings:
    wordpress_base_url = "https://bizfluxlab.com"
    approval_relay_shared_secret = _APPROVAL_SECRET
    affiliate_runtime_shared_secret = _AFFILIATE_SECRET


def _settings(**overrides):
    return type("S", (_Settings,), overrides)()


class _Recorder:
    """署名ヘッダだけを記録する。ネットワークには出ない。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def request(self, method, url, *, content=None, headers=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {})})
        return _Response()

    def close(self) -> None:
        return None


class _Response:
    status_code = 200

    def json(self) -> dict:
        return {"decisions": []}


def _verify(secret: str, headers: dict, *, path: str, body: bytes, method: str = "POST"):
    return verify_request(
        shared_secret=secret,
        method=method,
        path=path,
        timestamp=int(headers["X-BFL-Timestamp"]),
        now=int(headers["X-BFL-Timestamp"]),
        body=body,
        provided_content_sha256=headers["X-BFL-Content-SHA256"],
        provided_signature=headers["X-BFL-Signature"],
    )


# -- the relay uses only its own key -------------------------------------------
def test_the_relay_signs_with_the_approval_secret() -> None:
    import json

    payload = {
        "relay_session_id": "a" * 32,
        "subject_type": "change_request",
        "subject_id": 12,
        "subject_hash": "b" * 64,
        "subject_version": 1,
        "capability_digest": "c" * 64,
        "expires_at": "2026-09-25T00:00:00+00:00",
        "snapshot": {},
    }
    http = _Recorder()
    ApprovalRelayClient(_settings(), http_client=http).create_session(**payload)

    headers = http.calls[0]["headers"]
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert _verify(_APPROVAL_SECRET, headers, path=ROUTE_SESSIONS, body=body) == (True, "ok")


def test_the_affiliate_secret_cannot_authenticate_an_approval_call() -> None:
    """承認中継の呼び出しを affiliate の鍵で検証しても通らない。"""

    http = _Recorder()
    ApprovalRelayClient(_settings(), http_client=http).acknowledge(
        relay_session_id="a" * 32, local_outcome="approved"
    )
    headers = http.calls[0]["headers"]
    body = b'{"relay_session_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","local_outcome":"approved"}'

    ok, reason = _verify(
        _AFFILIATE_SECRET,
        headers,
        path="/wp-json/affiliate-ai/v1/approval-decisions/ack",
        body=body,
    )

    assert ok is False
    assert reason == "signature_mismatch"


def test_the_approval_secret_cannot_authenticate_an_affiliate_call() -> None:
    """逆方向。承認の鍵で affiliate runtime の endpoint は通らない。"""

    timestamp = int(time.time())
    signature = compute_signature(
        shared_secret=_APPROVAL_SECRET,
        method="POST",
        path=PROJECTION_ENDPOINT_PATH,
        timestamp=timestamp,
        body_sha256_hex=body_sha256(_BODY),
    )
    headers = {
        "X-BFL-Timestamp": str(timestamp),
        "X-BFL-Content-SHA256": body_sha256(_BODY),
        "X-BFL-Signature": signature,
    }

    ok, reason = _verify(_AFFILIATE_SECRET, headers, path=PROJECTION_ENDPOINT_PATH, body=_BODY)

    assert ok is False
    assert reason == "signature_mismatch"


def test_the_affiliate_runtime_still_accepts_its_own_key() -> None:
    """既存の経路は壊れていない。"""

    timestamp = int(time.time())
    signature = compute_signature(
        shared_secret=_AFFILIATE_SECRET,
        method="POST",
        path=PROJECTION_ENDPOINT_PATH,
        timestamp=timestamp,
        body_sha256_hex=body_sha256(_BODY),
    )
    headers = {
        "X-BFL-Timestamp": str(timestamp),
        "X-BFL-Content-SHA256": body_sha256(_BODY),
        "X-BFL-Signature": signature,
    }

    assert _verify(_AFFILIATE_SECRET, headers, path=PROJECTION_ENDPOINT_PATH, body=_BODY) == (
        True,
        "ok",
    )


# -- no fallback ----------------------------------------------------------------
def test_a_missing_approval_secret_fails_closed() -> None:
    http = _Recorder()
    client = ApprovalRelayClient(_settings(approval_relay_shared_secret=None), http_client=http)

    with pytest.raises(RelayError, match="APPROVAL_RELAY_SHARED_SECRET is not configured"):
        client.fetch_decisions()

    # 署名できないので、リクエストそのものが出ていない。
    assert http.calls == []


def test_a_blank_approval_secret_fails_closed() -> None:
    http = _Recorder()
    client = ApprovalRelayClient(_settings(approval_relay_shared_secret="   "), http_client=http)

    with pytest.raises(RelayError):
        client.acknowledge(relay_session_id="a" * 32, local_outcome="approved")

    assert http.calls == []


def test_the_relay_never_falls_back_to_the_affiliate_secret() -> None:
    """affiliate の鍵だけが設定されていても、承認中継は動かない。"""

    http = _Recorder()
    client = ApprovalRelayClient(_settings(approval_relay_shared_secret=None), http_client=http)

    for call in (
        lambda: client.fetch_decisions(),
        lambda: client.acknowledge(relay_session_id="a" * 32, local_outcome="approved"),
        lambda: client.revoke(relay_session_id="a" * 32, reason="x"),
        lambda: client.create_session(
            relay_session_id="a" * 32,
            subject_type="change_request",
            subject_id=1,
            subject_hash="b" * 64,
            subject_version=1,
            capability_digest="c" * 64,
            expires_at="2026-09-25T00:00:00+00:00",
            snapshot={},
        ),
    ):
        with pytest.raises(RelayError):
            call()

    assert http.calls == []


def test_the_relay_client_source_names_only_its_own_secret() -> None:
    """コードの上でも fallback が存在しないことを固定する。"""

    import inspect

    from app.approval import relay_client

    source = inspect.getsource(relay_client.ApprovalRelayClient)

    assert "approval_relay_shared_secret" in source
    assert "affiliate_runtime_shared_secret" not in source


# -- the secret never leaks ------------------------------------------------------
def test_the_secret_never_appears_in_an_error() -> None:
    http = _Recorder()
    client = ApprovalRelayClient(_settings(wordpress_base_url="http://insecure"), http_client=http)

    with pytest.raises(RelayError) as excinfo:
        client.fetch_decisions()

    assert _APPROVAL_SECRET not in str(excinfo.value)


def test_the_secret_never_appears_in_the_request_headers() -> None:
    http = _Recorder()
    ApprovalRelayClient(_settings(), http_client=http).fetch_decisions()

    headers = http.calls[0]["headers"]

    assert _APPROVAL_SECRET not in repr(headers)
    # 署名だけが送られる (鍵そのものではない)。
    assert len(headers["X-BFL-Signature"]) == 64


def test_the_settings_diagnostic_shows_only_whether_it_is_configured() -> None:
    from app.config.settings import Settings

    configured = Settings(approval_relay_shared_secret=_APPROVAL_SECRET)
    missing = Settings(approval_relay_shared_secret=None)

    assert configured.approval_relay_configured is True
    assert missing.approval_relay_configured is False
    # 真偽以外は返さない。
    assert isinstance(configured.approval_relay_configured, bool)


def test_a_blank_secret_is_not_considered_configured() -> None:
    from app.config.settings import Settings

    assert Settings(approval_relay_shared_secret="   ").approval_relay_configured is False
