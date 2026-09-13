"""scripts/probe_synthetic_runtime_click.py — action dispatch / lock / exit codes。

実ネットワークなし (httpx.MockTransport のみ)。full token が stdout に一切
現れないことを全 action で確認する。
"""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.orm import Session

import app.affiliate.synthetic_probe as probe
import scripts.probe_synthetic_runtime_click as cli

_SECRET = "s" * 40
_BASE = "https://runtime.example.test"


def _settings(**over):
    base = dict(
        affiliate_runtime_push_configured=True,
        wordpress_base_url=_BASE,
        affiliate_runtime_shared_secret=_SECRET,
        wordpress_verify_tls=True,
        affiliate_probe_state_file=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _sf(session: Session):
    return lambda: contextlib.nullcontext(session)


def _ok_active(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(
        200,
        json={
            "schema_version": 1,
            "projection_snapshot_hash": body["projection_snapshot_hash"],
            "received_count": 1,
            "inserted_count": 1,
            "updated_count": 0,
            "unchanged_count": 0,
        },
    )


def _ok_disable(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(
        200,
        json={
            "schema_version": 1,
            "projection_snapshot_hash": body["projection_snapshot_hash"],
            "received_count": 1,
            "inserted_count": 0,
            "updated_count": 1,
            "unchanged_count": 0,
        },
    )


def _go_302(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        302,
        headers={
            "location": probe.PROBE_DESTINATION_URL,
            "cache-control": "no-store",
            "x-robots-tag": "noindex, nofollow",
        },
    )


def _no_request(request: httpx.Request):  # pragma: no cover - must never fire
    raise AssertionError("no HTTP request expected")


def _full_token(path) -> str:
    return json.loads(path.read_text(encoding="utf-8"))["token"]


def _assert_no_token_leak(capsys, token: str) -> None:
    out = capsys.readouterr().out
    assert token not in out


# ==================== basic dispatch ====================================
def test_init_then_status_no_token_leak(tmp_path, session, capsys) -> None:
    path = str(tmp_path / "probe.json")
    code = cli.run(
        action="init", state_file=path, settings=_settings(), session_factory=_sf(session)
    )
    assert code == cli.EXIT_OK
    token = _full_token(tmp_path / "probe.json")
    _assert_no_token_leak(capsys, token)

    code = cli.run(
        action="status", state_file=path, settings=_settings(), session_factory=_sf(session)
    )
    assert code == cli.EXIT_OK
    _assert_no_token_leak(capsys, token)


def test_state_file_not_configured_returns_exit_code(session) -> None:
    code = cli.run(
        action="status",
        state_file=None,
        settings=_settings(affiliate_probe_state_file=None),
        session_factory=_sf(session),
    )
    assert code == cli.EXIT_CONFIG_ERROR


def test_active_plan_then_execute_happy_path(tmp_path, session, capsys) -> None:
    path = str(tmp_path / "probe.json")
    cli.run(action="init", state_file=path, settings=_settings(), session_factory=_sf(session))
    code = cli.run(
        action="active", state_file=path, settings=_settings(), session_factory=_sf(session)
    )
    assert code == cli.EXIT_OK

    token = _full_token(tmp_path / "probe.json")
    code = cli.run(
        action="active",
        execute=True,
        state_file=path,
        settings=_settings(),
        session_factory=_sf(session),
        transport=httpx.MockTransport(_ok_active),
    )
    assert code == cli.EXIT_OK
    st = probe.load_state(tmp_path / "probe.json")
    assert st["state"] == probe.STATE_ACTIVE_CONFIRMED
    _assert_no_token_leak(capsys, token)


def test_active_execute_not_configured(tmp_path, session) -> None:
    path = str(tmp_path / "probe.json")
    cli.run(action="init", state_file=path, settings=_settings(), session_factory=_sf(session))
    cli.run(action="active", state_file=path, settings=_settings(), session_factory=_sf(session))
    code = cli.run(
        action="active",
        execute=True,
        state_file=path,
        settings=_settings(affiliate_runtime_push_configured=False),
        session_factory=_sf(session),
        transport=httpx.MockTransport(_no_request),
    )
    assert code == cli.EXIT_CONFIG_ERROR


def test_active_execute_timeout_returns_ambiguous_exit(tmp_path, session) -> None:
    path = str(tmp_path / "probe.json")
    cli.run(action="init", state_file=path, settings=_settings(), session_factory=_sf(session))
    cli.run(action="active", state_file=path, settings=_settings(), session_factory=_sf(session))

    def timeout(request):
        raise httpx.ConnectTimeout("slow", request=request)

    code = cli.run(
        action="active",
        execute=True,
        state_file=path,
        settings=_settings(),
        session_factory=_sf(session),
        transport=httpx.MockTransport(timeout),
    )
    assert code == cli.EXIT_AMBIGUOUS
    assert probe.load_state(tmp_path / "probe.json")["state"] == probe.STATE_ACTIVE_ATTEMPTED


def _full_lifecycle_to_click_confirmed(path_str, tmp_path, session):
    cli.run(
        action="init", state_file=path_str, settings=_settings(), session_factory=_sf(session)
    )
    cli.run(
        action="active", state_file=path_str, settings=_settings(), session_factory=_sf(session)
    )
    cli.run(
        action="active",
        execute=True,
        state_file=path_str,
        settings=_settings(),
        session_factory=_sf(session),
        transport=httpx.MockTransport(_ok_active),
    )
    cli.run(action="go", state_file=path_str, settings=_settings(), session_factory=_sf(session))
    cli.run(
        action="go",
        execute=True,
        state_file=path_str,
        settings=_settings(),
        session_factory=_sf(session),
        transport=httpx.MockTransport(_go_302),
    )


def test_go_plan_then_execute_happy_path(tmp_path, session, capsys) -> None:
    path = str(tmp_path / "probe.json")
    _full_lifecycle_to_click_confirmed(path, tmp_path, session)
    token = _full_token(tmp_path / "probe.json")
    st = probe.load_state(tmp_path / "probe.json")
    assert st["state"] == probe.STATE_CLICK_CONFIRMED
    _assert_no_token_leak(capsys, token)


def test_go_plan_refused_before_active_confirmed(tmp_path, session) -> None:
    path = str(tmp_path / "probe.json")
    cli.run(action="init", state_file=path, settings=_settings(), session_factory=_sf(session))
    code = cli.run(action="go", state_file=path, settings=_settings(), session_factory=_sf(session))
    assert code == cli.EXIT_STATE_ERROR


def test_record_import_reads_only_and_advances_state(tmp_path, session, capsys) -> None:
    from datetime import datetime

    from app.models import AffiliateClickImportRun, AffiliateOutboundClick

    path = str(tmp_path / "probe.json")
    _full_lifecycle_to_click_confirmed(path, tmp_path, session)
    token = probe.load_state(tmp_path / "probe.json")["token"]

    run = AffiliateClickImportRun(
        status="succeeded",
        requested_since_id=0,
        requested_limit=1000,
        http_status=200,
        response_count=1,
        response_next_since_id=1,
        inserted_count=1,
        duplicate_count=0,
        unresolved_token_count=1,
        first_source_click_id=1,
        last_source_click_id=1,
        has_more=False,
    )
    session.add(run)
    session.flush()
    session.add(
        AffiliateOutboundClick(
            source_click_id=1,
            token=token,
            clicked_at=datetime(2026, 9, 1),
            source_import_run_id=run.id,
        )
    )
    session.commit()

    code = cli.run(
        action="record_import", state_file=path, settings=_settings(), session_factory=_sf(session)
    )
    assert code == cli.EXIT_OK
    st = probe.load_state(tmp_path / "probe.json")
    assert st["state"] == probe.STATE_IMPORT_CONFIRMED
    assert st["source_click_id"] == 1
    _assert_no_token_leak(capsys, token)


def test_disable_full_lifecycle(tmp_path, session, capsys) -> None:
    from datetime import datetime

    from app.models import AffiliateClickImportRun, AffiliateOutboundClick

    path = str(tmp_path / "probe.json")
    _full_lifecycle_to_click_confirmed(path, tmp_path, session)
    token = probe.load_state(tmp_path / "probe.json")["token"]

    run = AffiliateClickImportRun(
        status="succeeded",
        requested_since_id=0,
        requested_limit=1000,
        http_status=200,
        response_count=1,
        response_next_since_id=1,
        inserted_count=1,
        duplicate_count=0,
        unresolved_token_count=1,
        first_source_click_id=1,
        last_source_click_id=1,
        has_more=False,
    )
    session.add(run)
    session.flush()
    session.add(
        AffiliateOutboundClick(
            source_click_id=1,
            token=token,
            clicked_at=datetime(2026, 9, 1),
            source_import_run_id=run.id,
        )
    )
    session.commit()
    cli.run(
        action="record_import",
        state_file=path,
        settings=_settings(),
        session_factory=_sf(session),
    )

    code = cli.run(
        action="disable", state_file=path, settings=_settings(), session_factory=_sf(session)
    )
    assert code == cli.EXIT_OK
    code = cli.run(
        action="disable",
        execute=True,
        state_file=path,
        settings=_settings(),
        session_factory=_sf(session),
        transport=httpx.MockTransport(_ok_disable),
    )
    assert code == cli.EXIT_OK
    st = probe.load_state(tmp_path / "probe.json")
    assert st["state"] == probe.STATE_DISABLED_CONFIRMED
    _assert_no_token_leak(capsys, token)


# ==================== reconciliation dispatch ============================
def test_reconcile_active_requires_observed_via_argparse(tmp_path) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--reconcile-active", "--state-file", str(tmp_path / "p.json")])
    assert exc.value.code == 2


def test_reconcile_disable_requires_observed_via_argparse(tmp_path) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--reconcile-disable", "--state-file", str(tmp_path / "p.json")])
    assert exc.value.code == 2


def test_reconcile_active_absent_via_run(tmp_path, session) -> None:
    path = str(tmp_path / "probe.json")
    cli.run(action="init", state_file=path, settings=_settings(), session_factory=_sf(session))
    cli.run(action="active", state_file=path, settings=_settings(), session_factory=_sf(session))

    def timeout(request):
        raise httpx.ConnectTimeout("slow", request=request)

    cli.run(
        action="active",
        execute=True,
        state_file=path,
        settings=_settings(),
        session_factory=_sf(session),
        transport=httpx.MockTransport(timeout),
    )
    code = cli.run(
        action="reconcile_active", observed="absent", state_file=path, settings=_settings(),
        session_factory=_sf(session),
    )
    assert code == cli.EXIT_OK
    assert probe.load_state(tmp_path / "probe.json")["state"] == probe.STATE_ACTIVE_PREPARED


# ==================== lock contention at CLI level =======================
def test_lock_contention_returns_dedicated_exit_with_zero_network(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    cli.run(
        action="init", state_file=str(path), settings=_settings(), session_factory=_sf(session)
    )
    with probe.ProbeLock(path):
        code = cli.run(
            action="status",
            state_file=str(path),
            settings=_settings(),
            session_factory=_sf(session),
            transport=httpx.MockTransport(_no_request),
        )
    assert code == cli.EXIT_LOCK_CONTENTION


# ==================== mutually exclusive actions ==========================
def test_main_requires_exactly_one_action(tmp_path) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--init", "--status", "--state-file", str(tmp_path / "p.json")])
    assert exc.value.code == 2


def test_main_rejects_unknown_flag() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--nope"])
    assert exc.value.code == 2


# ==================== production projection CLI unchanged =================
def test_production_projection_cli_has_no_probe_arguments() -> None:
    import scripts.push_affiliate_target_projection as prod_cli

    with pytest.raises(SystemExit) as exc:
        prod_cli.main(["--state-file", "x"])
    assert exc.value.code == 2  # unrecognized argument: no --state-file exists

    with pytest.raises(SystemExit) as exc:
        prod_cli.main(["--synthetic"])
    assert exc.value.code == 2

    with pytest.raises(SystemExit) as exc:
        prod_cli.main(["--observed", "present"])
    assert exc.value.code == 2
