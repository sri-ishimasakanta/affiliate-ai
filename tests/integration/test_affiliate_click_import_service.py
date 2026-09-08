"""AffiliateClickImportService.import_next_page —
pre-network cursor integrity / timestamp normalization / duplicate & drift /
post-import invariant / empty & non-empty success / safety。

実ネットワークなし (httpx.MockTransport)。
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.exceptions import AffiliateClickImportError
from app.models import (
    AffiliateClickImportRun,
    AffiliateLinkTarget,
    AffiliateOutboundClick,
)
from app.services.affiliate_click_import_service import AffiliateClickImportService

_TOK_A = "tokAAAAAAAAAAAAAAAAA"
_TOK_B = "tokBBBBBBBBBBBBBBBBB"
_UNKNOWN = "tokUNKNOWN0000000000"


class _Settings:
    def __init__(self, *, configured: bool = True) -> None:
        self.affiliate_runtime_push_configured = configured
        self.wordpress_base_url = "https://runtime.example.test"
        self.affiliate_runtime_shared_secret = "x" * 40
        self.wordpress_verify_tls = True


def _r(rid: int, token: str = _TOK_A, clicked_at: str = "2026-09-01 12:00:00") -> dict:
    return {"id": rid, "token": token, "clicked_at": clicked_at}


def _page(rows: list[dict], *, since_id: int = 0, limit: int = 1000, next_since_id=None) -> dict:
    if next_since_id is None:
        next_since_id = rows[-1]["id"] if rows else since_id
    return {
        "schema_version": 1,
        "count": len(rows),
        "limit": limit,
        "next_since_id": next_since_id,
        "rows": rows,
    }


def _transport(page: dict, *, calls: list | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(200, json=page)

    return httpx.MockTransport(handler)


def _no_http() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP request expected")

    return httpx.MockTransport(handler)


def _seed_target(session: Session, token: str, *, art: int = 1, prog: int = 1) -> None:
    session.add(
        AffiliateLinkTarget(
            token=token,
            article_id=art,
            affiliate_program_id=prog,
            destination_url="https://aff.example.test/x",
            destination_host="aff.example.test",
            status="active",
            link_identity_hash="1" * 64,
        )
    )
    session.flush()


def _run_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(AffiliateClickImportRun))


def _click_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(AffiliateOutboundClick))


def _svc(session: Session) -> AffiliateClickImportService:
    return AffiliateClickImportService(session)


# ==================== config / arg guards ============================
def test_not_configured_creates_no_run_and_no_http(session: Session) -> None:
    with pytest.raises(AffiliateClickImportError):
        _svc(session).import_next_page(
            settings=_Settings(configured=False), transport=_no_http()
        )
    assert _run_count(session) == 0


@pytest.mark.parametrize("limit", [0, 1001, True])
def test_bad_limit_creates_no_run(session: Session, limit) -> None:
    with pytest.raises(AffiliateClickImportError):
        _svc(session).import_next_page(
            limit=limit, settings=_Settings(), transport=_no_http()
        )
    assert _run_count(session) == 0


# ==================== empty-page success (0/0) =======================
def test_empty_first_state_succeeds(session: Session) -> None:
    calls: list = []
    run = _svc(session).import_next_page(
        settings=_Settings(),
        transport=_transport(_page([], since_id=0, next_since_id=0), calls=calls),
    )
    assert len(calls) == 1  # exactly one GET
    assert run.status == "succeeded"
    assert run.http_status == 200
    assert run.requested_since_id == 0
    assert run.response_count == 0
    assert run.inserted_count == 0
    assert run.duplicate_count == 0
    assert run.unresolved_token_count == 0
    assert run.first_source_click_id is None
    assert run.last_source_click_id is None
    assert run.response_next_since_id == 0
    assert run.has_more is False
    assert _click_count(session) == 0
    assert run.response_snapshot == {
        "schema_version": 1,
        "count": 0,
        "limit": 1000,
        "next_since_id": 0,
        "inserted_count": 0,
        "duplicate_count": 0,
        "unresolved_token_count": 0,
        "has_more": False,
    }


# ==================== non-empty success (0/0 -> N) ===================
def test_non_empty_first_import_inserts_rows(session: Session) -> None:
    _seed_target(session, _TOK_A)
    session.commit()

    run = _svc(session).import_next_page(
        settings=_Settings(),
        transport=_transport(
            _page([_r(1, _TOK_A), _r(2, _UNKNOWN)], since_id=0)
        ),
    )
    assert run.status == "succeeded"
    assert run.inserted_count == 2
    assert run.duplicate_count == 0
    assert run.unresolved_token_count == 1  # _UNKNOWN has no local target (row count)
    assert run.first_source_click_id == 1
    assert run.last_source_click_id == 2
    assert run.response_next_since_id == 2
    assert run.has_more is False

    rows = session.scalars(
        select(AffiliateOutboundClick).order_by(AffiliateOutboundClick.source_click_id)
    ).all()
    assert [r.source_click_id for r in rows] == [1, 2]
    assert rows[0].clicked_at == datetime(2026, 9, 1, 12, 0, 0)  # naive UTC wall-clock
    assert rows[0].clicked_at.tzinfo is None
    assert rows[0].source_import_run_id == run.id


def test_two_consecutive_imports_advance_the_cursor(session: Session) -> None:
    svc = _svc(session)
    svc.import_next_page(
        settings=_Settings(), transport=_transport(_page([_r(1), _r(2)], since_id=0))
    )
    # run cursor == replica cursor == 2 → second page starts at since_id=2
    run2 = svc.import_next_page(
        settings=_Settings(),
        transport=_transport(_page([_r(3)], since_id=2, next_since_id=3)),
    )
    assert run2.status == "succeeded"
    assert run2.requested_since_id == 2
    assert run2.inserted_count == 1
    assert _click_count(session) == 3
    assert _run_count(session) == 2


# ==================== pre-network cursor integrity ===================
def _seed_succeeded_run(session: Session, next_since_id: int) -> AffiliateClickImportRun:
    run = AffiliateClickImportRun(
        status="succeeded",
        requested_since_id=0,
        requested_limit=1000,
        http_status=200,
        response_count=1,
        response_next_since_id=next_since_id,
        inserted_count=1,
        duplicate_count=0,
        unresolved_token_count=0,
        has_more=False,
        finished_at=datetime.now(UTC).replace(tzinfo=None),
    )
    session.add(run)
    session.flush()
    return run


def _seed_click(session: Session, rid: int, run_id: int, token: str = _TOK_A) -> None:
    session.add(
        AffiliateOutboundClick(
            source_click_id=rid,
            token=token,
            clicked_at=datetime(2026, 9, 1, 12, 0, 0),
            source_import_run_id=run_id,
        )
    )
    session.flush()


@pytest.mark.parametrize(
    ("run_cursor", "replica_cursor"),
    [(100, 80), (80, 100)],
)
def test_cursor_mismatch_fails_closed_before_http(
    session: Session, run_cursor: int, replica_cursor: int
) -> None:
    from app.exceptions import AffiliateClickImportError

    seed_run = _seed_succeeded_run(session, run_cursor)
    _seed_click(session, replica_cursor, seed_run.id)
    session.commit()
    runs_before = _run_count(session)

    with pytest.raises(AffiliateClickImportError, match="cursor integrity mismatch"):
        _svc(session).import_next_page(settings=_Settings(), transport=_no_http())

    assert _run_count(session) == runs_before  # no new run row
    assert _click_count(session) == 1  # unchanged


def test_consistent_100_100_passes(session: Session) -> None:
    seed_run = _seed_succeeded_run(session, 100)
    _seed_click(session, 100, seed_run.id)
    session.commit()

    run = _svc(session).import_next_page(
        settings=_Settings(),
        transport=_transport(_page([_r(101)], since_id=100, next_since_id=101)),
    )
    assert run.status == "succeeded"
    assert run.requested_since_id == 100
    assert run.inserted_count == 1
    assert _click_count(session) == 2


# ==================== duplicate / drift (misbehaving server) =========
def _pin_cursors_to_zero(monkeypatch, svc: AffiliateClickImportService) -> None:
    """replica に既存行があるのに export が再送してくる状況を再現するため、
    preflight を通過させる (server 側 misbehavior のシミュレーション)。"""

    monkeypatch.setattr(svc, "_run_cursor", lambda: 0)
    monkeypatch.setattr(svc, "_replica_cursor", lambda: 0)


def test_identical_refetch_is_duplicate_not_drift(
    session: Session, monkeypatch
) -> None:
    svc = _svc(session)
    svc.import_next_page(
        settings=_Settings(),
        transport=_transport(_page([_r(1, _TOK_A), _r(2, _TOK_A)], since_id=0)),
    )
    assert _click_count(session) == 2

    _pin_cursors_to_zero(monkeypatch, svc)
    run2 = svc.import_next_page(
        settings=_Settings(),
        transport=_transport(_page([_r(1, _TOK_A), _r(2, _TOK_A)], since_id=0)),
    )
    assert run2.status == "succeeded"
    assert run2.inserted_count == 0
    assert run2.duplicate_count == 2
    assert _click_count(session) == 2  # nothing added


@pytest.mark.parametrize(
    "drift_row",
    [
        _r(1, _TOK_B),  # different token
        _r(1, _TOK_A, "2026-09-01 12:00:01"),  # normalized clicked_at differs by 1s
    ],
)
def test_source_drift_rolls_back_whole_page(
    session: Session, monkeypatch, drift_row: dict
) -> None:
    from app.exceptions import AffiliateClickImportError

    svc = _svc(session)
    svc.import_next_page(
        settings=_Settings(),
        transport=_transport(_page([_r(1, _TOK_A), _r(2, _TOK_A)], since_id=0)),
    )
    _pin_cursors_to_zero(monkeypatch, svc)

    with pytest.raises(AffiliateClickImportError, match="source click drift"):
        svc.import_next_page(
            settings=_Settings(),
            transport=_transport(
                _page([drift_row, _r(3, _TOK_A)], since_id=0, next_since_id=3)
            ),
        )

    # whole page rolled back: no new click rows, original token untouched
    rows = session.scalars(
        select(AffiliateOutboundClick).order_by(AffiliateOutboundClick.source_click_id)
    ).all()
    assert [r.source_click_id for r in rows] == [1, 2]
    assert rows[0].token == _TOK_A
    latest = session.scalars(
        select(AffiliateClickImportRun).order_by(AffiliateClickImportRun.id.desc())
    ).first()
    assert latest.status == "failed"
    assert latest.error_message == "source click drift for source_click_id 1"


def test_mid_page_drift_inserts_nothing(session: Session, monkeypatch) -> None:
    from app.exceptions import AffiliateClickImportError

    svc = _svc(session)
    svc.import_next_page(
        settings=_Settings(),
        transport=_transport(_page([_r(11, _TOK_A)], since_id=0, next_since_id=11)),
    )
    assert _click_count(session) == 1
    _pin_cursors_to_zero(monkeypatch, svc)

    with pytest.raises(AffiliateClickImportError, match="source click drift"):
        svc.import_next_page(
            settings=_Settings(),
            transport=_transport(
                _page(
                    [_r(10, _TOK_A), _r(11, _TOK_B), _r(12, _TOK_A)],
                    since_id=0,
                    next_since_id=12,
                )
            ),
        )
    assert _click_count(session) == 1  # 10 and 12 not inserted


# ==================== post-import cursor invariant ===================
def test_post_import_invariant_violation_fails_run(
    session: Session, monkeypatch
) -> None:
    from app.exceptions import AffiliateClickImportError

    svc = _svc(session)
    calls = {"n": 0}
    real_max = svc._clicks.max_source_click_id

    def flaky_max() -> int | None:
        calls["n"] += 1
        # 1st call = pre-network replica cursor (0). Later call (post-insert) lies.
        return real_max() if calls["n"] == 1 else 999

    monkeypatch.setattr(svc._clicks, "max_source_click_id", flaky_max)

    with pytest.raises(AffiliateClickImportError, match="post-import click cursor invariant"):
        svc.import_next_page(
            settings=_Settings(),
            transport=_transport(_page([_r(1), _r(2)], since_id=0)),
        )

    assert _click_count(session) == 0  # page rolled back
    run = session.scalars(select(AffiliateClickImportRun)).first()
    assert run.status == "failed"
    assert run.error_message == "post-import click cursor invariant violated"


# ==================== safety / schema ===============================
def test_click_model_has_no_pii_or_destination_columns() -> None:
    cols = set(AffiliateOutboundClick.__table__.columns.keys())
    forbidden = {
        "ip", "hashed_ip", "ip_hash", "user_agent", "referer", "referrer",
        "cookie", "session_id", "user_id", "email", "device", "destination_url",
        "destination", "article_id", "affiliate_program_id", "source_event_hash",
        "updated_at",
    }
    assert cols.isdisjoint(forbidden)
    assert cols == {
        "id", "source_click_id", "token", "clicked_at", "source_import_run_id",
        "created_at",
    }


def test_run_storage_has_no_secret_or_body_columns() -> None:
    cols = set(AffiliateClickImportRun.__table__.columns.keys())
    forbidden = {
        "shared_secret", "secret", "signature", "response_body", "raw_body",
        "headers", "request_headers", "authorization", "updated_at",
    }
    assert cols.isdisjoint(forbidden)


def test_success_snapshot_only_contains_safe_counts(session: Session) -> None:
    _seed_target(session, _TOK_A)
    session.commit()
    run = _svc(session).import_next_page(
        settings=_Settings(),
        transport=_transport(_page([_r(1, _TOK_A)], since_id=0, next_since_id=1)),
    )
    assert set(run.response_snapshot) == {
        "schema_version", "count", "limit", "next_since_id",
        "inserted_count", "duplicate_count", "unresolved_token_count", "has_more",
    }
