"""app/affiliate/projection.py — WordPress runtime projection contract。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.affiliate.projection import (
    IMMUTABLE_ENTRY_FIELDS,
    PROJECTION_STATUS_ACTIVE,
    PROJECTION_STATUS_DISABLED,
    PROJECTION_VERSION_ACTIVE,
    PROJECTION_VERSION_DISABLED,
    REJECT_FORBIDDEN_REACTIVATION,
    REJECT_HASH_CONFLICT,
    REJECT_IMMUTABLE_DRIFT,
    REJECT_STALE_VERSION,
    UPSERT_INSERT,
    UPSERT_NOOP,
    UPSERT_UPDATE,
    AffiliateProjectionError,
    build_projection_batch_request,
    build_projection_entry,
    build_projection_snapshot,
    build_snapshot_from_targets,
    decide_projection_upsert,
    evaluate_projection_batch,
    projection_from_target,
)

_DEST = "https://aff.example.test/track/x?a8mat=ABC123&utm_source=n#frag"
_HOST = "aff.example.test"
_LH = "a" * 64
_C = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
_D = datetime(2026, 9, 3, 9, 30, 0, tzinfo=UTC)


def _target(
    *,
    token: str,
    dest: str = _DEST,
    host: str = _HOST,
    link_hash: str = _LH,
    status: str = "active",
    created: datetime = _C,
    disabled: datetime | None = None,
):
    return SimpleNamespace(
        token=token,
        destination_url=dest,
        destination_host=host,
        link_identity_hash=link_hash,
        status=status,
        created_at=created,
        disabled_at=disabled,
    )


def _entry(**kw):
    base = dict(
        token="AAAAtoken000000000000",
        destination_url=_DEST,
        destination_host=_HOST,
        link_identity_hash=_LH,
        alt_status="active",
        activated_at=_C,
        disabled_at=None,
    )
    base.update(kw)
    return build_projection_entry(**base)


# ==================== status / version mapping =====================
def test_active_target_projects_version_1() -> None:
    p = projection_from_target(_target(token="AAAA111111111111111111"))
    assert p.status == PROJECTION_STATUS_ACTIVE
    assert p.projection_version == PROJECTION_VERSION_ACTIVE
    assert p.disabled_at is None
    assert p.activated_at == "2026-09-01T12:00:00+00:00"


def test_disabled_target_projects_version_2() -> None:
    p = projection_from_target(
        _target(token="BBBB222222222222222222", status="disabled", disabled=_D)
    )
    assert p.status == PROJECTION_STATUS_DISABLED
    assert p.projection_version == PROJECTION_VERSION_DISABLED
    assert p.disabled_at == "2026-09-03T09:30:00+00:00"


def test_superseded_target_projects_as_disabled_version_2() -> None:
    p = projection_from_target(
        _target(token="CCCC333333333333333333", status="superseded", disabled=_D)
    )
    assert p.status == PROJECTION_STATUS_DISABLED
    assert p.projection_version == PROJECTION_VERSION_DISABLED


# ==================== exact preservation + determinism =============
def test_destination_url_preserved_exactly() -> None:
    p = _entry()
    assert p.destination_url == _DEST
    assert "a8mat=ABC123" in p.destination_url
    assert "utm_source=n" in p.destination_url
    assert p.destination_url.endswith("#frag")


def test_projection_entry_is_deterministic() -> None:
    a = _entry()
    b = _entry()
    assert a == b
    assert a.projection_entry_hash == b.projection_entry_hash
    assert len(a.projection_entry_hash) == 64


@pytest.mark.parametrize(
    "override",
    [
        {"destination_url": _DEST + "&z=9"},
        {"destination_host": "other.example.test", "override_url_host": True},
        {"link_identity_hash": "b" * 64},
        {"alt_status": "disabled", "disabled_at": _D},
        {"activated_at": datetime(2020, 1, 1, tzinfo=UTC)},
    ],
)
def test_entry_hash_is_component_sensitive(override: dict) -> None:
    base = _entry()
    kw = dict(override)
    if kw.pop("override_url_host", False):
        kw["destination_url"] = "https://other.example.test/track/x?a8mat=ABC123"
    changed = _entry(**kw)
    assert changed.projection_entry_hash != base.projection_entry_hash


# ==================== snapshot ==================================
def test_snapshot_is_ordering_independent() -> None:
    e1 = _entry(token="AAAA000000000000000000")
    e2 = _entry(token="ZZZZ999999999999999999")
    s1 = build_projection_snapshot([e1, e2])
    s2 = build_projection_snapshot([e2, e1])
    assert s1.projection_snapshot_hash == s2.projection_snapshot_hash
    assert [t.token for t in s1.targets] == [t.token for t in s2.targets]
    assert s1.targets[0].token.startswith("AAAA")


def test_empty_snapshot_is_deterministic() -> None:
    a = build_projection_snapshot([])
    b = build_projection_snapshot([])
    assert a.targets == ()
    assert a.projection_snapshot_hash == b.projection_snapshot_hash
    assert len(a.projection_snapshot_hash) == 64


def test_snapshot_rejects_duplicate_token() -> None:
    with pytest.raises(AffiliateProjectionError, match="duplicate token"):
        build_projection_snapshot([_entry(token="DUP0000000000000000000")] * 2)


def test_batch_request_body_has_no_nondeterministic_fields() -> None:
    snap = build_projection_snapshot([_entry()])
    body = build_projection_batch_request(snap)
    assert body["schema_version"] == 1
    assert body["projection_snapshot_hash"] == snap.projection_snapshot_hash
    assert len(body["targets"]) == 1
    for banned in ("generated_at", "nonce", "machine", "request_timestamp", "host"):
        assert banned not in body


def test_build_snapshot_from_targets_excludes_ineligible_without_deleting() -> None:
    ok = _target(token="OKAY000000000000000000")
    unicode_host = _target(
        token="UNIC000000000000000000",
        dest="https://日本語.example/x",
        host="xn--wgv71a119e.example",
    )
    snap, ineligible = build_snapshot_from_targets([ok, unicode_host])
    assert [t.token for t in snap.targets] == ["OKAY000000000000000000"]
    assert ineligible == ["UNIC000000000000000000"]
    # 元 target の destination_url は punycode へ書き換えられていない
    assert unicode_host.destination_url == "https://日本語.example/x"


# ==================== cross-runtime hostname contract =============
def test_ascii_host_exact_match_accepted() -> None:
    p = _entry(
        destination_url="https://aff.example.test/x",
        destination_host="aff.example.test",
    )
    assert p.destination_host == "aff.example.test"
    assert p.status == PROJECTION_STATUS_ACTIVE


def test_host_case_normalization_accepted() -> None:
    p = build_projection_entry(
        token="CASE000000000000000000",
        destination_url="https://AFF.Example.Test/x?a=1",
        destination_host="aff.example.test",
        link_identity_hash=_LH,
        alt_status="active",
        activated_at=_C,
        disabled_at=None,
    )
    assert p.destination_host == "aff.example.test"
    assert p.destination_url == "https://AFF.Example.Test/x?a=1"  # URL は無改変


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://evil.aff.example.test/x", "aff.example.test"),  # subdomain
        ("https://aff.example.test.evil.test/x", "aff.example.test"),  # suffix
        ("https://xaff.example.test/x", "aff.example.test"),  # prefix
        ("https://aff.example.test/x", "aff.example.tes"),  # stored truncated
    ],
)
def test_runtime_host_mismatch_rejected(url: str, host: str) -> None:
    with pytest.raises(AffiliateProjectionError):
        build_projection_entry(
            token="MISM000000000000000000",
            destination_url=url,
            destination_host=host,
            link_identity_hash=_LH,
            alt_status="active",
            activated_at=_C,
            disabled_at=None,
        )


def test_unicode_host_url_fails_closed() -> None:
    with pytest.raises(AffiliateProjectionError, match="ASCII"):
        build_projection_entry(
            token="UNIC000000000000000000",
            destination_url="https://日本語.example/x",
            destination_host="xn--wgv71a119e.example",
            link_identity_hash=_LH,
            alt_status="active",
            activated_at=_C,
            disabled_at=None,
        )


def test_https_requirement_retained_in_projection_layer() -> None:
    with pytest.raises(AffiliateProjectionError, match="https"):
        build_projection_entry(
            token="HTTP000000000000000000",
            destination_url="http://aff.example.test/x",
            destination_host="aff.example.test",
            link_identity_hash=_LH,
            alt_status="active",
            activated_at=_C,
            disabled_at=None,
        )


# ==================== upsert semantics ===========================
def _act(**kw):
    return _entry(token="UPS0000000000000000000", **kw)


def _dis(**kw):
    return _entry(
        token="UPS0000000000000000000", alt_status="disabled", disabled_at=_D, **kw
    )


def test_upsert_insert_when_no_current() -> None:
    assert decide_projection_upsert(current=None, incoming=_act()).result == UPSERT_INSERT


def test_upsert_initial_disabled_version_2_accepted() -> None:
    d = decide_projection_upsert(current=None, incoming=_dis())
    assert d.result == UPSERT_INSERT and d.ok


def test_upsert_same_version_same_hash_is_noop() -> None:
    cur = _act()
    assert decide_projection_upsert(current=cur, incoming=_act()).result == UPSERT_NOOP


def test_upsert_same_version_different_hash_is_conflict() -> None:
    cur = _act()
    # version 同一・immutable identity 同一だが entry hash が違う (activated_at 差) -> conflict。
    other = _entry(
        token="UPS0000000000000000000", activated_at=datetime(2019, 1, 1, tzinfo=UTC)
    )
    assert (
        decide_projection_upsert(current=cur, incoming=other).result
        == REJECT_HASH_CONFLICT
    )


def test_upsert_lower_version_rejected() -> None:
    cur = _dis()
    assert (
        decide_projection_upsert(current=cur, incoming=_act()).result
        in (REJECT_STALE_VERSION, REJECT_FORBIDDEN_REACTIVATION)
    )


def test_upsert_1_to_2_accepted() -> None:
    assert (
        decide_projection_upsert(current=_act(), incoming=_dis()).result
        == UPSERT_UPDATE
    )


def test_upsert_2_to_active_is_forbidden_reactivation() -> None:
    assert (
        decide_projection_upsert(current=_dis(), incoming=_act()).result
        == REJECT_FORBIDDEN_REACTIVATION
    )


def test_upsert_immutable_identity_drift_rejected() -> None:
    cur = _act()
    drifted = _entry(
        token="UPS0000000000000000000",
        destination_url="https://aff.example.test/DIFFERENT?a=1",
    )
    assert (
        decide_projection_upsert(current=cur, incoming=drifted).result
        == REJECT_IMMUTABLE_DRIFT
    )
    assert "destination_url" in IMMUTABLE_ENTRY_FIELDS


# ==================== batch atomicity ===========================
def test_batch_atomic_rejected_when_any_entry_conflicts() -> None:
    good = _entry(token="G000000000000000000000")
    cur_for_bad = _entry(token="B000000000000000000000")
    bad_incoming = _entry(
        token="B000000000000000000000",
        activated_at=datetime(2018, 1, 1, tzinfo=UTC),  # same version, different hash
    )
    ev = evaluate_projection_batch(
        current_by_token={"B000000000000000000000": cur_for_bad},
        incoming=[good, bad_incoming],
    )
    assert ev.received_count == 2
    assert ev.rejected_count == 1
    assert ev.atomic_accepted is False


def test_batch_all_ok_is_accepted() -> None:
    ev = evaluate_projection_batch(
        current_by_token={},
        incoming=[
            _entry(token="A000000000000000000000"),
            _entry(token="C000000000000000000000"),
        ],
    )
    assert ev.atomic_accepted is True
    assert ev.inserted_count == 2 and ev.rejected_count == 0


def test_batch_missing_token_is_untouched_not_deleted() -> None:
    # incoming に無い token は decisions に現れない (= 触らない)。
    ev = evaluate_projection_batch(
        current_by_token={
            "KEEP00000000000000000": _entry(token="KEEP00000000000000000")
        },
        incoming=[_entry(token="NEW000000000000000000")],
    )
    assert [tok for tok, _ in ev.decisions] == ["NEW000000000000000000"]
    assert ev.atomic_accepted is True
