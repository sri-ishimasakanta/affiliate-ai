"""ArticleDraftPromotion の永続化アクセス。

``commit`` は行わず ``flush`` のみ。immutable のため update / delete メソッドを持たない
(内容変更は新しい行の append)。latest は ``created_at DESC, id DESC``。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ArticleDraftPromotion

_LATEST_ORDER = (
    ArticleDraftPromotion.created_at.desc(),
    ArticleDraftPromotion.id.desc(),
)


class ArticleDraftPromotionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        *,
        article_id: int,
        source_run_id: int,
        source_prompt_input_hash: str,
        source_rendered_prompt_hash: str,
        body_markdown: str,
        meta_description: str,
        body_hash: str,
        meta_hash: str,
        candidate_content_hash: str,
        validation_report: dict,
        human_review_notes: list | None,
        idempotency_key: str | None,
        promoted_at,
    ) -> ArticleDraftPromotion:
        entity = ArticleDraftPromotion(
            article_id=article_id,
            source_run_id=source_run_id,
            source_prompt_input_hash=source_prompt_input_hash,
            source_rendered_prompt_hash=source_rendered_prompt_hash,
            body_markdown=body_markdown,
            meta_description=meta_description,
            body_hash=body_hash,
            meta_hash=meta_hash,
            candidate_content_hash=candidate_content_hash,
            validation_report=validation_report,
            human_review_notes=human_review_notes,
            idempotency_key=idempotency_key,
            promoted_at=promoted_at,
        )
        self._session.add(entity)
        self._session.flush()
        return entity

    # -- read -----------------------------------------------------
    def get_by_id(self, promotion_id: int) -> ArticleDraftPromotion | None:
        return self._session.get(ArticleDraftPromotion, promotion_id)

    def list_by_article(self, article_id: int) -> list[ArticleDraftPromotion]:
        stmt = (
            select(ArticleDraftPromotion)
            .where(ArticleDraftPromotion.article_id == article_id)
            .order_by(*_LATEST_ORDER)
        )
        return list(self._session.scalars(stmt).all())

    def get_by_idempotency_key(self, key: str) -> ArticleDraftPromotion | None:
        stmt = select(ArticleDraftPromotion).where(
            ArticleDraftPromotion.idempotency_key == key
        )
        return self._session.scalars(stmt).first()

    def find_by_article_and_candidate_hash(
        self, article_id: int, candidate_content_hash: str
    ) -> ArticleDraftPromotion | None:
        stmt = select(ArticleDraftPromotion).where(
            ArticleDraftPromotion.article_id == article_id,
            ArticleDraftPromotion.candidate_content_hash == candidate_content_hash,
        )
        return self._session.scalars(stmt).first()

    def count_by_article(self, article_id: int) -> int:
        stmt = select(ArticleDraftPromotion).where(
            ArticleDraftPromotion.article_id == article_id
        )
        return len(list(self._session.scalars(stmt).all()))
