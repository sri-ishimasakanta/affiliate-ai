"""evaluate_affiliate_publication_readiness (D-E2) — pure unit tests.

DB read 0・network 0。``ArtifactInspection``/``ManifestEntrySummary``/
``CurrentOccurrenceEvidence`` を直接構築し、各理由コードが正しく検出される
ことを個別に証明する (D-E2 §26: 一部の integrity 分岐は
``WordPressContentUpdatePreflightService`` の既存 local gate が classify() の
時点で先に fail closed にするため、full integration では到達できない --
ここではそれらも含めて exhaustive に検証する)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.services.affiliate_publication_readiness import (
    REASON_ARTIFACT_APPROVAL_HASH_MISMATCH,
    REASON_ARTIFACT_HASH_INVALID,
    REASON_ARTIFACT_NOT_APPROVED,
    REASON_CANONICAL_MISMATCH,
    REASON_CURRENT_EVIDENCE_UNRESOLVABLE,
    REASON_HOST_NOT_APPROVED,
    REASON_MANIFEST_INVALID,
    REASON_MAPPING_NOT_ACTIVE,
    REASON_PROGRAM_NOT_ACTIVE,
    REASON_PROJECTION_NOT_ELIGIBLE,
    REASON_PROJECTION_VERSION_MISMATCH,
    REASON_STRICT_HTML_INVALID,
    REASON_TARGET_NOT_ACTIVE,
    REASON_TRACKED_HTML_HASH_INVALID,
    evaluate_affiliate_publication_readiness,
)
from app.services.article_publication_artifact_inspection_service import (
    CURRENT_CANONICAL_ARTICLE_NOT_FOUND,
    CURRENT_CANONICAL_MATCH,
    ArtifactInspection,
    CurrentOccurrenceEvidence,
    ManifestEntrySummary,
)

_NOW = datetime.now(UTC)


def _resolvable_current(**overrides) -> CurrentOccurrenceEvidence:
    base = dict(
        resolvable=True,
        fail_reason=None,
        mapping_status="active",
        target_status="active",
        program_name="Test ASP",
        program_provider="test-asp",
        program_status="active",
        destination_host="aff.example.test",
        current_projection_version=1,
        projection_eligible=True,
        host_policy_eligible=True,
    )
    base.update(overrides)
    return CurrentOccurrenceEvidence(**base)


def _unresolvable_current(reason: str = "mapping_missing") -> CurrentOccurrenceEvidence:
    return CurrentOccurrenceEvidence(
        resolvable=False,
        fail_reason=reason,
        mapping_status=None,
        target_status=None,
        program_name=None,
        program_provider=None,
        program_status=None,
        destination_host=None,
        current_projection_version=None,
        projection_eligible=None,
        host_policy_eligible=None,
    )


def _entry(*, ordinal: int = 0, current: CurrentOccurrenceEvidence, projection_version: int = 1):
    return ManifestEntrySummary(
        occurrence_ordinal=ordinal,
        occurrence_identity_hash="a" * 64,
        mapping_id=1,
        affiliate_link_target_id=1,
        target_projection_version=projection_version,
        original_href="https://official.example.test/x",
        original_host="official.example.test",
        replacement_href_masked="https://bizfluxlab.com/go/<masked>",
        token_fingerprint="f" * 64,
        rel_before="",
        rel_after="sponsored nofollow noopener noreferrer",
        is_affiliate_substitution=True,
        current=current,
    )


def _inspection(*, manifest_summary=(), all_current_evidence_resolvable=True, **overrides):
    base = dict(
        artifact_id=1,
        article_id=1,
        article_title="t",
        canonical_body_hash="b" * 64,
        renderer_version="wordpress_html_v1",
        artifact_schema_version=1,
        substitution_count=len(manifest_summary),
        artifact_hash="c" * 64,
        tracked_html_hash="d" * 64,
        approved=True,
        approved_at=_NOW,
        approved_artifact_hash="c" * 64,
        generated_at=_NOW,
        created_at=_NOW,
        manifest_summary=list(manifest_summary),
        artifact_hash_valid=True,
        tracked_html_hash_valid=True,
        manifest_valid=True,
        strict_html_validation_valid=True,
        current_canonical_status=CURRENT_CANONICAL_MATCH,
        all_current_evidence_resolvable=all_current_evidence_resolvable,
    )
    base.update(overrides)
    return ArtifactInspection(**base)


# ==================== fully-ready baseline ======================================
def test_fully_ready_inspection_is_ready_with_no_failures() -> None:
    entry = _entry(current=_resolvable_current())
    insp = _inspection(manifest_summary=[entry])

    result = evaluate_affiliate_publication_readiness(insp)

    assert result.ready is True
    assert result.failure_reasons == ()


# ==================== artifact-level gates (§5) ==================================
def test_not_approved_yields_artifact_not_approved() -> None:
    entry = _entry(current=_resolvable_current())
    insp = _inspection(manifest_summary=[entry], approved=False, approved_artifact_hash=None)

    result = evaluate_affiliate_publication_readiness(insp)

    assert result.ready is False
    assert REASON_ARTIFACT_NOT_APPROVED in result.failure_reasons
    # 未承認のときは approval-hash-mismatch を二重に出さない (承認自体が無い)。
    assert REASON_ARTIFACT_APPROVAL_HASH_MISMATCH not in result.failure_reasons


def test_approval_hash_mismatch_yields_reason() -> None:
    entry = _entry(current=_resolvable_current())
    insp = _inspection(manifest_summary=[entry], approved=True, approved_artifact_hash="f" * 64)

    result = evaluate_affiliate_publication_readiness(insp)

    assert result.ready is False
    assert REASON_ARTIFACT_APPROVAL_HASH_MISMATCH in result.failure_reasons


def test_artifact_hash_invalid_yields_reason() -> None:
    entry = _entry(current=_resolvable_current())
    insp = _inspection(manifest_summary=[entry], artifact_hash_valid=False)

    result = evaluate_affiliate_publication_readiness(insp)

    assert result.ready is False
    assert REASON_ARTIFACT_HASH_INVALID in result.failure_reasons


def test_tracked_html_hash_invalid_yields_reason() -> None:
    entry = _entry(current=_resolvable_current())
    insp = _inspection(manifest_summary=[entry], tracked_html_hash_valid=False)

    result = evaluate_affiliate_publication_readiness(insp)

    assert result.ready is False
    assert REASON_TRACKED_HTML_HASH_INVALID in result.failure_reasons


def test_manifest_invalid_yields_reason_and_no_occurrence_evaluation() -> None:
    # manifest_valid=False の実際の inspection は manifest_summary=[] になる
    # (parse_manifest が失敗すれば summary を作れないため) -- その実挙動を再現。
    insp = _inspection(manifest_summary=[], manifest_valid=False, substitution_count=1)

    result = evaluate_affiliate_publication_readiness(insp)

    assert result.ready is False
    assert REASON_MANIFEST_INVALID in result.failure_reasons
    # manifest_summary が空なので all_current_evidence_resolvable は
    # vacuously True のまま (呼び出し側の inspection がそう作る) -- readiness
    # 全体としては artifact-level 理由だけで既に not ready。
    assert REASON_CURRENT_EVIDENCE_UNRESOLVABLE not in result.failure_reasons


def test_strict_html_invalid_yields_reason() -> None:
    entry = _entry(current=_resolvable_current())
    insp = _inspection(manifest_summary=[entry], strict_html_validation_valid=False)

    result = evaluate_affiliate_publication_readiness(insp)

    assert result.ready is False
    assert REASON_STRICT_HTML_INVALID in result.failure_reasons


def test_canonical_mismatch_yields_reason() -> None:
    entry = _entry(current=_resolvable_current())
    insp = _inspection(
        manifest_summary=[entry], current_canonical_status=CURRENT_CANONICAL_ARTICLE_NOT_FOUND
    )

    result = evaluate_affiliate_publication_readiness(insp)

    assert result.ready is False
    assert REASON_CANONICAL_MISMATCH in result.failure_reasons


# ==================== current-evidence resolvability (§6) =======================
def test_unresolvable_current_evidence_yields_reason_and_skips_per_occurrence_checks() -> None:
    entry = _entry(current=_unresolvable_current("target_missing"))
    insp = _inspection(manifest_summary=[entry], all_current_evidence_resolvable=False)

    result = evaluate_affiliate_publication_readiness(insp)

    assert result.ready is False
    assert result.failure_reasons == (REASON_CURRENT_EVIDENCE_UNRESOLVABLE,)
    # unresolvable のときは mapping/target/program/host/projection の個別
    # チェックへ進まない (current.* は全て None なので評価しようがない)。
    assert REASON_MAPPING_NOT_ACTIVE not in result.failure_reasons
    assert REASON_TARGET_NOT_ACTIVE not in result.failure_reasons
    assert REASON_PROGRAM_NOT_ACTIVE not in result.failure_reasons
    assert REASON_HOST_NOT_APPROVED not in result.failure_reasons
    assert REASON_PROJECTION_NOT_ELIGIBLE not in result.failure_reasons


# ==================== per-occurrence operational gates (§7/§8/§9/§10/§11) =======
def test_mapping_revoked_yields_mapping_not_active() -> None:
    entry = _entry(current=_resolvable_current(mapping_status="revoked"))
    insp = _inspection(manifest_summary=[entry])

    result = evaluate_affiliate_publication_readiness(insp)

    assert result.ready is False
    assert REASON_MAPPING_NOT_ACTIVE in result.failure_reasons


def test_mapping_superseded_yields_mapping_not_active() -> None:
    entry = _entry(current=_resolvable_current(mapping_status="superseded"))
    insp = _inspection(manifest_summary=[entry])

    result = evaluate_affiliate_publication_readiness(insp)

    assert REASON_MAPPING_NOT_ACTIVE in result.failure_reasons


def test_target_disabled_yields_target_not_active() -> None:
    entry = _entry(current=_resolvable_current(target_status="disabled"))
    insp = _inspection(manifest_summary=[entry])

    result = evaluate_affiliate_publication_readiness(insp)

    assert REASON_TARGET_NOT_ACTIVE in result.failure_reasons


def test_target_superseded_yields_target_not_active() -> None:
    entry = _entry(current=_resolvable_current(target_status="superseded"))
    insp = _inspection(manifest_summary=[entry])

    result = evaluate_affiliate_publication_readiness(insp)

    assert REASON_TARGET_NOT_ACTIVE in result.failure_reasons


def test_program_paused_yields_program_not_active() -> None:
    entry = _entry(current=_resolvable_current(program_status="paused"))
    insp = _inspection(manifest_summary=[entry])

    result = evaluate_affiliate_publication_readiness(insp)

    assert REASON_PROGRAM_NOT_ACTIVE in result.failure_reasons


def test_program_ended_yields_program_not_active() -> None:
    entry = _entry(current=_resolvable_current(program_status="ended"))
    insp = _inspection(manifest_summary=[entry])

    result = evaluate_affiliate_publication_readiness(insp)

    assert REASON_PROGRAM_NOT_ACTIVE in result.failure_reasons


def test_program_unknown_yields_program_not_active() -> None:
    entry = _entry(current=_resolvable_current(program_status="unknown"))
    insp = _inspection(manifest_summary=[entry])

    result = evaluate_affiliate_publication_readiness(insp)

    assert REASON_PROGRAM_NOT_ACTIVE in result.failure_reasons


def test_host_not_approved_yields_reason() -> None:
    entry = _entry(current=_resolvable_current(host_policy_eligible=False))
    insp = _inspection(manifest_summary=[entry])

    result = evaluate_affiliate_publication_readiness(insp)

    assert REASON_HOST_NOT_APPROVED in result.failure_reasons


def test_projection_not_eligible_yields_reason() -> None:
    entry = _entry(current=_resolvable_current(projection_eligible=False))
    insp = _inspection(manifest_summary=[entry])

    result = evaluate_affiliate_publication_readiness(insp)

    assert REASON_PROJECTION_NOT_ELIGIBLE in result.failure_reasons


# ==================== frozen vs current projection version (§12) ================
def test_projection_version_match_is_not_a_failure() -> None:
    entry = _entry(current=_resolvable_current(current_projection_version=1), projection_version=1)
    insp = _inspection(manifest_summary=[entry])

    result = evaluate_affiliate_publication_readiness(insp)

    assert REASON_PROJECTION_VERSION_MISMATCH not in result.failure_reasons


def test_projection_version_mismatch_yields_reason_even_if_otherwise_ready() -> None:
    # target は「現在 active (version 1)」だが frozen manifest は version 2 の
    # まま -- is_target_eligible() 自体は別途 False になり得るが、ここでは
    # defense-in-depth の比較そのものを孤立させて検証する: projection_eligible
    # は True (別の経路のダブルチェックとして) でも、version 不一致だけで
    # readiness は fail closed になること。
    entry = _entry(
        current=_resolvable_current(current_projection_version=1, projection_eligible=True),
        projection_version=2,
    )
    insp = _inspection(manifest_summary=[entry])

    result = evaluate_affiliate_publication_readiness(insp)

    assert result.ready is False
    assert REASON_PROJECTION_VERSION_MISMATCH in result.failure_reasons


# ==================== multiple occurrences / dedup (§14/§16) ====================
def test_multiple_occurrences_all_evaluated_independently() -> None:
    good = _entry(ordinal=0, current=_resolvable_current())
    bad = _entry(ordinal=1, current=_resolvable_current(target_status="disabled"))
    insp = _inspection(manifest_summary=[good, bad])

    result = evaluate_affiliate_publication_readiness(insp)

    assert result.ready is False
    assert REASON_TARGET_NOT_ACTIVE in result.failure_reasons


def test_same_reason_across_occurrences_is_deduplicated() -> None:
    e0 = _entry(ordinal=0, current=_resolvable_current(target_status="disabled"))
    e1 = _entry(ordinal=1, current=_resolvable_current(target_status="disabled"))
    insp = _inspection(manifest_summary=[e0, e1])

    result = evaluate_affiliate_publication_readiness(insp)

    assert result.failure_reasons.count(REASON_TARGET_NOT_ACTIVE) == 1


def test_fail_all_contract_collects_multiple_distinct_reasons() -> None:
    """D-E2 §16: fail-all 契約 -- 複数の独立した failure を同時に返す
    (最初の 1 つで打ち切らない)。"""

    entry = _entry(
        current=_resolvable_current(
            mapping_status="revoked", target_status="disabled", host_policy_eligible=False
        )
    )
    insp = _inspection(manifest_summary=[entry], tracked_html_hash_valid=False)

    result = evaluate_affiliate_publication_readiness(insp)

    assert result.ready is False
    assert REASON_TRACKED_HTML_HASH_INVALID in result.failure_reasons
    assert REASON_MAPPING_NOT_ACTIVE in result.failure_reasons
    assert REASON_TARGET_NOT_ACTIVE in result.failure_reasons
    assert REASON_HOST_NOT_APPROVED in result.failure_reasons


# ==================== safe output (§25 analog) ===================================
def test_failure_reasons_never_contain_secrets_or_identifiers() -> None:
    entry = _entry(
        current=_resolvable_current(target_status="disabled", host_policy_eligible=False)
    )
    insp = _inspection(manifest_summary=[entry], approved=False)

    result = evaluate_affiliate_publication_readiness(insp)

    joined = " ".join(result.failure_reasons)
    assert "token" not in joined.lower()
    assert "aff.example.test" not in joined
    assert "https://" not in joined
    assert all(r.islower() and " " not in r for r in result.failure_reasons)
