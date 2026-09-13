"""ArticlePublicationArtifact の永続化アクセス。

``commit`` は行わず ``flush`` のみ。汎用 ``update`` は持たない。作成後の変更は
``approve`` (set-once) のみ。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.exceptions import ArticlePublicationArtifactError
from app.models import ArticlePublicationArtifact


class ArticlePublicationArtifactRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, **fields) -> ArticlePublicationArtifact:
        entity = ArticlePublicationArtifact(**fields)
        self._session.add(entity)
        self._session.flush()
        return entity

    def get_by_id(self, artifact_id: int) -> ArticlePublicationArtifact | None:
        return self._session.get(ArticlePublicationArtifact, artifact_id)

    def get_by_hash(self, artifact_hash: str) -> ArticlePublicationArtifact | None:
        return self._session.scalars(
            select(ArticlePublicationArtifact).where(
                ArticlePublicationArtifact.artifact_hash == artifact_hash
            )
        ).first()

    def list_for_article(self, article_id: int) -> list[ArticlePublicationArtifact]:
        return list(
            self._session.scalars(
                select(ArticlePublicationArtifact)
                .where(ArticlePublicationArtifact.article_id == article_id)
                .order_by(ArticlePublicationArtifact.id)
            ).all()
        )

    # -- narrow set-once approval -------------------------------------
    def approve(
        self,
        entity: ArticlePublicationArtifact,
        *,
        approved_at: datetime,
        approved_artifact_hash: str,
    ) -> ArticlePublicationArtifact:
        if entity.approved_at is not None:
            raise ArticlePublicationArtifactError(
                f"artifact {entity.id} is already approved; no second approval"
            )
        if approved_artifact_hash != entity.artifact_hash:
            raise ArticlePublicationArtifactError(
                "approved_artifact_hash does not match the artifact's current "
                "artifact_hash"
            )
        entity.approved_at = approved_at
        entity.approved_artifact_hash = approved_artifact_hash
        self._session.flush()
        return entity
