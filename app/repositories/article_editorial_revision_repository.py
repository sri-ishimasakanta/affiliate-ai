"""ArticleEditorialRevision の永続化アクセス。

``commit`` は行わず ``flush`` のみ。immutable のため update / delete メソッドを持たない
(内容変更は新しい行の append)。latest は ``created_at DESC, id DESC``。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ArticleEditorialRevision

_LATEST_ORDER = (
    ArticleEditorialRevision.created_at.desc(),
    ArticleEditorialRevision.id.desc(),
)


class ArticleEditorialRevisionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        *,
        article_id: int,
        base_promotion_id: int,
        revision_reason: str,
        article_status_at_revision: str,
        published_update_intent: str | None,
        previous_body_hash: str,
        previous_meta_hash: str,
        body_markdown: str,
        meta_description: str,
        body_hash: str,
        meta_hash: str,
        revision_content_hash: str,
        validation_report: dict,
        editor_notes: list | None,
        idempotency_key: str | None,
        revised_at,
    ) -> ArticleEditorialRevision:
        entity = ArticleEditorialRevision(
            article_id=article_id,
            base_promotion_id=base_promotion_id,
            revision_reason=revision_reason,
            article_status_at_revision=article_status_at_revision,
            published_update_intent=published_update_intent,
            previous_body_hash=previous_body_hash,
            previous_meta_hash=previous_meta_hash,
            body_markdown=body_markdown,
            meta_description=meta_description,
            body_hash=body_hash,
            meta_hash=meta_hash,
            revision_content_hash=revision_content_hash,
            validation_report=validation_report,
            editor_notes=editor_notes,
            idempotency_key=idempotency_key,
            revised_at=revised_at,
        )
        self._session.add(entity)
        self._session.flush()
        return entity

    # -- read -----------------------------------------------------
    def get_by_id(self, revision_id: int) -> ArticleEditorialRevision | None:
        return self._session.get(ArticleEditorialRevision, revision_id)

    def list_by_article(self, article_id: int) -> list[ArticleEditorialRevision]:
        statement = (
            select(ArticleEditorialRevision)
            .where(ArticleEditorialRevision.article_id == article_id)
            .order_by(*_LATEST_ORDER)
        )
        return list(self._session.scalars(statement).all())

    def get_latest(self, article_id: int) -> ArticleEditorialRevision | None:
        statement = (
            select(ArticleEditorialRevision)
            .where(ArticleEditorialRevision.article_id == article_id)
            .order_by(*_LATEST_ORDER)
            .limit(1)
        )
        return self._session.scalars(statement).first()

    def get_by_idempotency_key(self, key: str) -> ArticleEditorialRevision | None:
        statement = select(ArticleEditorialRevision).where(
            ArticleEditorialRevision.idempotency_key == key
        )
        return self._session.scalars(statement).first()

    def find_by_article_and_content_hash(
        self, article_id: int, revision_content_hash: str
    ) -> ArticleEditorialRevision | None:
        statement = select(ArticleEditorialRevision).where(
            ArticleEditorialRevision.article_id == article_id,
            ArticleEditorialRevision.revision_content_hash == revision_content_hash,
        )
        return self._session.scalars(statement).first()
