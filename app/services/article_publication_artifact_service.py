"""ArticlePublicationArtifactService — artifact 生成/承認オーケストレーション
(transaction owner)。

生成:
  未整列の substitution manifest エントリ群 -> canonical 化 (``build_substitution_manifest``)
  -> ``artifact_hash`` / ``tracked_html_hash`` を計算 -> 同一 hash が既にあれば
  その既存行を返す (idempotent — D-D0 §18) -> 無ければ新規 append。

承認:
  ``approved_at`` / ``approved_artifact_hash`` の NULL -> 値、ちょうど 1 回だけ。
  呼び出し側が明示した ``expected_artifact_hash`` が現在の ``artifact_hash`` と
  一致しない限り承認しない (D-D0.1 §8: 別の再生成 artifact を黙って承認できない)。

D-D1 は foundation のみ — 実際の HTML 置換や WordPress 更新は行わない。
``tracked_html`` は呼び出し側が既に生成した exact 文字列をそのまま凍結する。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.article.fact_freshness import to_storage_utc
from app.exceptions import ArticlePublicationArtifactError, EntityNotFoundError
from app.models import ArticlePublicationArtifact
from app.repositories.article_publication_artifact_repository import (
    ArticlePublicationArtifactRepository,
)
from app.wordpress.publication_artifact import (
    ARTIFACT_SCHEMA_VERSION,
    PublicationArtifactError,
    build_substitution_manifest,
    compute_artifact_hash,
    compute_tracked_html_hash,
    serialize_manifest,
)


class ArticlePublicationArtifactService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._artifacts = ArticlePublicationArtifactRepository(session)

    # -- create ------------------------------------------------------
    def create_artifact(
        self,
        *,
        article_id: int,
        canonical_body_hash: str,
        renderer_version: str,
        manifest_entries: Sequence[Mapping[str, Any]],
        tracked_html: str,
        artifact_schema_version: int = ARTIFACT_SCHEMA_VERSION,
        generated_at: datetime | None = None,
    ) -> ArticlePublicationArtifact:
        generated_at = generated_at or datetime.now(UTC)

        try:
            manifest = build_substitution_manifest(manifest_entries)
        except PublicationArtifactError as exc:
            raise ArticlePublicationArtifactError(str(exc)) from exc

        artifact_hash = compute_artifact_hash(
            artifact_schema_version=artifact_schema_version,
            article_id=article_id,
            canonical_body_hash=canonical_body_hash,
            renderer_version=renderer_version,
            manifest=manifest,
        )

        existing = self._artifacts.get_by_hash(artifact_hash)
        if existing is not None:
            # 同一の凍結入力からの再生成 -> 同じ artifact (idempotent no-op)。
            return existing

        artifact = self._artifacts.add(
            article_id=article_id,
            canonical_body_hash=canonical_body_hash,
            renderer_version=renderer_version,
            artifact_schema_version=artifact_schema_version,
            substitution_manifest_json=serialize_manifest(manifest),
            artifact_hash=artifact_hash,
            tracked_html=tracked_html,
            tracked_html_hash=compute_tracked_html_hash(tracked_html),
            substitution_count=len(manifest),
            approved_at=None,
            approved_artifact_hash=None,
            generated_at=to_storage_utc(generated_at),
        )
        self._session.commit()
        self._session.refresh(artifact)
        return artifact

    # -- approve (set-once) ---------------------------------------------
    def approve_artifact(
        self,
        artifact_id: int,
        *,
        expected_artifact_hash: str,
        approved_at: datetime | None = None,
    ) -> ArticlePublicationArtifact:
        approved_at = approved_at or datetime.now(UTC)
        artifact = self._require_artifact(artifact_id)
        self._artifacts.approve(
            artifact,
            approved_at=to_storage_utc(approved_at),
            approved_artifact_hash=expected_artifact_hash,
        )
        self._session.commit()
        self._session.refresh(artifact)
        return artifact

    # -- reads ------------------------------------------------------
    def get(self, artifact_id: int) -> ArticlePublicationArtifact:
        return self._require_artifact(artifact_id)

    def get_by_hash(self, artifact_hash: str) -> ArticlePublicationArtifact | None:
        return self._artifacts.get_by_hash(artifact_hash)

    def list_for_article(self, article_id: int) -> list[ArticlePublicationArtifact]:
        return self._artifacts.list_for_article(article_id)

    def _require_artifact(self, artifact_id: int) -> ArticlePublicationArtifact:
        artifact = self._artifacts.get_by_id(artifact_id)
        if artifact is None:
            raise EntityNotFoundError("ArticlePublicationArtifact", artifact_id)
        return artifact
