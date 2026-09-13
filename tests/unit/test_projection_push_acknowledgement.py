"""app/affiliate/projection_push_acknowledgement.py — manifest / classification /
resolver / eligibility (pure, no DB, no network)。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.affiliate.projection import build_projection_entry, build_projection_snapshot
from app.affiliate.projection_push_acknowledgement import (
    DEFINITIVE_FAILED_SERVER_CODES,
    AcknowledgementResolution,
    build_request_manifest,
    is_definitive_failure_code,
    is_target_eligible,
    parse_manifest,
    resolve_latest_acknowledgement,
    serialize_manifest,
    token_fingerprint,
)
from app.models.affiliate_target_projection_push_run import (
    ATPP_FAILED,
    ATPP_OUTCOME_UNKNOWN,
    ATPP_RUNNING,
    ATPP_SUCCEEDED,
)


def _entry(token: str, *, status="active", disabled=None, activated=None):
    from datetime import UTC, datetime

    activated = activated or datetime(2026, 9, 1, tzinfo=UTC)
    return build_projection_entry(
        token=token,
        destination_url="https://aff.example.test/x",
        destination_host="aff.example.test",
        link_identity_hash="a" * 64,
        alt_status=status,
        activated_at=activated,
        disabled_at=disabled,
    )


def _orm(*, token, target_id):
    return SimpleNamespace(token=token, id=target_id)


# ==================== manifest =========================================
def test_manifest_exact_field_set() -> None:
    entry = _entry("AAAA0000tokenone00000")
    snap = build_projection_snapshot([entry])
    manifest = build_request_manifest(
        orm_targets=[_orm(token=entry.token, target_id=5)],
        snapshot_targets=snap.targets,
    )
    assert len(manifest) == 1
    assert set(manifest[0]) == {
        "affiliate_link_target_id", "token_fingerprint", "link_identity_hash",
        "status", "projection_version", "entry_hash",
    }


def test_manifest_canonical_ordering_by_target_id() -> None:
    e1 = _entry("AAAA0000tokenone00000")
    e2 = _entry("BBBB0000tokentwo00000")
    snap = build_projection_snapshot([e1, e2])
    manifest = build_request_manifest(
        orm_targets=[_orm(token=e1.token, target_id=9), _orm(token=e2.token, target_id=2)],
        snapshot_targets=snap.targets,
    )
    assert [m["affiliate_link_target_id"] for m in manifest] == [2, 9]


def test_manifest_serialization_is_deterministic() -> None:
    e1 = _entry("AAAA0000tokenone00000")
    snap = build_projection_snapshot([e1])
    manifest = build_request_manifest(
        orm_targets=[_orm(token=e1.token, target_id=1)], snapshot_targets=snap.targets
    )
    a = serialize_manifest(manifest)
    b = serialize_manifest(list(reversed(manifest)) if len(manifest) > 1 else manifest)
    assert a == b
    assert parse_manifest(a) == manifest


def test_manifest_rejects_duplicate_target_id() -> None:
    e1 = _entry("AAAA0000tokenone00000")
    e2 = _entry("BBBB0000tokentwo00000")
    snap = build_projection_snapshot([e1, e2])
    with pytest.raises(ValueError, match="duplicate"):
        build_request_manifest(
            orm_targets=[_orm(token=e1.token, target_id=1), _orm(token=e2.token, target_id=1)],
            snapshot_targets=snap.targets,
        )


def test_manifest_missing_orm_target_fails_closed() -> None:
    e1 = _entry("AAAA0000tokenone00000")
    snap = build_projection_snapshot([e1])
    with pytest.raises(ValueError, match="not found"):
        build_request_manifest(orm_targets=[], snapshot_targets=snap.targets)


def test_manifest_never_contains_full_token_or_destination_url() -> None:
    token = "AAAA0000tokenone00000"
    entry = _entry(token)
    snap = build_projection_snapshot([entry])
    manifest = build_request_manifest(
        orm_targets=[_orm(token=token, target_id=1)], snapshot_targets=snap.targets
    )
    serialized = serialize_manifest(manifest)
    assert token not in serialized
    assert "destination_url" not in serialized
    assert "aff.example.test" not in serialized
    assert manifest[0]["token_fingerprint"] == token_fingerprint(token)


# ==================== definitive failure classification ==================
@pytest.mark.parametrize(
    "code",
    [
        "secret_not_configured", "bad_timestamp", "content_sha256_mismatch",
        "timestamp_outside_window", "signature_mismatch",
        "invalid_json", "bad_schema_version", "bad_targets",
        "bad_entry", "bad_token", "bad_status", "bad_version_for_status",
        "bad_link_identity_hash", "bad_projection_entry_hash",
        "bad_destination_url", "bad_destination_host",
        "bad_activated_at", "bad_disabled_at",
        "entry_hash_mismatch", "destination_validation_failed",
        "snapshot_hash_mismatch", "conflict_hash_mismatch",
        "conflict_stale_version", "forbidden_reactivation",
        "conflict_immutable_identity_drift", "persist_failed",
    ],
)
def test_known_pre_transaction_and_persist_failed_codes_are_definitive(code) -> None:
    assert is_definitive_failure_code(code) is True


@pytest.mark.parametrize("code", ["body_hash_mismatch", "bad_signature"])
def test_unreachable_codes_are_excluded_from_definitive_set(code) -> None:
    # Human review ruling (D-D1A §2): these are allowlisted client-side but have
    # no source-traced emission path in the current PHP handler.
    assert is_definitive_failure_code(code) is False
    assert code not in DEFINITIVE_FAILED_SERVER_CODES


def test_unknown_code_is_not_definitive() -> None:
    assert is_definitive_failure_code("totally_unrecognized_code") is False


def test_none_code_is_not_definitive() -> None:
    assert is_definitive_failure_code(None) is False


# ==================== acknowledgement resolver ============================
def _manifest_entry(target_id: int, **over):
    base = {
        "affiliate_link_target_id": target_id,
        "token_fingerprint": "fp",
        "link_identity_hash": "lh",
        "status": "active",
        "projection_version": 1,
        "entry_hash": "eh",
    }
    base.update(over)
    return base


def _run(*, status, manifest=None, run_id=1):
    return SimpleNamespace(
        status=status,
        id=run_id,
        request_manifest_json=serialize_manifest(manifest or []),
    )


def test_resolver_latest_succeeded_wins() -> None:
    manifest = [{"affiliate_link_target_id": 1, "token_fingerprint": "f", "link_identity_hash": "h",
                 "status": "active", "projection_version": 1, "entry_hash": "e"}]
    runs = [_run(status=ATPP_SUCCEEDED, manifest=manifest, run_id=5)]
    res = resolve_latest_acknowledgement(runs)
    assert res.acknowledged
    assert res.source_run_id == 5
    assert res.manifest == manifest


def test_resolver_latest_running_fails_closed() -> None:
    runs = [_run(status=ATPP_RUNNING, run_id=2), _run(status=ATPP_SUCCEEDED, run_id=1)]
    res = resolve_latest_acknowledgement(runs)
    assert not res.acknowledged
    assert res.manifest is None


def test_resolver_latest_outcome_unknown_fails_closed() -> None:
    runs = [_run(status=ATPP_OUTCOME_UNKNOWN, run_id=2), _run(status=ATPP_SUCCEEDED, run_id=1)]
    res = resolve_latest_acknowledgement(runs)
    assert not res.acknowledged


def test_resolver_failed_walks_back_to_prior_succeeded() -> None:
    old_manifest = [_manifest_entry(1)]
    runs = [
        _run(status=ATPP_FAILED, run_id=3),
        _run(status=ATPP_SUCCEEDED, manifest=old_manifest, run_id=2),
    ]
    res = resolve_latest_acknowledgement(runs)
    assert res.acknowledged
    assert res.source_run_id == 2
    assert res.manifest == old_manifest


def test_resolver_walks_through_multiple_consecutive_failed_rows() -> None:
    manifest = [{"affiliate_link_target_id": 1, "token_fingerprint": "f", "link_identity_hash": "h",
                 "status": "active", "projection_version": 1, "entry_hash": "e"}]
    runs = [
        _run(status=ATPP_FAILED, run_id=4),
        _run(status=ATPP_FAILED, run_id=3),
        _run(status=ATPP_SUCCEEDED, manifest=manifest, run_id=2),
    ]
    res = resolve_latest_acknowledgement(runs)
    assert res.source_run_id == 2


def test_resolver_failed_then_older_running_fails_closed() -> None:
    runs = [
        _run(status=ATPP_FAILED, run_id=3),
        _run(status=ATPP_RUNNING, run_id=2),
        _run(status=ATPP_SUCCEEDED, run_id=1),
    ]
    res = resolve_latest_acknowledgement(runs)
    assert not res.acknowledged


def test_resolver_failed_then_older_outcome_unknown_fails_closed() -> None:
    runs = [
        _run(status=ATPP_FAILED, run_id=3),
        _run(status=ATPP_OUTCOME_UNKNOWN, run_id=2),
        _run(status=ATPP_SUCCEEDED, run_id=1),
    ]
    res = resolve_latest_acknowledgement(runs)
    assert not res.acknowledged


def test_resolver_no_succeeded_anywhere_fails_closed() -> None:
    runs = [_run(status=ATPP_FAILED, run_id=2), _run(status=ATPP_FAILED, run_id=1)]
    res = resolve_latest_acknowledgement(runs)
    assert not res.acknowledged


def test_resolver_empty_history_fails_closed() -> None:
    res = resolve_latest_acknowledgement([])
    assert not res.acknowledged


# ==================== per-target eligibility ===============================
def _resolution_with(manifest_entries):
    return AcknowledgementResolution(manifest=manifest_entries, source_run_id=1)


def test_eligible_exact_active_v1_match() -> None:
    res = _resolution_with(
        [{"affiliate_link_target_id": 7, "token_fingerprint": "fp", "link_identity_hash": "lh",
          "status": "active", "projection_version": 1, "entry_hash": "eh"}]
    )
    assert is_target_eligible(
        res, affiliate_link_target_id=7, expected_token_fingerprint="fp",
        expected_link_identity_hash="lh", expected_status="active",
        expected_projection_version=1, expected_entry_hash="eh",
    )


def test_ineligible_when_disabled_v2() -> None:
    res = _resolution_with(
        [{"affiliate_link_target_id": 7, "token_fingerprint": "fp", "link_identity_hash": "lh",
          "status": "disabled", "projection_version": 2, "entry_hash": "eh"}]
    )
    assert not is_target_eligible(
        res, affiliate_link_target_id=7, expected_token_fingerprint="fp",
        expected_link_identity_hash="lh", expected_status="active",
        expected_projection_version=1, expected_entry_hash="eh",
    )


def test_ineligible_on_fingerprint_mismatch() -> None:
    res = _resolution_with([_manifest_entry(7, token_fingerprint="fp-wrong")])
    assert not is_target_eligible(
        res, affiliate_link_target_id=7, expected_token_fingerprint="fp",
        expected_link_identity_hash="lh", expected_status="active",
        expected_projection_version=1, expected_entry_hash="eh",
    )


def test_ineligible_on_link_identity_hash_mismatch() -> None:
    res = _resolution_with([_manifest_entry(7, link_identity_hash="lh-wrong")])
    assert not is_target_eligible(
        res, affiliate_link_target_id=7, expected_token_fingerprint="fp",
        expected_link_identity_hash="lh", expected_status="active",
        expected_projection_version=1, expected_entry_hash="eh",
    )


def test_ineligible_on_entry_hash_mismatch() -> None:
    res = _resolution_with(
        [{"affiliate_link_target_id": 7, "token_fingerprint": "fp", "link_identity_hash": "lh",
          "status": "active", "projection_version": 1, "entry_hash": "eh-wrong"}]
    )
    assert not is_target_eligible(
        res, affiliate_link_target_id=7, expected_token_fingerprint="fp",
        expected_link_identity_hash="lh", expected_status="active",
        expected_projection_version=1, expected_entry_hash="eh",
    )


def test_ineligible_when_target_omitted_from_manifest() -> None:
    res = _resolution_with([])
    assert not is_target_eligible(
        res, affiliate_link_target_id=7, expected_token_fingerprint="fp",
        expected_link_identity_hash="lh", expected_status="active",
        expected_projection_version=1, expected_entry_hash="eh",
    )


def test_ineligible_when_no_acknowledgement_at_all() -> None:
    res = AcknowledgementResolution(manifest=None, source_run_id=None)
    assert not is_target_eligible(
        res, affiliate_link_target_id=7, expected_token_fingerprint="fp",
        expected_link_identity_hash="lh", expected_status="active",
        expected_projection_version=1, expected_entry_hash="eh",
    )
