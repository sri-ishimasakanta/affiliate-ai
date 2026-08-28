"""Article と AffiliateProgram の関連 (中間モデル) のビジネスロジック。

- Article / AffiliateProgram の存在確認、関連の重複確認
- **1 Article につき is_primary=True は最大 1 件** をアプリ層で保証する
  (DB の partial unique index は V1 では作らない → :mod:`docs` に制約を明記)
- トランザクション境界 (commit / rollback) の制御

DB アクセスは Repository に委譲する。planned 段階での relation 登録は許可するが、
tracking URL を本文へ挿入する *link injection* は後続 Phase (approved 後) の責務。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.article.schemas import (
    ArticleAffiliateProgramCreate,
    ArticleAffiliateProgramRead,
    ArticleAffiliateProgramUpdate,
)
from app.exceptions import DuplicateEntityError, EntityNotFoundError
from app.models import ArticleAffiliateProgram
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.repositories.article_affiliate_program_repository import (
    ArticleAffiliateProgramRepository,
)
from app.repositories.article_repository import ArticleRepository

_ENTITY = "ArticleAffiliateProgram"


class ArticleAffiliateProgramService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._links = ArticleAffiliateProgramRepository(session)
        self._articles = ArticleRepository(session)
        self._programs = AffiliateProgramRepository(session)

    # -- read ------------------------------------------------------------
    def list_by_article(self, article_id: int) -> list[ArticleAffiliateProgramRead]:
        self._ensure_article(article_id)
        return [self._to_read(link) for link in self._links.list_by_article(article_id)]

    # -- write -----------------------------------------------------------
    def attach(
        self, article_id: int, payload: ArticleAffiliateProgramCreate
    ) -> ArticleAffiliateProgramRead:
        self._ensure_article(article_id)
        self._ensure_program(payload.affiliate_program_id)
        if (
            self._links.get_by_article_and_program(
                article_id, payload.affiliate_program_id
            )
            is not None
        ):
            raise DuplicateEntityError(
                _ENTITY, "affiliate_program_id", payload.affiliate_program_id
            )

        try:
            if payload.is_primary:
                self._demote_primary(article_id)
            entity = self._links.create(
                article_id=article_id,
                affiliate_program_id=payload.affiliate_program_id,
                is_primary=payload.is_primary,
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        self._session.refresh(entity)
        return self._to_read(entity)

    def update_link(
        self,
        article_id: int,
        link_id: int,
        payload: ArticleAffiliateProgramUpdate,
    ) -> ArticleAffiliateProgramRead:
        entity = self._get_link_for_article(article_id, link_id)
        try:
            if payload.is_primary:
                self._demote_primary(article_id, except_link_id=entity.id)
            self._links.update(entity, {"is_primary": payload.is_primary})
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        self._session.refresh(entity)
        return self._to_read(entity)

    def set_primary(
        self, article_id: int, link_id: int
    ) -> ArticleAffiliateProgramRead:
        entity = self._get_link_for_article(article_id, link_id)
        try:
            self._demote_primary(article_id, except_link_id=entity.id)
            self._links.update(entity, {"is_primary": True})
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        self._session.refresh(entity)
        return self._to_read(entity)

    def detach(self, article_id: int, link_id: int) -> None:
        entity = self._get_link_for_article(article_id, link_id)
        try:
            self._links.delete(entity)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    # -- helpers -------------------------------------------------------
    def _demote_primary(
        self, article_id: int, *, except_link_id: int | None = None
    ) -> None:
        current = self._links.get_primary(article_id)
        if current is not None and current.id != except_link_id:
            self._links.update(current, {"is_primary": False})

    def _get_link_for_article(
        self, article_id: int, link_id: int
    ) -> ArticleAffiliateProgram:
        self._ensure_article(article_id)
        entity = self._links.get_by_id(link_id)
        if entity is None or entity.article_id != article_id:
            raise EntityNotFoundError(_ENTITY, link_id)
        return entity

    def _ensure_article(self, article_id: int) -> None:
        if self._articles.get_by_id(article_id) is None:
            raise EntityNotFoundError("Article", article_id)

    def _ensure_program(self, program_id: int) -> None:
        if self._programs.get_by_id(program_id) is None:
            raise EntityNotFoundError("AffiliateProgram", program_id)

    @staticmethod
    def _to_read(entity: ArticleAffiliateProgram) -> ArticleAffiliateProgramRead:
        return ArticleAffiliateProgramRead(
            id=entity.id,
            article_id=entity.article_id,
            affiliate_program_id=entity.affiliate_program_id,
            is_primary=entity.is_primary,
            created_at=entity.created_at,
        )
