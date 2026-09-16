"""affiliate publication readiness — D-E2 の mandatory pre-write gate (pure)。

``substitution_count > 0`` な :class:`~app.models.article_publication_artifact.
ArticlePublicationArtifact` を WordPress content-update の POST 境界まで進めて
よいかを、D-E1 の :class:`~app.services.article_publication_artifact_inspection_service.
ArticlePublicationArtifactInspectionService` が **既に fresh に読んだ**
:class:`~app.services.article_publication_artifact_inspection_service.ArtifactInspection`
だけから fail-closed で判定する。

DB read 0・network request 0・commit 0 -- ここでは mapping/target/program/
projection acknowledgement の識別ロジックを一切再実装しない。inspection が
既に解決した FROZEN evidence (``artifact_hash_valid`` 等) と CURRENT evidence
(``ManifestEntrySummary.current``) を再解釈するだけの、狭い集約レイヤー。

Human 承認 (``approved``/``approved_artifact_hash``) は **どの厳密な frozen
artifact が承認されたか** を証明するだけで、mapping/target/program の現在の
運用状態や projection acknowledgement の現在状態を永久に固定するものではない
(D-E0.3/D-E1 で確立した契約)。よってこの関数は承認済みかどうかに関わらず、
呼び出しの都度 fresh な CURRENT evidence を要求する -- 呼び出し側
(:class:`~app.services.wordpress_content_update_execution_service.
WordPressContentUpdateExecutionService`) が Transaction A の直前に毎回これを
呼ぶことで、承認後に状態が悪化していないことを保証する。

fail-all 契約 (D-E2 §16): 安全に判定できる readiness failure は全て収集して
返す (Human diagnosis を優先する)。token/destination_url/query parameter/
secret は理由コードに一切含めない -- 理由コードは固定の安全な文字列のみ。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.article_publication_artifact_inspection_service import (
    CURRENT_CANONICAL_MATCH,
    ArtifactInspection,
)

# -- fail-closed reason codes (D-E2 §15; CLI/logging に安全) -------------------
REASON_ARTIFACT_NOT_APPROVED = "artifact_not_approved"
REASON_ARTIFACT_APPROVAL_HASH_MISMATCH = "artifact_approval_hash_mismatch"
REASON_ARTIFACT_HASH_INVALID = "artifact_hash_invalid"
REASON_TRACKED_HTML_HASH_INVALID = "tracked_html_hash_invalid"
REASON_MANIFEST_INVALID = "manifest_invalid"
REASON_STRICT_HTML_INVALID = "strict_html_invalid"
REASON_CANONICAL_MISMATCH = "canonical_mismatch"
REASON_CURRENT_EVIDENCE_UNRESOLVABLE = "current_evidence_unresolvable"
REASON_MAPPING_NOT_ACTIVE = "mapping_not_active"
REASON_TARGET_NOT_ACTIVE = "target_not_active"
REASON_PROGRAM_NOT_ACTIVE = "program_not_active"
REASON_HOST_NOT_APPROVED = "host_not_approved"
REASON_PROJECTION_NOT_ELIGIBLE = "projection_not_eligible"
REASON_PROJECTION_VERSION_MISMATCH = "projection_version_mismatch"

_ACTIVE_MAPPING_STATUS = "active"
_ACTIVE_TARGET_STATUS = "active"
_ACTIVE_PROGRAM_STATUS = "active"


@dataclass(frozen=True)
class AffiliatePublicationReadinessResult:
    """安全な事実のみを含む immutable な readiness 判定結果。

    ``failure_reasons`` は重複を除いた、決定的な順序 (最初に検出された順) の
    固定理由コード列。空なら ``ready`` は必ず ``True``。"""

    ready: bool
    failure_reasons: tuple[str, ...]


def evaluate_affiliate_publication_readiness(
    inspection: ArtifactInspection,
) -> AffiliatePublicationReadinessResult:
    """``inspection`` (呼び出し側が同じ呼び出しの中で取得した fresh な
    ``inspect()`` 結果) だけから、DB read 0・network 0 で readiness を判定する。

    呼び出し側の責務: ``inspection.substitution_count == 0`` のときはそもそも
    この関数を呼ばないこと (D-E2 は affiliate substitution が無い artifact に
    は一切関与しない)。この関数自身はその前提を検証しない -- 呼ばれたら常に
    affiliate readiness を評価する。
    """

    reasons: list[str] = []

    # -- artifact-level: frozen 整合性 + Human 承認 evidence -----------------
    if not inspection.approved:
        reasons.append(REASON_ARTIFACT_NOT_APPROVED)
    elif inspection.approved_artifact_hash != inspection.artifact_hash:
        reasons.append(REASON_ARTIFACT_APPROVAL_HASH_MISMATCH)
    if not inspection.artifact_hash_valid:
        reasons.append(REASON_ARTIFACT_HASH_INVALID)
    if not inspection.tracked_html_hash_valid:
        reasons.append(REASON_TRACKED_HTML_HASH_INVALID)
    if not inspection.manifest_valid:
        reasons.append(REASON_MANIFEST_INVALID)
    if not inspection.strict_html_validation_valid:
        reasons.append(REASON_STRICT_HTML_INVALID)
    if inspection.current_canonical_status != CURRENT_CANONICAL_MATCH:
        reasons.append(REASON_CANONICAL_MISMATCH)

    # -- CURRENT evidence: identity resolvability は前提条件 -----------------
    # resolvable でない occurrence が 1 つでもあれば、その occurrence の
    # mapping_status/target_status/... は全て None (捏造されていない) ため、
    # 個々の運用状態チェックには進まない -- unresolvable 自体が fail-closed。
    if not inspection.all_current_evidence_resolvable:
        reasons.append(REASON_CURRENT_EVIDENCE_UNRESOLVABLE)
    else:
        for entry in inspection.manifest_summary:
            current = entry.current
            if current.mapping_status != _ACTIVE_MAPPING_STATUS:
                reasons.append(REASON_MAPPING_NOT_ACTIVE)
            if current.target_status != _ACTIVE_TARGET_STATUS:
                reasons.append(REASON_TARGET_NOT_ACTIVE)
            if current.program_status != _ACTIVE_PROGRAM_STATUS:
                reasons.append(REASON_PROGRAM_NOT_ACTIVE)
            if not current.host_policy_eligible:
                reasons.append(REASON_HOST_NOT_APPROVED)
            if not current.projection_eligible:
                reasons.append(REASON_PROJECTION_NOT_ELIGIBLE)
            # D-E2 §12: defense-in-depth -- is_target_eligible() は既に current
            # projection version を照合済みだが、frozen manifest の
            # target_projection_version との一致を明示的にも要求する。
            if entry.target_projection_version != current.current_projection_version:
                reasons.append(REASON_PROJECTION_VERSION_MISMATCH)

    deduped_reasons = tuple(dict.fromkeys(reasons))
    return AffiliatePublicationReadinessResult(
        ready=(len(deduped_reasons) == 0), failure_reasons=deduped_reasons
    )
