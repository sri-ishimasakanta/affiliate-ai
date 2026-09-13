"""app/affiliate/synthetic_probe.py — state machine / validation / atomic write / lock。

実ネットワークなし (httpx.MockTransport のみ)。full token は state file にのみ
保持される想定 (D-C3-C0.1 §3) — このテストは「それ以外の場所 (通常出力・
例外・lock metadata) に一切現れない」ことを検証する。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import httpx
import pytest

import app.affiliate.synthetic_probe as sp
from app.affiliate.token import TOKEN_PATTERN

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


def _ok_active_handler(request: httpx.Request) -> httpx.Response:
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


def _ok_disable_handler(request: httpx.Request) -> httpx.Response:
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
            "location": sp.PROBE_DESTINATION_URL,
            "cache-control": "no-store",
            "x-robots-tag": "noindex, nofollow",
        },
    )


def _go_404(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404)


def _timeout(request: httpx.Request):
    raise httpx.ConnectTimeout("slow", request=request)


def _no_request(request: httpx.Request):  # pragma: no cover - must never fire
    raise AssertionError("no HTTP request expected")


def _export_page(*, count: int, token: str) -> dict:
    rows = [
        {"id": i, "token": token, "clicked_at": "2026-09-01 00:00:00"}
        for i in range(1, count + 1)
    ]
    return {
        "schema_version": 1,
        "count": count,
        "limit": 1000,
        "next_since_id": count,
        "rows": rows,
    }


def _init(path):
    return sp.action_init(path)


def _to_active_confirmed(path, session, settings, *, active_handler=_ok_active_handler):
    _init(path)
    sp.action_active_plan(path, session)
    state, result = sp.action_active_execute(
        path, settings=settings, transport=httpx.MockTransport(active_handler)
    )
    return state, result


def _to_click_attempted(path, session, settings):
    _to_active_confirmed(path, session, settings)
    try:
        sp.action_go_execute(path, settings=settings, transport=httpx.MockTransport(_go_404))
    except sp.SyntheticProbeAmbiguousError:
        pass
    return sp.action_status(path)


# ==================== token / fingerprint / hash ======================
def test_init_generates_token_exactly_once(tmp_path) -> None:
    path = tmp_path / "probe.json"
    state = _init(path)
    assert TOKEN_PATTERN.match(state["token"])
    assert state["token_fingerprint"] == sp.token_fingerprint(state["token"])

    state2 = _init(path)
    assert state2["token"] == state["token"]  # never regenerated


def test_masked_prefix_never_the_full_token() -> None:
    token = "A" * 22
    assert sp.masked_prefix(token) != token
    assert sp.masked_prefix(token).startswith("AAAA")


def test_link_identity_hash_is_namespaced_and_recomputable(tmp_path) -> None:
    path = tmp_path / "probe.json"
    state = _init(path)
    expected = sp.compute_synthetic_link_identity_hash(state["token"])
    assert state["link_identity_hash"] == expected
    assert len(state["link_identity_hash"]) == 64


def test_probe_destination_is_the_fixed_constant() -> None:
    assert sp.PROBE_DESTINATION_URL == "https://example.com/"
    assert sp.PROBE_DESTINATION_HOST == "example.com"


# ==================== state path resolution ============================
def test_resolve_state_path_prefers_cli_arg_over_settings(tmp_path) -> None:
    target = tmp_path / "a.json"
    settings = _settings(affiliate_probe_state_file=str(tmp_path / "b.json"))
    resolved = sp.resolve_state_path(cli_path=str(target), settings=settings)
    assert resolved == target.resolve()


def test_resolve_state_path_falls_back_to_settings(tmp_path) -> None:
    target = tmp_path / "b.json"
    settings = _settings(affiliate_probe_state_file=str(target))
    resolved = sp.resolve_state_path(cli_path=None, settings=settings)
    assert resolved == target.resolve()


def test_resolve_state_path_fails_closed_when_unconfigured() -> None:
    with pytest.raises(sp.SyntheticProbeConfigError, match="not configured"):
        sp.resolve_state_path(cli_path=None, settings=_settings())


@pytest.mark.parametrize(
    "sub", ["app/affiliate/synthetic_probe.py", "app/affiliate", ""]
)
def test_resolve_state_path_rejects_repo_working_tree(sub) -> None:
    repo_root = sp._repo_root()  # noqa: SLF001 - test-only introspection
    candidate = repo_root / sub if sub else repo_root
    with pytest.raises(sp.SyntheticProbeConfigError, match="outside the repository"):
        sp.resolve_state_path(cli_path=str(candidate), settings=_settings())


def test_resolve_state_path_rejects_missing_parent(tmp_path) -> None:
    missing = tmp_path / "does-not-exist" / "probe.json"
    with pytest.raises(sp.SyntheticProbeConfigError, match="does not exist"):
        sp.resolve_state_path(cli_path=str(missing), settings=_settings())


# ==================== structural / candidate validation =================
def test_load_state_missing_file_fails_closed(tmp_path) -> None:
    with pytest.raises(sp.SyntheticProbeStateError, match="does not exist"):
        sp.load_state(tmp_path / "nope.json")


def test_init_refuses_to_replace_malformed_state(tmp_path) -> None:
    path = tmp_path / "probe.json"
    path.write_text("{not valid json", encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(sp.SyntheticProbeStateError):
        _init(path)
    assert path.read_bytes() == before  # untouched


def test_structural_validation_rejects_fingerprint_mismatch(tmp_path) -> None:
    path = tmp_path / "probe.json"
    _init(path)
    tampered = dict(json.loads(path.read_text(encoding="utf-8")))
    tampered["token_fingerprint"] = "0" * 64
    with pytest.raises(sp.SyntheticProbeStateError, match="fingerprint"):
        sp._validate_structure(tampered)  # noqa: SLF001


def test_structural_validation_rejects_forbidden_field(tmp_path) -> None:
    path = tmp_path / "probe.json"
    state = _init(path)
    tampered = dict(state)
    tampered["shared_secret"] = "leak"
    with pytest.raises(sp.SyntheticProbeStateError, match="unexpected shape"):
        sp._validate_structure(tampered)  # noqa: SLF001


def test_structural_validation_rejects_non_hex64_hash(tmp_path) -> None:
    path = tmp_path / "probe.json"
    state = _init(path)
    tampered = dict(state)
    tampered["active_projection_entry_hash"] = "not-a-hash"
    with pytest.raises(sp.SyntheticProbeStateError, match="hex64"):
        sp._validate_structure(tampered)  # noqa: SLF001


def test_write_state_atomic_rejects_illegal_transition(tmp_path) -> None:
    path = tmp_path / "probe.json"
    state = _init(path)
    candidate = dict(state)
    candidate["state"] = sp.STATE_CLICK_CONFIRMED  # cannot jump from initialized
    with pytest.raises(sp.SyntheticProbeStateError, match="illegal probe state transition"):
        sp.write_state_atomic(path, candidate, previous=state)
    assert sp.load_state(path)["state"] == sp.STATE_INITIALIZED  # untouched


def test_write_state_atomic_rejects_mutated_identity_field(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    _init(path)
    sp.action_active_plan(path, session)
    on_disk = sp.load_state(path)
    candidate = dict(on_disk)
    candidate["link_identity_hash"] = "1" * 64  # attempt to mutate frozen identity
    with pytest.raises(sp.SyntheticProbeStateError):
        sp.write_state_atomic(path, candidate, previous=on_disk)


# ==================== atomic write / crash safety =======================
def test_atomic_replace_preserves_old_bytes_on_replace_failure(tmp_path) -> None:
    path = tmp_path / "probe.json"
    state = _init(path)
    before = path.read_bytes()
    candidate = dict(state)  # unused as a "transition"; exercises _atomic_replace directly
    with mock.patch("app.affiliate.synthetic_probe.os.replace", side_effect=OSError("boom")):
        with pytest.raises(OSError):
            sp._atomic_replace(path, sp._serialize(candidate))  # noqa: SLF001
    assert path.read_bytes() == before
    # no orphaned temp file left behind
    assert not list(tmp_path.glob("probe.json.tmp-*"))


def test_candidate_validation_failure_never_touches_disk(tmp_path) -> None:
    path = tmp_path / "probe.json"
    state = _init(path)
    before = path.read_bytes()
    bad = dict(state)
    bad["state"] = "not-a-real-state"
    with mock.patch("app.affiliate.synthetic_probe._atomic_replace") as replace_mock:
        with pytest.raises(sp.SyntheticProbeStateError):
            sp.write_state_atomic(path, bad, previous=state)
    replace_mock.assert_not_called()
    assert path.read_bytes() == before


# ==================== lock ==============================================
def test_lock_contention_fails_closed_before_any_action(tmp_path) -> None:
    path = tmp_path / "probe.json"
    with sp.ProbeLock(path):
        with pytest.raises(sp.SyntheticProbeLockError):
            with sp.ProbeLock(path):
                pytest.fail("should never enter a second lock")


def test_lock_metadata_contains_no_token_or_secret(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    state = _init(path)
    with sp.ProbeLock(path) as lock:
        lock.record_fingerprint(state["token_fingerprint"])
    meta_path = path.with_name(path.name + ".lock.meta.json")
    meta_text = meta_path.read_text(encoding="utf-8")
    assert state["token"] not in meta_text
    assert _SECRET not in meta_text
    meta = json.loads(meta_text)
    assert set(meta) == {"pid", "acquired_at", "token_fingerprint"}
    assert meta["token_fingerprint"] == state["token_fingerprint"]


def test_lock_dispatch_posix_branch_uses_fcntl_flock(tmp_path, monkeypatch) -> None:
    calls: list[str] = []
    fake_fcntl = SimpleNamespace(
        LOCK_EX=2, LOCK_NB=4, LOCK_UN=8,
        flock=lambda fd, flags: calls.append(f"flock:{flags}"),
    )
    monkeypatch.setattr(sp.os, "name", "posix")
    monkeypatch.setitem(__import__("sys").modules, "fcntl", fake_fcntl)
    fh = open(tmp_path / "l.lock", "a+b")
    try:
        sp._platform_lock(fh, 1)  # noqa: SLF001
        sp._platform_unlock(fh, 1)  # noqa: SLF001
    finally:
        fh.close()
    assert calls == ["flock:6", "flock:8"]


@pytest.mark.skipif(os.name != "nt", reason="msvcrt is Windows-only")
def test_lock_dispatch_windows_branch_uses_msvcrt_locking(tmp_path) -> None:
    fh = open(tmp_path / "l.lock", "a+b")
    try:
        fh.write(b"\x00")
        fh.flush()
        sp._platform_lock(fh, 1)  # noqa: SLF001
        sp._platform_unlock(fh, 1)  # noqa: SLF001
    finally:
        fh.close()


# ==================== --active =========================================
def test_active_plan_freezes_material_once(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    _init(path)
    first = sp.action_active_plan(path, session)
    second = sp.action_active_plan(path, session)  # re-review
    assert first["activated_at"] == second["activated_at"]
    assert first["active_projection_entry_hash"] == second["active_projection_entry_hash"]
    assert first["active_projection_snapshot_hash"] == second["active_projection_snapshot_hash"]


def test_active_execute_requires_active_prepared(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    _init(path)
    with pytest.raises(sp.SyntheticProbeStateError, match="active_prepared"):
        sp.action_active_execute(
            path, settings=_settings(), transport=httpx.MockTransport(_no_request)
        )


def test_active_attempted_is_durable_before_the_post_is_sent(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    _init(path)
    sp.action_active_plan(path, session)

    def handler(request: httpx.Request) -> httpx.Response:
        on_disk = sp.load_state(path)
        assert on_disk["state"] == sp.STATE_ACTIVE_ATTEMPTED
        assert on_disk["active_attempted_at"] is not None
        return _ok_active_handler(request)

    sp.action_active_execute(path, settings=_settings(), transport=httpx.MockTransport(handler))


def test_active_execute_sends_exactly_one_post(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    _init(path)
    sp.action_active_plan(path, session)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _ok_active_handler(request)

    sp.action_active_execute(path, settings=_settings(), transport=httpx.MockTransport(handler))
    assert len(calls) == 1


def test_active_timeout_leaves_active_attempted_and_refuses_reexecute(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    _init(path)
    sp.action_active_plan(path, session)
    with pytest.raises(Exception):  # noqa: B017 - AffiliateProjectionPushError
        sp.action_active_execute(
            path, settings=_settings(), transport=httpx.MockTransport(_timeout)
        )
    assert sp.action_status(path)["state"] == sp.STATE_ACTIVE_ATTEMPTED

    with pytest.raises(sp.SyntheticProbeStateError):
        sp.action_active_execute(
            path, settings=_settings(), transport=httpx.MockTransport(_no_request)
        )


def test_active_execute_requires_configured_runtime(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    _init(path)
    sp.action_active_plan(path, session)
    with pytest.raises(sp.SyntheticProbeConfigError):
        sp.action_active_execute(
            path,
            settings=_settings(affiliate_runtime_push_configured=False),
            transport=httpx.MockTransport(_no_request),
        )


def test_active_plan_rejects_local_token_collision(tmp_path, session) -> None:
    from app.models import AffiliateLinkTarget

    path = tmp_path / "probe.json"
    state = _init(path)
    session.add(
        AffiliateLinkTarget(
            token=state["token"],
            article_id=1,
            affiliate_program_id=1,
            destination_url="https://aff.example.test/x",
            destination_host="aff.example.test",
            status="active",
            link_identity_hash="1" * 64,
        )
    )
    session.commit()
    with pytest.raises(sp.SyntheticProbeStateError, match="collides"):
        sp.action_active_plan(path, session)


# ==================== --go ==============================================
def test_go_refused_before_active_confirmed(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    _init(path)
    with pytest.raises(sp.SyntheticProbeStateError, match="active_confirmed"):
        sp.action_go_execute(
            path, settings=_settings(), transport=httpx.MockTransport(_no_request)
        )
    with pytest.raises(sp.SyntheticProbeStateError):
        sp.action_go_plan(path)


def test_go_attempted_is_durable_before_the_get_is_sent(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    _to_active_confirmed(path, session, _settings())

    def handler(request: httpx.Request) -> httpx.Response:
        on_disk = sp.load_state(path)
        assert on_disk["state"] == sp.STATE_CLICK_ATTEMPTED
        return _go_302(request)

    sp.action_go_execute(path, settings=_settings(), transport=httpx.MockTransport(handler))


def test_go_execute_sends_exactly_one_get_with_no_redirect_follow(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    _to_active_confirmed(path, session, _settings())
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _go_302(request)

    sp.action_go_execute(path, settings=_settings(), transport=httpx.MockTransport(handler))
    assert len(calls) == 1
    assert calls[0].method == "GET"
    assert str(calls[0].url).endswith("/go/" + sp.load_state(path)["token"])


def test_go_never_contacts_example_com(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    _to_active_confirmed(path, session, _settings())
    hosts_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts_seen.append(request.url.host)
        return _go_302(request)

    sp.action_go_execute(path, settings=_settings(), transport=httpx.MockTransport(handler))
    assert hosts_seen == ["runtime.example.test"]
    assert "example.com" not in hosts_seen


def test_go_timeout_leaves_click_attempted(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    _to_active_confirmed(path, session, _settings())
    with pytest.raises(sp.SyntheticProbeAmbiguousError):
        sp.action_go_execute(path, settings=_settings(), transport=httpx.MockTransport(_timeout))
    assert sp.action_status(path)["state"] == sp.STATE_CLICK_ATTEMPTED


def test_second_go_is_refused_after_click_attempted(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    _to_click_attempted(path, session, _settings())
    with pytest.raises(sp.SyntheticProbeStateError):
        sp.action_go_execute(
            path, settings=_settings(), transport=httpx.MockTransport(_no_request)
        )


def test_second_go_is_refused_after_click_confirmed(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    _to_active_confirmed(path, session, _settings())
    sp.action_go_execute(path, settings=_settings(), transport=httpx.MockTransport(_go_302))
    with pytest.raises(sp.SyntheticProbeStateError):
        sp.action_go_execute(
            path, settings=_settings(), transport=httpx.MockTransport(_no_request)
        )


# ==================== --record-import (read-only) =======================
def test_record_import_requires_click_confirmed(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    _init(path)
    with pytest.raises(sp.SyntheticProbeStateError, match="click_confirmed"):
        sp.action_record_import(path, session)


def test_record_import_reads_only_and_validates_single_replica_row(tmp_path, session) -> None:
    from app.models import AffiliateClickImportRun, AffiliateOutboundClick

    path = tmp_path / "probe.json"
    state, _ = _to_active_confirmed(path, session, _settings())
    sp.action_go_execute(path, settings=_settings(), transport=httpx.MockTransport(_go_302))
    token = sp.load_state(path)["token"]

    run = AffiliateClickImportRun(
        status="succeeded", requested_since_id=0, requested_limit=1000, http_status=200,
        response_count=1, response_next_since_id=1, inserted_count=1, duplicate_count=0,
        unresolved_token_count=1, first_source_click_id=1, last_source_click_id=1, has_more=False,
    )
    session.add(run)
    session.flush()
    click = AffiliateOutboundClick(
        source_click_id=1, token=token, clicked_at=datetime(2026, 9, 1), source_import_run_id=run.id
    )
    session.add(click)
    session.commit()

    business_row_count_before = session.query(AffiliateOutboundClick).count()
    confirmed = sp.action_record_import(path, session)
    assert confirmed["state"] == sp.STATE_IMPORT_CONFIRMED
    assert confirmed["source_click_id"] == 1
    assert confirmed["import_run_id"] == run.id
    # read-only: no business rows added/removed
    assert session.query(AffiliateOutboundClick).count() == business_row_count_before


def test_record_import_rejects_zero_matching_rows(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    _to_active_confirmed(path, session, _settings())
    sp.action_go_execute(path, settings=_settings(), transport=httpx.MockTransport(_go_302))
    with pytest.raises(sp.SyntheticProbeStateError, match="exactly one"):
        sp.action_record_import(path, session)


def test_record_import_rejects_more_than_one_matching_row(tmp_path, session) -> None:
    from app.models import AffiliateClickImportRun, AffiliateOutboundClick

    path = tmp_path / "probe.json"
    _to_active_confirmed(path, session, _settings())
    sp.action_go_execute(path, settings=_settings(), transport=httpx.MockTransport(_go_302))
    token = sp.load_state(path)["token"]

    run = AffiliateClickImportRun(
        status="succeeded", requested_since_id=0, requested_limit=1000, http_status=200,
        response_count=2, response_next_since_id=2, inserted_count=2, duplicate_count=0,
        unresolved_token_count=2, first_source_click_id=1, last_source_click_id=2, has_more=False,
    )
    session.add(run)
    session.flush()
    session.add_all(
        [
            AffiliateOutboundClick(
                source_click_id=1,
                token=token,
                clicked_at=datetime(2026, 9, 1),
                source_import_run_id=run.id,
            ),
            AffiliateOutboundClick(
                source_click_id=2,
                token=token,
                clicked_at=datetime(2026, 9, 2),
                source_import_run_id=run.id,
            ),
        ]
    )
    session.commit()
    with pytest.raises(sp.SyntheticProbeStateError, match="exactly one"):
        sp.action_record_import(path, session)


def test_record_import_rejects_run_not_succeeded(tmp_path, session) -> None:
    from app.models import AffiliateClickImportRun, AffiliateOutboundClick

    path = tmp_path / "probe.json"
    _to_active_confirmed(path, session, _settings())
    sp.action_go_execute(path, settings=_settings(), transport=httpx.MockTransport(_go_302))
    token = sp.load_state(path)["token"]

    run = AffiliateClickImportRun(status="running", requested_since_id=0, requested_limit=1000)
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
    with pytest.raises(sp.SyntheticProbeStateError, match="succeeded"):
        sp.action_record_import(path, session)


def test_record_import_rejects_cursor_invariant_violation(tmp_path, session) -> None:
    from app.models import AffiliateClickImportRun, AffiliateOutboundClick

    path = tmp_path / "probe.json"
    _to_active_confirmed(path, session, _settings())
    sp.action_go_execute(path, settings=_settings(), transport=httpx.MockTransport(_go_302))
    token = sp.load_state(path)["token"]

    run = AffiliateClickImportRun(
        status="succeeded", requested_since_id=0, requested_limit=1000, http_status=200,
        response_count=1, response_next_since_id=999,  # deliberately wrong cursor
        inserted_count=1, duplicate_count=0, unresolved_token_count=1,
        first_source_click_id=1, last_source_click_id=1, has_more=False,
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
    with pytest.raises(sp.SyntheticProbeStateError, match="cursor invariant"):
        sp.action_record_import(path, session)


# ==================== --disable =========================================
def _to_import_confirmed(path, session, settings):
    from app.models import AffiliateClickImportRun, AffiliateOutboundClick

    _to_active_confirmed(path, session, settings)
    sp.action_go_execute(path, settings=settings, transport=httpx.MockTransport(_go_302))
    token = sp.load_state(path)["token"]
    run = AffiliateClickImportRun(
        status="succeeded", requested_since_id=0, requested_limit=1000, http_status=200,
        response_count=1, response_next_since_id=1, inserted_count=1, duplicate_count=0,
        unresolved_token_count=1, first_source_click_id=1, last_source_click_id=1, has_more=False,
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
    return sp.action_record_import(path, session)


def test_disable_refused_before_import_confirmed(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    _to_active_confirmed(path, session, _settings())
    with pytest.raises(sp.SyntheticProbeStateError, match="import_confirmed"):
        sp.action_disable_plan(path, session)


def test_disable_plan_freezes_v2_material_and_reuses_active_identity(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    _to_import_confirmed(path, session, _settings())
    active_state = sp.load_state(path)

    disabled = sp.action_disable_plan(path, session)
    assert disabled["disabled_projection_entry_hash"] is not None
    assert disabled["disabled_projection_snapshot_hash"] is not None

    # same immutable identity as the active projection
    assert active_state["token"] == disabled["token"]
    assert active_state["destination_url"] == disabled["destination_url"]
    assert active_state["destination_host"] == disabled["destination_host"]
    assert active_state["link_identity_hash"] == disabled["link_identity_hash"]

    reviewed = sp.action_disable_plan(path, session)  # re-review is idempotent
    assert reviewed["disabled_projection_entry_hash"] == disabled["disabled_projection_entry_hash"]


def test_disable_attempted_is_durable_before_the_post_is_sent(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    _to_import_confirmed(path, session, _settings())
    sp.action_disable_plan(path, session)

    def handler(request: httpx.Request) -> httpx.Response:
        on_disk = sp.load_state(path)
        assert on_disk["state"] == sp.STATE_DISABLE_ATTEMPTED
        return _ok_disable_handler(request)

    sp.action_disable_execute(path, settings=_settings(), transport=httpx.MockTransport(handler))


def test_disable_timeout_leaves_disable_attempted(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    _to_import_confirmed(path, session, _settings())
    sp.action_disable_plan(path, session)
    with pytest.raises(Exception):  # noqa: B017 - AffiliateProjectionPushError
        sp.action_disable_execute(
            path, settings=_settings(), transport=httpx.MockTransport(_timeout)
        )
    assert sp.action_status(path)["state"] == sp.STATE_DISABLE_ATTEMPTED


def test_disable_execute_sends_exactly_one_post(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    _to_import_confirmed(path, session, _settings())
    sp.action_disable_plan(path, session)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _ok_disable_handler(request)

    confirmed, result = sp.action_disable_execute(
        path, settings=_settings(), transport=httpx.MockTransport(handler)
    )
    assert len(calls) == 1
    assert confirmed["state"] == sp.STATE_DISABLED_CONFIRMED
    assert result.updated_count == 1


# ==================== reconciliation ====================================
def test_reconcile_active_requires_active_attempted(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    _init(path)
    sp.action_active_plan(path, session)
    with pytest.raises(sp.SyntheticProbeStateError, match="active_attempted"):
        sp.action_reconcile_active(path, observed="present")


def test_reconcile_active_absent_preserves_frozen_material(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    _init(path)
    sp.action_active_plan(path, session)
    frozen = sp.load_state(path)
    with pytest.raises(Exception):  # noqa: B017
        sp.action_active_execute(
            path, settings=_settings(), transport=httpx.MockTransport(_timeout)
        )
    reconciled = sp.action_reconcile_active(path, observed="absent")
    assert reconciled["state"] == sp.STATE_ACTIVE_PREPARED
    assert reconciled["token"] == frozen["token"]
    assert reconciled["active_projection_entry_hash"] == frozen["active_projection_entry_hash"]
    # a fresh attempt is now legal again
    confirmed, _ = sp.action_active_execute(
        path, settings=_settings(), transport=httpx.MockTransport(_ok_active_handler)
    )
    assert confirmed["state"] == sp.STATE_ACTIVE_CONFIRMED


def test_reconcile_active_present_confirms_without_new_post(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    _init(path)
    sp.action_active_plan(path, session)
    with pytest.raises(Exception):  # noqa: B017
        sp.action_active_execute(
            path, settings=_settings(), transport=httpx.MockTransport(_timeout)
        )
    reconciled = sp.action_reconcile_active(path, observed="present")
    assert reconciled["state"] == sp.STATE_ACTIVE_CONFIRMED


@pytest.mark.parametrize(
    ("count", "expected_state"),
    [(0, sp.STATE_ACTIVE_CONFIRMED), (1, sp.STATE_CLICK_CONFIRMED)],
)
def test_reconcile_go_machine_verified_outcomes(tmp_path, session, count, expected_state) -> None:
    path = tmp_path / "probe.json"
    state = _to_click_attempted(path, session, _settings())
    token = state["token"]

    def export_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_export_page(count=count, token=token))

    reconciled = sp.action_reconcile_go(
        path, settings=_settings(), transport=httpx.MockTransport(export_handler)
    )
    assert reconciled["state"] == expected_state
    assert reconciled["click_contaminated"] is False


def test_reconcile_go_contamination_fails_closed_and_never_deletes(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    state = _to_click_attempted(path, session, _settings())
    token = state["token"]

    def export_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_export_page(count=2, token=token))

    reconciled = sp.action_reconcile_go(
        path, settings=_settings(), transport=httpx.MockTransport(export_handler)
    )
    assert reconciled["state"] == sp.STATE_CLICK_ATTEMPTED
    assert reconciled["click_contaminated"] is True
    # once set, contamination cannot be silently cleared by a later write
    with pytest.raises(sp.SyntheticProbeStateError, match="cleared"):
        sp.write_state_atomic(
            path,
            {**reconciled, "click_contaminated": False},
            previous=reconciled,
        )


def test_reconcile_disable_requires_disable_attempted(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    _to_import_confirmed(path, session, _settings())
    sp.action_disable_plan(path, session)
    with pytest.raises(sp.SyntheticProbeStateError, match="disable_attempted"):
        sp.action_reconcile_disable(path, observed="present")


def test_reconcile_disable_absent_allows_retry_with_same_identity(tmp_path, session) -> None:
    path = tmp_path / "probe.json"
    _to_import_confirmed(path, session, _settings())
    sp.action_disable_plan(path, session)
    frozen = sp.load_state(path)
    with pytest.raises(Exception):  # noqa: B017
        sp.action_disable_execute(
            path, settings=_settings(), transport=httpx.MockTransport(_timeout)
        )
    reconciled = sp.action_reconcile_disable(path, observed="absent")
    assert reconciled["state"] == sp.STATE_DISABLE_PREPARED
    assert reconciled["disabled_projection_entry_hash"] == frozen["disabled_projection_entry_hash"]
    confirmed, _ = sp.action_disable_execute(
        path, settings=_settings(), transport=httpx.MockTransport(_ok_disable_handler)
    )
    assert confirmed["state"] == sp.STATE_DISABLED_CONFIRMED


# ==================== no auto-retry (uniform) ===========================
@pytest.mark.parametrize(
    "action_name",
    ["active", "go", "disable"],
)
def test_no_automatic_retry_on_ambiguous_failure(tmp_path, session, action_name) -> None:
    path = tmp_path / "probe.json"
    if action_name == "active":
        _init(path)
        sp.action_active_plan(path, session)
        call = lambda transport: sp.action_active_execute(  # noqa: E731
            path, settings=_settings(), transport=transport
        )
    elif action_name == "go":
        _to_active_confirmed(path, session, _settings())
        call = lambda transport: sp.action_go_execute(  # noqa: E731
            path, settings=_settings(), transport=transport
        )
    else:
        _to_import_confirmed(path, session, _settings())
        sp.action_disable_plan(path, session)
        call = lambda transport: sp.action_disable_execute(  # noqa: E731
            path, settings=_settings(), transport=transport
        )

    counting = {"n": 0}

    def handler(request: httpx.Request):
        counting["n"] += 1
        raise httpx.ConnectTimeout("slow", request=request)

    with pytest.raises(Exception):  # noqa: B017
        call(httpx.MockTransport(handler))
    assert counting["n"] == 1  # exactly one attempt, no retry
