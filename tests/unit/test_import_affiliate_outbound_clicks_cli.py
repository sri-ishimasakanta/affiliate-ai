"""scripts/import_affiliate_outbound_clicks.py — PLAN / --execute の orchestration。

WordPress へは一切通信しない (httpx.MockTransport / stub settings / in-memory session)。
service 自体のテストは integration 側で網羅済みなので重複しない。
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AffiliateClickImportRun, AffiliateOutboundClick
from scripts.import_affiliate_outbound_clicks import (
    EXIT_CURSOR_MISMATCH,
    EXIT_NOT_CONFIGURED,
    EXIT_OK,
    main,
    run,
)

_TOK = "tokAAAAAAAAAAAAAAAAA"


def _settings(*, configured: bool = True, base: str | None = "https://runtime.example.test"):
    return SimpleNamespace(
        affiliate_runtime_push_configured=configured and bool(base),
        wordpress_base_url=base,
        affiliate_runtime_shared_secret="x" * 40,
        wordpress_verify_tls=True,
    )


def _sf(session: Session):
    return lambda: contextlib.nullcontext(session)


def _page(rows, *, since_id=0, limit=1000, next_since_id=None):
    if next_since_id is None:
        next_since_id = rows[-1]["id"] if rows else since_id
    return {
        "schema_version": 1,
        "count": len(rows),
        "limit": limit,
        "next_since_id": next_since_id,
        "rows": rows,
    }


def _transport(page, *, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(200, json=page)

    return httpx.MockTransport(handler)


def _no_http():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP expected in plan mode")

    return httpx.MockTransport(handler)


def _runs(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(AffiliateClickImportRun))


# ==================== plan mode =====================================
def test_plan_mode_reports_cursors_and_writes_nothing(session: Session, capsys) -> None:
    code = run(
        execute=False,
        limit=1000,
        settings=_settings(),
        session_factory=_sf(session),
        transport=_no_http(),
    )
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "since_id                    = 0" in out
    assert "replica_max_source_click_id = 0" in out
    assert "cursor_consistent           = True" in out
    assert "limit                       = 1000" in out
    assert "endpoint_host               = https://runtime.example.test" in out
    assert "/wp-json/affiliate-ai/v1/outbound-clicks" in out
    assert "runtime_configured          = True" in out
    assert "execute                     = False" in out
    assert "plan only" in out
    assert _runs(session) == 0


def test_plan_mode_flags_cursor_mismatch_would_fail_closed(
    session: Session, capsys
) -> None:
    seed = AffiliateClickImportRun(
        status="succeeded",
        requested_since_id=0,
        requested_limit=1000,
        response_next_since_id=50,
    )
    session.add(seed)
    session.flush()
    session.add(
        AffiliateOutboundClick(
            source_click_id=10,
            token=_TOK,
            clicked_at=datetime(2026, 9, 1, 12, 0, 0),
            source_import_run_id=seed.id,
        )
    )
    session.commit()

    code = run(
        execute=False,
        limit=1000,
        settings=_settings(),
        session_factory=_sf(session),
        transport=_no_http(),
    )
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "cursor_consistent           = False" in out
    assert "would" in out and "fail closed" in out


def test_plan_mode_works_when_not_configured(session: Session, capsys) -> None:
    code = run(
        execute=False,
        limit=1000,
        settings=_settings(configured=False, base=None),
        session_factory=_sf(session),
        transport=_no_http(),
    )
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "runtime_configured          = False" in out
    assert "endpoint_host               = (not configured)" in out


# ==================== execute mode ==================================
def test_execute_not_configured_returns_exit_code(session: Session, capsys) -> None:
    code = run(
        execute=True,
        limit=1000,
        settings=_settings(configured=False, base=None),
        session_factory=_sf(session),
        transport=_no_http(),
    )
    assert code == EXIT_NOT_CONFIGURED
    assert "NOT CONFIGURED" in capsys.readouterr().out
    assert _runs(session) == 0


def test_execute_happy_prints_summary_and_persists_run(
    session: Session, capsys
) -> None:
    calls: list = []
    code = run(
        execute=True,
        limit=1000,
        settings=_settings(),
        session_factory=_sf(session),
        transport=_transport(
            _page(
                [{"id": 1, "token": _TOK, "clicked_at": "2026-09-01 12:00:00"}],
                since_id=0,
                next_since_id=1,
            ),
            calls=calls,
        ),
    )
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert len(calls) == 1  # exactly one GET
    assert "response_count         = 1" in out
    assert "inserted_count         = 1" in out
    assert "duplicate_count        = 0" in out
    assert "unresolved_token_count = 1" in out
    assert "next_since_id          = 1" in out
    assert "has_more               = False" in out

    row = session.scalars(select(AffiliateClickImportRun)).one()
    assert row.status == "succeeded"
    assert session.scalar(select(func.count()).select_from(AffiliateOutboundClick)) == 1


def test_execute_cursor_mismatch_returns_dedicated_exit(
    session: Session, capsys
) -> None:
    seed = AffiliateClickImportRun(
        status="succeeded",
        requested_since_id=0,
        requested_limit=1000,
        response_next_since_id=50,
    )
    session.add(seed)
    session.flush()
    session.add(
        AffiliateOutboundClick(
            source_click_id=10,
            token=_TOK,
            clicked_at=datetime(2026, 9, 1, 12, 0, 0),
            source_import_run_id=seed.id,
        )
    )
    session.commit()

    code = run(
        execute=True,
        limit=1000,
        settings=_settings(),
        session_factory=_sf(session),
        transport=_no_http(),
    )
    out = capsys.readouterr().out
    assert code == EXIT_CURSOR_MISMATCH
    assert "IMPORT FAILED: local click cursor integrity mismatch" in out


def test_execute_output_excludes_secret_and_signature(
    session: Session, capsys
) -> None:
    run(
        execute=True,
        limit=1000,
        settings=_settings(),
        session_factory=_sf(session),
        transport=_transport(_page([], since_id=0, next_since_id=0)),
    )
    out = capsys.readouterr().out
    assert "x" * 40 not in out
    for banned in ("X-BFL-Signature", "shared_secret", "Authorization", "secret"):
        assert banned not in out


# ==================== argparse ====================================
@pytest.mark.parametrize("bad", ["0", "1001", "-5"])
def test_main_rejects_out_of_range_limit(bad) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--limit", bad])
    assert exc.value.code == 2


def test_main_rejects_unknown_flag() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--nope"])
    assert exc.value.code == 2
