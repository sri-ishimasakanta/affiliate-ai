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
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.affiliate.projection_push_acknowledgement import token_fingerprint
from app.article.draft_promotion_canonical import compute_text_hash
from app.exceptions import EntityNotFoundError
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


class ArticlePublicationArtifactInspectionService:
    """READ-ONLY。commit を一度も呼ばない。"""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._articles = ArticleRepository(session)
        self._artifacts = ArticlePublicationArtifactRepository(session)

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
            [self._summarize_entry(e) for e in parsed_manifest] if manifest_valid else []
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
        )

    @staticmethod
    def _summarize_entry(entry: dict) -> ManifestEntrySummary:
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
        )
