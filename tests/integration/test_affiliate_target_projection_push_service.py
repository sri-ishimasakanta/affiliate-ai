"""AffiliateTargetProjectionPushService — two-transaction durability + outcome
classification (D-D0.3 §9-11, D-D1A)。実ネットワークなし (httpx.MockTransport のみ)。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.exceptions import AffiliateProjectionPushError
from app.models import (
    AffiliateLinkTarget,
    AffiliateTargetProjectionPushRun,
    Article,
    ArticleAffiliateProgram,
)
from app.models.affiliate_target_projection_push_run import (
    ATPP_FAILED,
    ATPP_OUTCOME_UNKNOWN,
    ATPP_RUNNING,
    ATPP_SUCCEEDED,
)
from app.models.enums import AffiliateProgramStatus
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.services.affiliate_link_target_service import AffiliateLinkTargetService
from app.services.affiliate_target_projection_push_service import (
    AffiliateTargetProjectionPushService,
)

_SECRET = "s" * 40
_BASE = "https://runtime.example.test"
_HOST = "aff.example.test"
_TRACKING = f"https://{_HOST}/track?a8mat=SECRETTRACKID999#f"
_POLICY = {"a8": frozenset({_HOST})}
_EMPTY_HASH = "019ac81aeaceee4153c5e477492bb27965210e24764d1ba1cbe50ac67e617b4d"


def _settings(*, base=_BASE, secret=_SECRET, verify=True):
    return SimpleNamespace(
        wordpress_base_url=base,
        wordpress_verify_tls=verify,
        affiliate_runtime_shared_secret=secret,
        affiliate_runtime_push_configured=bool(base and secret),
    )


def _factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _seed_one_target(session: Session) -> str:
    art = Article(title="t", slug="p1", keyword_id=None, body="# b\n")
    session.add(art)
    session.flush()
    session.commit()
    pid = AffiliateProgramRepository(session).create(
        name="P", provider="a8", tracking_url=_TRACKING,
        status=AffiliateProgramStatus.ACTIVE,
    ).id
    session.add(ArticleAffiliateProgram(article_id=art.id, affiliate_program_id=pid))
    session.commit()
    target = AffiliateLinkTargetService(session, host_policy=_POLICY).create_target(
        article_id=art.id, affiliate_program_id=pid
    )
    session.commit()
    return target.token


def _ok_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(
        200,
        json={
            "schema_version": 1,
            "projection_snapshot_hash": body["projection_snapshot_hash"],
            "received_count": 0, "inserted_count": 0, "updated_count": 0,
            "unchanged_count": 0,
        },
    )


def _run_count(session: Session) -> int:
    return session.scalar(
        select(func.count()).select_from(AffiliateTargetProjectionPushRun)
    )


def _latest_run(session: Session) -> AffiliateTargetProjectionPushRun:
    return session.scalars(
        select(AffiliateTargetProjectionPushRun).order_by(
            AffiliateTargetProjectionPushRun.id.desc()
        )
    ).first()


# ==================== durability: running committed before network =======
def test_running_row_is_durably_committed_before_mock_receives_request(engine) -> None:
    sf = _factory(engine)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # 独立した別セッション/接続で読む (同じ engine, 別 Session インスタンス)。
        with sf() as independent_session:
            row = independent_session.scalars(
                select(AffiliateTargetProjectionPushRun)
            ).one()
            seen["status"] = row.status
            seen["requested_snapshot_hash"] = row.requested_snapshot_hash
            seen["request_manifest_json"] = row.request_manifest_json
        return _ok_handler(request)

    svc = AffiliateTargetProjectionPushService(session_factory=sf)
    outcome = svc.push(
        settings=_settings(), transport=httpx.MockTransport(handler), now=1_760_000_000
    )

    assert seen["status"] == ATPP_RUNNING  # observed BEFORE the response returned
    assert seen["requested_snapshot_hash"] == outcome.result.projection_snapshot_hash
    assert seen["request_manifest_json"] == "[]"

    with sf() as s:
        final = _latest_run(s)
        assert final.status == ATPP_SUCCEEDED


def test_add_running_repository_error_yields_zero_http_requests(
    engine, monkeypatch
) -> None:
    """service-error-propagation テスト (D-D1A.1 §11 と同じ理由で再分類)。

    ``add_running`` 自体 (repository メソッド、``Session.commit`` そのものでは
    ない) が例外を送出した場合、POST に到達しないことを確認する。

    **Transaction A の実際の ``session.commit()`` 失敗の証明ではない** — その
    証明は
    :mod:`tests.integration.test_affiliate_target_projection_push_durability`
    の ``test_transaction_a_actual_session_commit_failure``
    (file-backed DB + 物理的に独立した接続 + ``Session.commit`` 自体への patch)
    が担う。
    """

    sf = _factory(engine)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _ok_handler(request)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated add_running repository error")

    monkeypatch.setattr(
        "app.repositories.affiliate_target_projection_push_run_repository."
        "AffiliateTargetProjectionPushRunRepository.add_running",
        boom,
    )
    svc = AffiliateTargetProjectionPushService(session_factory=sf)
    with pytest.raises(RuntimeError):
        svc.push(settings=_settings(), transport=httpx.MockTransport(handler), now=1)

    assert len(calls) == 0
    with sf() as s:
        assert _run_count(s) == 0


# ==================== manifest / snapshot immutability between commit and POST
def test_prepared_request_is_not_rebuilt_after_target_state_changes(engine) -> None:
    """running row を commit した後に別接続で target を変更しても、実際に送信される
    body / manifest は commit 時点で凍結された内容のまま (§D-D0.3-13/15)。"""

    sf = _factory(engine)
    with sf() as seed_session:
        token = _seed_one_target(seed_session)

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured["body"] = body
        # 別接続から target を破壊的に変更してみる (実際の DB 変更)。
        with sf() as mutate_session:
            row = mutate_session.scalars(select(AffiliateLinkTarget)).one()
            row.status = "disabled"
            mutate_session.commit()
        n = len(body["targets"])
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "projection_snapshot_hash": body["projection_snapshot_hash"],
                "received_count": n, "inserted_count": n, "updated_count": 0,
                "unchanged_count": 0,
            },
        )

    svc = AffiliateTargetProjectionPushService(session_factory=sf)
    svc.push(settings=_settings(), transport=httpx.MockTransport(handler), now=1)

    with sf() as s:
        run = _latest_run(s)
        manifest = json.loads(run.request_manifest_json)

    assert len(captured["body"]["targets"]) == 1
    assert captured["body"]["targets"][0]["token"] == token
    assert captured["body"]["targets"][0]["status"] == "active"  # pre-mutation value
    assert manifest[0]["status"] == "active"  # frozen manifest matches what was sent


# ==================== classification: succeeded ===========================
def test_validated_200_is_succeeded(engine) -> None:
    sf = _factory(engine)
    svc = AffiliateTargetProjectionPushService(session_factory=sf)
    outcome = svc.push(
        settings=_settings(), transport=httpx.MockTransport(_ok_handler), now=1
    )
    with sf() as s:
        run = s.get(AffiliateTargetProjectionPushRun, outcome.run_id)
        assert run.status == ATPP_SUCCEEDED
        assert run.http_status == 200
        assert run.received_count == 0


# ==================== classification: outcome_unknown ======================
def _push_and_get_status(sf, handler, *, settings=None) -> AffiliateTargetProjectionPushRun:
    svc = AffiliateTargetProjectionPushService(session_factory=sf)
    with pytest.raises(AffiliateProjectionPushError):
        svc.push(
            settings=settings or _settings(),
            transport=httpx.MockTransport(handler),
            now=1,
        )
    with sf() as s:
        return _latest_run(s)


def test_timeout_is_outcome_unknown(engine) -> None:
    def handler(request: httpx.Request):
        raise httpx.ConnectTimeout("slow", request=request)

    run = _push_and_get_status(_factory(engine), handler)
    assert run.status == ATPP_OUTCOME_UNKNOWN
    assert run.http_status is None


def test_transport_reset_is_outcome_unknown(engine) -> None:
    def handler(request: httpx.Request):
        raise httpx.ConnectError("reset", request=request)

    run = _push_and_get_status(_factory(engine), handler)
    assert run.status == ATPP_OUTCOME_UNKNOWN


def test_generic_html_500_is_outcome_unknown(engine) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="<html>Internal Server Error</html>")

    run = _push_and_get_status(_factory(engine), handler)
    assert run.status == ATPP_OUTCOME_UNKNOWN
    assert run.http_status == 500
    assert run.server_code is None


def test_unknown_json_error_code_is_outcome_unknown(engine) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": {"code": "some_new_future_code"}})

    run = _push_and_get_status(_factory(engine), handler)
    assert run.status == ATPP_OUTCOME_UNKNOWN


def test_malformed_json_error_shape_is_outcome_unknown(engine) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"unexpected": "shape"})

    run = _push_and_get_status(_factory(engine), handler)
    assert run.status == ATPP_OUTCOME_UNKNOWN


def test_body_hash_mismatch_is_outcome_unknown(engine) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "body_hash_mismatch"}})

    run = _push_and_get_status(_factory(engine), handler)
    assert run.status == ATPP_OUTCOME_UNKNOWN


def test_bad_signature_is_outcome_unknown(engine) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "bad_signature"}})

    run = _push_and_get_status(_factory(engine), handler)
    assert run.status == ATPP_OUTCOME_UNKNOWN


def test_malformed_http_200_is_outcome_unknown(engine) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "schema_version": 1, "projection_snapshot_hash": "not-the-real-hash",
                "received_count": 0, "inserted_count": 0, "updated_count": 0,
                "unchanged_count": 0,
            },
        )

    run = _push_and_get_status(_factory(engine), handler)
    assert run.status == ATPP_OUTCOME_UNKNOWN
    assert run.http_status == 200  # side effect proven per D-D0.2/D-D0.3


# ==================== classification: failed ================================
@pytest.mark.parametrize(
    ("status", "code"),
    [
        (403, "secret_not_configured"),
        (401, "signature_mismatch"),
        (422, "bad_token"),
        (409, "entry_hash_mismatch"),
        (409, "conflict_stale_version"),
        (500, "persist_failed"),
    ],
)
def test_known_definitive_codes_are_failed(engine, status, code) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": code}})

    run = _push_and_get_status(_factory(engine), handler)
    assert run.status == ATPP_FAILED
    assert run.http_status == status
    assert run.server_code == code


# ==================== terminal durability / no double POST =================
def test_mark_succeeded_repository_error_leaves_run_durably_running(
    engine, monkeypatch
) -> None:
    """service-error-propagation テスト (D-D1A.1 §11 による再分類)。

    ``mark_succeeded`` 自体 (repository メソッド、``Session.commit`` そのものでは
    ない) が例外を送出した場合、Transaction B が最後まで到達しないことを確認する。

    **これは「Transaction B の実際の ``session.commit()`` 失敗」の証明では
    ない** — その証明は
    :mod:`tests.integration.test_affiliate_target_projection_push_durability`
    の
    ``test_transaction_b_actual_session_commit_failure_leaves_row_running``
    (file-backed DB + 物理的に独立した接続 + ``Session.commit`` 自体への patch)
    が担う。本テストは「commit に到達する前の repository 層の例外でも durable
    running のまま保たれ、再送しない」という、それ自体独立して価値のある
    service-error 伝播の回帰テストとして残す。
    """

    sf = _factory(engine)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _ok_handler(request)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated mark_succeeded repository error")

    monkeypatch.setattr(
        "app.repositories.affiliate_target_projection_push_run_repository."
        "AffiliateTargetProjectionPushRunRepository.mark_succeeded",
        boom,
    )
    svc = AffiliateTargetProjectionPushService(session_factory=sf)
    with pytest.raises(RuntimeError):
        svc.push(settings=_settings(), transport=httpx.MockTransport(handler), now=1)

    assert len(calls) == 1  # exactly one POST; no compensating retry
    with sf() as s:
        run = _latest_run(s)
        assert run.status == ATPP_RUNNING


# ==================== no automatic retry (uniform) ==========================
def test_no_automatic_retry_on_timeout(engine) -> None:
    sf = _factory(engine)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request):
        calls.append(request)
        raise httpx.ConnectTimeout("slow", request=request)

    svc = AffiliateTargetProjectionPushService(session_factory=sf)
    with pytest.raises(AffiliateProjectionPushError):
        svc.push(settings=_settings(), transport=httpx.MockTransport(handler), now=1)
    assert len(calls) == 1
