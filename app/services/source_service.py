"""Source (公式ページの観測記録) のビジネスロジック。

- Article 存在確認
- URL safety (https / credential 除外 / tracking host 拒否 / canonicalize)
- Source は immutable: update は提供しない (PATCH なし)
- delete: ArticleFact から参照されている Source は 409 で拒否
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.article.schemas import SourceCreate, SourceRead
from app.article.source_url_safety import UrlSafetyError, validate_and_canonicalize
from app.exceptions import (
    DuplicateEntityError,
    EntityInUseError,
    EntityNotFoundError,
    FactValidationError,
)
from app.models import AffiliateProgram, Source
from app.repositories.article_repository import ArticleRepository
from app.repositories.source_repository import SourceRepository

_ENTITY = "Source"


class SourceService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._sources = SourceRepository(session)
        self._articles = ArticleRepository(session)

    # -- read ----------------------------------------------------------
    def list_by_article(self, article_id: int) -> list[SourceRead]:
        self._ensure_article(article_id)
        return [self._to_read(s) for s in self._sources.list_by_article(article_id)]

    def get(self, article_id: int, source_id: int) -> SourceRead:
        return self._to_read(self._get_owned(article_id, source_id))

    # -- write ---------------------------------------------------------
    def create(self, article_id: int, payload: SourceCreate) -> SourceRead:
        self._ensure_article(article_id)
        canonical = self._safe_url(payload.source_url)
        checked_at = self._require_aware_not_future(payload.checked_at)

        existing = self._sources.find_observation(
            article_id=article_id, source_url=canonical, checked_at=checked_at
        )
        if existing is not None:
            raise DuplicateEntityError(_ENTITY, "source_url", canonical)

        try:
            entity = self._sources.create(
                article_id=article_id,
                source_type=payload.source_type,
                source_url=canonical,
                title=payload.title,
                checked_at=checked_at,
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        self._session.refresh(entity)
        return self._to_read(entity)

    def delete(self, article_id: int, source_id: int) -> None:
        entity = self._get_owned(article_id, source_id)
        if self._sources.is_referenced_by_fact(source_id):
            raise EntityInUseError(_ENTITY, source_id, "ArticleFact")
        try:
            self._sources.delete(entity)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    # -- helpers -----------------------------------------------------
    def safe_url(self, url: str) -> str:
        """URL safety を検証し canonical URL を返す (DB write なし・bulk import 用)。"""

        return self._safe_url(url)

    def require_aware_not_future(self, value: datetime) -> datetime:
        return self._require_aware_not_future(value)

    def _safe_url(self, url: str) -> str:
        blocked = {
            (p.tracking_url or "").split("//")[-1].split("/")[0].lower()
            for p in self._session.scalars(
                select(AffiliateProgram).where(AffiliateProgram.tracking_url.isnot(None))
            ).all()
        }
        blocked.discard("")
        try:
            return validate_and_canonicalize(url, blocked_hosts=frozenset(blocked))
        except UrlSafetyError as exc:
            raise FactValidationError(f"source_url unsafe: {exc}") from exc

    @staticmethod
    def _require_aware_not_future(value: datetime) -> datetime:
        if value.tzinfo is None:
            raise FactValidationError("checked_at must be timezone-aware")
        if value > datetime.now(UTC):
            raise FactValidationError("checked_at must not be in the future")
        return value

    def _ensure_article(self, article_id: int) -> None:
        if self._articles.get_by_id(article_id) is None:
            raise EntityNotFoundError("Article", article_id)

    def _get_owned(self, article_id: int, source_id: int) -> Source:
        self._ensure_article(article_id)
        entity = self._sources.get_by_id(source_id)
        if entity is None or entity.article_id != article_id:
            raise EntityNotFoundError(_ENTITY, source_id)
        return entity

    @staticmethod
    def _to_read(entity: Source) -> SourceRead:
        return SourceRead(
            id=entity.id,
            article_id=entity.article_id,
            source_type=entity.source_type,
            source_url=entity.source_url,
            title=entity.title,
            checked_at=entity.checked_at,
            created_at=entity.created_at,
        )
