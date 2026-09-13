"""D-D1A.1: AffiliateTargetProjectionPushService の durability を **物理的に独立した
DB 接続** で証明する (D-D1A の in-memory StaticPool 共有接続では不十分だったための
hardening)。

- file-backed な一時 SQLite DB を使い、``app.config.database.build_engine`` を
  **複数回別々に呼んで** 独立した engine/connection pool を作る
  (``:memory:`` 専用の ``StaticPool`` 共有はここでは一切発生しない — 通常の
  file-backed SQLite URL には適用されない)。
- Transaction A / Transaction B の **実際の** ``Session.commit()`` 自体を失敗させる
  (repository メソッドや flush ではなく、``sqlalchemy.orm.Session.commit`` を直接
  patch する) ことで、コミット失敗時の durable state を厳密に検証する。

実ネットワークなし (httpx.MockTransport のみ)。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import mock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config.database import build_engine
from app.models import AffiliateTargetProjectionPushRun, Base
from app.models.affiliate_target_projection_push_run import ATPP_RUNNING
from app.services.affiliate_target_projection_push_service import (
    AffiliateTargetProjectionPushService,
)

_SECRET = "s" * 40
_BASE = "https://runtime.example.test"
_EMPTY_HASH = "019ac81aeaceee4153c5e477492bb27965210e24764d1ba1cbe50ac67e617b4d"


def _settings():
    return SimpleNamespace(
        wordpress_base_url=_BASE,
        wordpress_verify_tls=True,
        affiliate_runtime_shared_secret=_SECRET,
        affiliate_runtime_push_configured=True,
    )


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "schema_version": 1,
            "projection_snapshot_hash": _EMPTY_HASH,
            "received_count": 0, "inserted_count": 0, "updated_count": 0,
            "unchanged_count": 0,
        },
    )


def _new_engine(url: str):
    """``app.config.database.build_engine`` を **その都度独立に** 呼ぶ。

    file-backed SQLite URL には ``StaticPool`` は適用されない (``build_engine`` は
    ``:memory:``/``sqlite://`` の特殊 URL にのみ適用する) — 呼ぶたびに別々の
    connection pool / 別々の物理 DBAPI 接続が作られる。これが「独立した接続」の
    保証そのものである (呼び出し側で追加の細工は不要)。
    """

    return build_engine(url)


def _prepare_db(tmp_path) -> tuple[str, sessionmaker]:
    db_path = tmp_path / "d1a1_durability.db"
    url = f"sqlite:///{db_path}"
    engine_a = _new_engine(url)
    Base.metadata.create_all(engine_a)
    sf = sessionmaker(bind=engine_a, autoflush=False, expire_on_commit=False)
    return url, sf


def _independent_rows(url: str) -> list[AffiliateTargetProjectionPushRun]:
    """呼び出し元とは別の ``build_engine`` 呼び出し (= 別の物理接続) で読む。"""

    verify_engine = _new_engine(url)
    try:
        with sessionmaker(bind=verify_engine)() as verify_session:
            return list(verify_session.scalars(select(AffiliateTargetProjectionPushRun)))
    finally:
        verify_engine.dispose()


# ==================== §3-5: running visible via independent connection ====
def test_running_row_visible_via_physically_independent_connection(tmp_path) -> None:
    url, sf = _prepare_db(tmp_path)
    observed: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # MockTransport ハンドラ内で **新しい build_engine() 呼び出し** による、
        # Transaction A とは物理的に別の DBAPI 接続を作って読む。file-backed DB
        # なので StaticPool のような接続共有は起こり得ない (上の docstring 参照)。
        verify_engine = _new_engine(url)
        try:
            with sessionmaker(bind=verify_engine)() as verify_session:
                rows = list(
                    verify_session.scalars(select(AffiliateTargetProjectionPushRun))
                )
                observed["count"] = len(rows)
                observed["status"] = rows[0].status if rows else None
                observed["requested_snapshot_hash"] = (
                    rows[0].requested_snapshot_hash if rows else None
                )
                observed["request_manifest_json"] = (
                    rows[0].request_manifest_json if rows else None
                )
                observed["runtime_origin"] = rows[0].runtime_origin if rows else None
        finally:
            verify_engine.dispose()
        return _ok_handler(request)

    svc = AffiliateTargetProjectionPushService(session_factory=sf)
    outcome = svc.push(
        settings=_settings(), transport=httpx.MockTransport(handler), now=1_760_000_000
    )

    # 独立接続が HTTP レスポンスが返る **前に** observe した内容 — durable であることの証明。
    assert observed["count"] == 1
    assert observed["status"] == ATPP_RUNNING
    assert observed["requested_snapshot_hash"] == outcome.result.projection_snapshot_hash
    assert observed["request_manifest_json"] == "[]"
    assert observed["runtime_origin"] == _BASE


# ==================== §6: Transaction A actual commit() failure ===========
def test_transaction_a_actual_session_commit_failure(tmp_path) -> None:
    url, sf = _prepare_db(tmp_path)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _ok_handler(request)

    svc = AffiliateTargetProjectionPushService(session_factory=sf)
    # ``Session.commit`` そのもの (repository メソッドでも flush でもない) を
    # 失敗させる。Transaction A で最初に呼ばれる commit がこれ。
    with mock.patch.object(
        Session, "commit", side_effect=RuntimeError("simulated Transaction A commit failure")
    ):
        with pytest.raises(RuntimeError, match="simulated Transaction A commit failure"):
            svc.push(settings=_settings(), transport=httpx.MockTransport(handler), now=1)

    assert len(calls) == 0  # 0 HTTP requests
    rows = _independent_rows(url)
    assert rows == []  # durable running row は一切存在しない (独立接続で確認)


# ==================== §7-10: Transaction B actual commit() failure ========
@pytest.mark.parametrize("handler_kind", ["succeeded", "failed", "outcome_unknown"])
def test_transaction_b_actual_session_commit_failure_leaves_row_running(
    tmp_path, handler_kind
) -> None:
    url, sf = _prepare_db(tmp_path)
    calls: list[httpx.Request] = []

    if handler_kind == "succeeded":
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return _ok_handler(request)
    elif handler_kind == "failed":
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(409, json={"error": {"code": "conflict_stale_version"}})
    else:  # outcome_unknown (timeout)
        def handler(request: httpx.Request):
            calls.append(request)
            raise httpx.ConnectTimeout("slow", request=request)

    real_commit = Session.commit
    call_state = {"n": 0}

    def commit_side_effect(self, *args, **kwargs):
        call_state["n"] += 1
        if call_state["n"] == 2:  # Transaction A's commit is call #1; this is Txn B's
            raise RuntimeError("simulated Transaction B commit failure")
        return real_commit(self, *args, **kwargs)

    svc = AffiliateTargetProjectionPushService(session_factory=sf)
    with mock.patch.object(Session, "commit", commit_side_effect):
        with pytest.raises(RuntimeError, match="simulated Transaction B commit failure"):
            svc.push(settings=_settings(), transport=httpx.MockTransport(handler), now=1)

    # ちょうど 1 回だけ POST/GET が発生した (retry なし)。
    assert len(calls) == 1

    rows = _independent_rows(url)
    assert len(rows) == 1
    row = rows[0]

    # Transaction A の事実は durable のまま。
    assert row.status == ATPP_RUNNING

    # terminal フィールドは一切 durable に漏れていない (§9)。
    assert row.finished_at is None
    assert row.response_projection_snapshot_hash is None
    assert row.received_count is None
    assert row.inserted_count is None
    assert row.updated_count is None
    assert row.unchanged_count is None
    assert row.error_message is None
    # http_status/server_code は Transaction A では never set — succeeded 分岐でも
    # Transaction B の commit が失敗した以上、durable には反映されていない。
    assert row.http_status is None
    assert row.server_code is None


# ==================== §12: prepared-request reuse (regression) ============
def test_prepared_request_matches_committed_manifest_no_rebuild(tmp_path) -> None:
    """Transaction A commit 後、送信される body の manifest は commit 済みの
    request_manifest_json と論理的に一致する (再構築しない)。0-target のケースで
    完全一致することを確認する (multi-target のケースは D-D1A 側のテストで別途検証済み)。
    """

    url, sf = _prepare_db(tmp_path)
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _ok_handler(request)

    svc = AffiliateTargetProjectionPushService(session_factory=sf)
    svc.push(settings=_settings(), transport=httpx.MockTransport(handler), now=1)

    rows = _independent_rows(url)
    assert rows[0].requested_snapshot_hash == captured["body"]["projection_snapshot_hash"]
    assert rows[0].request_manifest_json == "[]"
    assert captured["body"]["targets"] == []
