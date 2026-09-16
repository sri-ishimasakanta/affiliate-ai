"""ArticlePublicationArtifactInspectionService — D-D4 の READ-ONLY Human 向け
artifact 検査。

DB write 0。persisted な ``ArticlePublicationArtifact`` 行を **信用せず**、
凍結フィールドから独立に再計算・再検証してから安全な要約として返す:

- ``artifact_hash_valid`` — 保存済み manifest/canonical_body_hash/renderer_version/
  artifact_schema_version から ``compute_artifact_hash`` を再計算し、保存済み
  ``artifact_hash`` と一致するか。
- ``tracked_html_hash_valid`` — 保存済み ``tracked_html`` から
  ``compute_tracked_html_hash`` を再計算し、保存済み ``tracked_html_hash`` と
  一致するか。
- ``manifest_valid`` — 保存済み ``substitution_manifest_json`` が
  ``parse_manifest`` (D-D1 の形状検証) を通るか。
- ``strict_html_validation_valid`` — 保存済み ``tracked_html`` + manifest だけ
  から ``reverse_tracked_html`` が例外なく完了するか (tracked_html/manifest
  自体の内部整合性。**現在の** Article.body には一切依存しない — 純粋に凍結
  行だけの自己整合性チェック)。

これとは明確に分離して ``current_canonical_status`` を報告する:

- ``CURRENT_CANONICAL_MATCH`` — 現在の Article.body の hash が
  ``artifact.canonical_body_hash`` と一致する。
- ``CURRENT_CANONICAL_DRIFT`` — 一致しない (Article.body がこの artifact 生成後に
  変わった)。ただし drift は artifact 自体の破損を意味しない — 別の独立した
  最新 artifact を新たに準備できるかどうかの話でしかない。
- ``ARTICLE_NOT_FOUND`` — article が既に存在しない。

mapping/target/projection acknowledgement の **現在** の状態は一切参照しない —
historical inspection を current eligibility 判定にすり替えない (D-D4 §19)。

full token / destination_url / runtime secret は一切出力しない — manifest summary
は token fingerprint とマスクされた ``/go/<masked>`` 表示のみ。

D-E1: 上記の historical independence (frozen artifact 自体の整合性検証) はそのまま
維持しつつ、Human Preview のために各 occurrence へ **別枠の** CURRENT 証跡
(:class:`CurrentOccurrenceEvidence`) を追加する。これは manifest の frozen
``mapping_id`` / ``affiliate_link_target_id`` / ``token`` を現在の
``ArticleLinkSubstitutionMapping`` / ``AffiliateLinkTarget`` / ``AffiliateProgram``
行へ突き合わせて読むだけ (DB write 0、WordPress request 0) — FROZEN artifact
integrity の判定ロジックには一切混ぜない。mapping/target の識別子が現在の行と
一致しない場合は CURRENT 証跡を **捏造せず** fail closed (``resolvable=False``)
で返す。projection eligibility / host-policy eligibility の判定は既存の pure
helper (:func:`app.affiliate.projection_push_acknowledgement.is_target_eligible`
等、:mod:`app.services.article_link_occurrence_preview_service` が使うのと同じ
もの) をそのまま再利用し、判定ロジック自体を再実装しない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.affiliate.destination_policy import is_host_approved
from app.affiliate.projection import (
    PROJECTION_STATUS_ACTIVE,
    PROJECTION_VERSION_ACTIVE,
    AffiliateProjectionError,
    projection_from_target,
    runtime_status_and_version,
)
from app.affiliate.projection_push_acknowledgement import (
    is_target_eligible,
    resolve_latest_acknowledgement,
    token_fingerprint,
)
from app.affiliate.runtime_http import require_https_origin
from app.article.draft_promotion_canonical import compute_text_hash
from app.config.settings import Settings, get_settings
from app.exceptions import EntityNotFoundError
from app.repositories.affiliate_link_target_repository import (
    AffiliateLinkTargetRepository,
)
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.repositories.affiliate_target_projection_push_run_repository import (
    AffiliateTargetProjectionPushRunRepository,
)
from app.repositories.article_link_substitution_mapping_repository import (
    ArticleLinkSubstitutionMappingRepository,
)
from app.repositories.article_publication_artifact_repository import (
    ArticlePublicationArtifactRepository,
)
from app.repositories.article_repository import ArticleRepository
from app.wordpress.link_occurrence import extract_original_host
from app.wordpress.publication_artifact import (
    GO_BASE_URL,
    GO_PATH_PREFIX,
    PublicationArtifactError,
    compute_artifact_hash,
    compute_tracked_html_hash,
    parse_manifest,
)
from app.wordpress.publication_substitution import (
    PublicationSubstitutionError,
    reverse_tracked_html,
)

_MASKED_REPLACEMENT_HREF = f"{GO_BASE_URL}{GO_PATH_PREFIX}<masked>"

CURRENT_CANONICAL_MATCH = "CURRENT_CANONICAL_MATCH"
CURRENT_CANONICAL_DRIFT = "CURRENT_CANONICAL_DRIFT"
CURRENT_CANONICAL_ARTICLE_NOT_FOUND = "ARTICLE_NOT_FOUND"

# D-E1: CURRENT evidence が安全に構築できない (= fail closed) 理由コード。
FAIL_MAPPING_MISSING = "mapping_missing"
FAIL_MAPPING_IDENTITY_MISMATCH = "mapping_identity_mismatch"
FAIL_TARGET_MISSING = "target_missing"
FAIL_TARGET_IDENTITY_MISMATCH = "target_identity_mismatch"
FAIL_PROGRAM_MISSING = "program_missing"


@dataclass(frozen=True)
class CurrentOccurrenceEvidence:
    """CURRENT (frozen ではない) control-plane 証跡。manifest の frozen
    ``mapping_id`` / ``affiliate_link_target_id`` / ``token`` を今この瞬間の
    mapping/target/program 行へ突き合わせて読んだ結果 -- lifecycle で変化しうる。

    ``resolvable`` が ``False`` のとき、他のフィールドは全て ``None`` (捏造しない)。
    mapping/target の識別子が現在の行と一致しない、または行自体が存在しない場合に
    ``False`` になる -- D-E1 の Human Preview / 承認は resolvable でない
    occurrence を含む artifact を承認してはならない (fail closed)。

    ``resolvable`` が ``True`` でも、mapping/target/program の運用状態
    (revoked/disabled/paused 等) や projection/host-policy 適格性は独立に
    悪化しうる -- これらは CURRENT WARNING として表示するだけで、D-E1 では
    承認そのものをブロックしない (D-E2 が実行境界で改めて fresh に強制する)。
    """

    resolvable: bool
    fail_reason: str | None

    mapping_status: str | None
    target_status: str | None
    program_name: str | None
    program_provider: str | None
    program_status: str | None
    destination_host: str | None
    current_projection_version: int | None
    projection_eligible: bool | None
    host_policy_eligible: bool | None


@dataclass(frozen=True)
class ManifestEntrySummary:
    occurrence_ordinal: int
    occurrence_identity_hash: str
    mapping_id: int
    affiliate_link_target_id: int
    target_projection_version: int
    original_href: str
    original_host: str | None
    replacement_href_masked: str
    token_fingerprint: str
    rel_before: str
    rel_after: str
    # manifest にエントリが存在すること自体が affiliate substitution の
    # authoritative な証拠 (D-E0.2 §7) -- host / /go path / rel からの推測ではない。
    is_affiliate_substitution: bool
    current: CurrentOccurrenceEvidence


@dataclass(frozen=True)
class ArtifactInspection:
    artifact_id: int
    article_id: int
    article_title: str
    canonical_body_hash: str
    renderer_version: str
    artifact_schema_version: int
    substitution_count: int
    artifact_hash: str
    tracked_html_hash: str
    approved: bool
    approved_at: datetime | None
    approved_artifact_hash: str | None
    generated_at: datetime
    created_at: datetime

    manifest_summary: list[ManifestEntrySummary]

    artifact_hash_valid: bool
    tracked_html_hash_valid: bool
    manifest_valid: bool
    strict_html_validation_valid: bool

    current_canonical_status: str

    # D-E1: 全 occurrence の CurrentOccurrenceEvidence.resolvable が True か
    # (substitution_count == 0 なら vacuously True)。承認前の fail-closed gate。
    all_current_evidence_resolvable: bool


class ArticlePublicationArtifactInspectionService:
    """READ-ONLY。commit を一度も呼ばない。"""

    def __init__(self, session: Session, *, settings: Settings | None = None) -> None:
        self._session = session
        self._settings = settings if settings is not None else get_settings()
        self._articles = ArticleRepository(session)
        self._artifacts = ArticlePublicationArtifactRepository(session)
        self._mappings = ArticleLinkSubstitutionMappingRepository(session)
        self._targets = AffiliateLinkTargetRepository(session)
        self._programs = AffiliateProgramRepository(session)
        self._push_runs = AffiliateTargetProjectionPushRunRepository(session)

    def inspect(self, artifact_id: int) -> ArtifactInspection:
        artifact = self._artifacts.get_by_id(artifact_id)
        if artifact is None:
            raise EntityNotFoundError("ArticlePublicationArtifact", artifact_id)

        try:
            parsed_manifest = parse_manifest(artifact.substitution_manifest_json)
            manifest_valid = True
        except (PublicationArtifactError, ValueError):
            # ``parse_manifest`` は形状不正で ``PublicationArtifactError`` を
            # raise するが、JSON として構文的に壊れている場合は
            # ``json.loads`` 自身が ``json.JSONDecodeError`` (``ValueError`` の
            # 派生) を raise する -- 検査は両方とも "manifest_valid=False" として
            # 安全に扱う。
            parsed_manifest = []
            manifest_valid = False

        artifact_hash_valid = False
        if manifest_valid:
            try:
                recomputed_hash = compute_artifact_hash(
                    artifact_schema_version=artifact.artifact_schema_version,
                    article_id=artifact.article_id,
                    canonical_body_hash=artifact.canonical_body_hash,
                    renderer_version=artifact.renderer_version,
                    manifest=parsed_manifest,
                )
                artifact_hash_valid = recomputed_hash == artifact.artifact_hash
            except Exception:  # noqa: BLE001 - 検査は fail closed で false にするだけ
                artifact_hash_valid = False

        tracked_html_hash_valid = (
            compute_tracked_html_hash(artifact.tracked_html) == artifact.tracked_html_hash
        )

        strict_html_validation_valid = False
        if manifest_valid:
            try:
                reverse_tracked_html(
                    tracked_html=artifact.tracked_html, manifest=parsed_manifest
                )
                strict_html_validation_valid = True
            except PublicationSubstitutionError:
                strict_html_validation_valid = False

        article = self._articles.get_by_id(artifact.article_id)
        if article is None:
            current_canonical_status = CURRENT_CANONICAL_ARTICLE_NOT_FOUND
        else:
            current_body_hash = compute_text_hash(article.body or "")
            current_canonical_status = (
                CURRENT_CANONICAL_MATCH
                if current_body_hash == artifact.canonical_body_hash
                else CURRENT_CANONICAL_DRIFT
            )

        manifest_summary = (
            [self._summarize_entry(artifact, e) for e in parsed_manifest]
            if manifest_valid
            else []
        )
        all_current_evidence_resolvable = all(
            e.current.resolvable for e in manifest_summary
        )

        return ArtifactInspection(
            artifact_id=artifact.id,
            article_id=artifact.article_id,
            article_title=(article.title or "") if article is not None else "",
            canonical_body_hash=artifact.canonical_body_hash,
            renderer_version=artifact.renderer_version,
            artifact_schema_version=artifact.artifact_schema_version,
            substitution_count=artifact.substitution_count,
            artifact_hash=artifact.artifact_hash,
            tracked_html_hash=artifact.tracked_html_hash,
            approved=(artifact.approved_at is not None),
            approved_at=artifact.approved_at,
            approved_artifact_hash=artifact.approved_artifact_hash,
            generated_at=artifact.generated_at,
            created_at=artifact.created_at,
            manifest_summary=manifest_summary,
            artifact_hash_valid=artifact_hash_valid,
            tracked_html_hash_valid=tracked_html_hash_valid,
            manifest_valid=manifest_valid,
            strict_html_validation_valid=strict_html_validation_valid,
            current_canonical_status=current_canonical_status,
            all_current_evidence_resolvable=all_current_evidence_resolvable,
        )

    def _summarize_entry(self, artifact, entry: dict) -> ManifestEntrySummary:
        return ManifestEntrySummary(
            occurrence_ordinal=entry["occurrence_ordinal"],
            occurrence_identity_hash=entry["occurrence_identity_hash"],
            mapping_id=entry["mapping_id"],
            affiliate_link_target_id=entry["affiliate_link_target_id"],
            target_projection_version=entry["target_projection_version"],
            original_href=entry["original_href"],
            original_host=extract_original_host(entry["original_href"]),
            replacement_href_masked=_MASKED_REPLACEMENT_HREF,
            token_fingerprint=token_fingerprint(entry["token"]),
            rel_before=entry["rel_before"],
            rel_after=entry["rel_after"],
            is_affiliate_substitution=True,
            current=self._resolve_current_evidence(artifact=artifact, entry=entry),
        )

    # -- D-E1: CURRENT control-plane evidence (read-only, DB write 0) --------
    def _resolve_current_evidence(self, *, artifact, entry: dict) -> CurrentOccurrenceEvidence:
        mapping = self._mappings.get_by_id(entry["mapping_id"])
        if mapping is None:
            return self._unresolvable(FAIL_MAPPING_MISSING)
        if (
            mapping.article_id != artifact.article_id
            or mapping.occurrence_identity_hash != entry["occurrence_identity_hash"]
            or mapping.original_href != entry["original_href"]
            or mapping.affiliate_link_target_id != entry["affiliate_link_target_id"]
        ):
            return self._unresolvable(FAIL_MAPPING_IDENTITY_MISMATCH)

        target = self._targets.get_by_id(entry["affiliate_link_target_id"])
        if target is None:
            return self._unresolvable(FAIL_TARGET_MISSING)
        if (
            target.id != entry["affiliate_link_target_id"]
            or target.article_id != artifact.article_id
            or target.token != entry["token"]
        ):
            return self._unresolvable(FAIL_TARGET_IDENTITY_MISMATCH)

        program = self._programs.get_by_id(target.affiliate_program_id)
        if program is None:
            return self._unresolvable(FAIL_PROGRAM_MISSING)

        try:
            _, current_projection_version = runtime_status_and_version(target.status)
        except AffiliateProjectionError:
            current_projection_version = None

        return CurrentOccurrenceEvidence(
            resolvable=True,
            fail_reason=None,
            mapping_status=mapping.status,
            target_status=target.status,
            program_name=program.name,
            program_provider=program.provider,
            program_status=program.status,
            destination_host=target.destination_host,
            current_projection_version=current_projection_version,
            projection_eligible=self._resolve_projection_eligible(target),
            host_policy_eligible=is_host_approved(
                provider=program.provider, destination_host=target.destination_host
            ),
        )

    @staticmethod
    def _unresolvable(reason: str) -> CurrentOccurrenceEvidence:
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

    def _resolve_projection_eligible(self, target) -> bool:
        """既存 pure helper (is_target_eligible/resolve_latest_acknowledgement) を
        そのまま使う -- ArticleLinkOccurrencePreviewService と同じ判定ロジックの
        再利用で、reason の粒度は持たない単純な bool (D-E1 では informational)。"""

        runtime_origin = self._resolve_runtime_origin()
        if runtime_origin is None:
            return False
        runs = self._push_runs.latest_for_origin(runtime_origin)
        if not runs:
            return False
        resolution = resolve_latest_acknowledgement(runs)
        if resolution.manifest is None:
            return False
        try:
            expected_entry_hash = projection_from_target(target).projection_entry_hash
        except AffiliateProjectionError:
            return False
        return is_target_eligible(
            resolution,
            affiliate_link_target_id=target.id,
            expected_token_fingerprint=token_fingerprint(target.token),
            expected_link_identity_hash=target.link_identity_hash,
            expected_status=PROJECTION_STATUS_ACTIVE,
            expected_projection_version=PROJECTION_VERSION_ACTIVE,
            expected_entry_hash=expected_entry_hash,
        )

    def _resolve_runtime_origin(self) -> str | None:
        try:
            return require_https_origin(self._settings.wordpress_base_url, error_cls=ValueError)
        except ValueError:
            return None
