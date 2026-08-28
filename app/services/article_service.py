"""Article のビジネスロジック。

- Schema (外部入出力) とモデルの対応付け
- slug 重複チェック / keyword_id 存在チェック
- status 遷移ルールの適用 (published への遷移時に published_at を設定)
- トランザクション境界 (commit / rollback) の制御

DB アクセス自体は :class:`ArticleRepository` / :class:`KeywordRepository` に委譲する。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.article.schemas import ArticleCreate, ArticleRead, ArticleUpdate
from app.exceptions import DuplicateEntityError, EntityNotFoundError
from app.models import Article
from app.models.enums import ArticleStatus
from app.repositories.article_repository import ArticleRepository
from app.repositories.keyword_repository import KeywordRepository
from app.services.status_transitions import (
    ARTICLE_TRANSITIONS,
    ensure_transition_allowed,
)

_ENTITY = "Article"

# Schema フィールド名 -> モデル属性名
_UPDATE_FIELD_MAP = {
    "title": "title",
    "slug": "slug",
    "draft_content": "body",
}


class ArticleService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._repo = ArticleRepository(session)
        self._keywords = KeywordRepository(session)

    # -- read ---------------------------------------------------------------
    def get_article(self, article_id: int) -> ArticleRead:
        entity = self._repo.get_by_id(article_id)
        if entity is None:
            raise EntityNotFoundError(_ENTITY, article_id)
        return self._to_read(entity)

    def list_articles(self, *, limit: int = 100, offset: int = 0) -> list[ArticleRead]:
        return [self._to_read(entity) for entity in self._repo.list(limit=limit, offset=offset)]

    # -- write --------------------------------------------------------------
    def create_article(self, payload: ArticleCreate) -> ArticleRead:
        if self._repo.get_by_slug(payload.slug) is not None:
            raise DuplicateEntityError(_ENTITY, "slug", payload.slug)
        self._ensure_keyword_exists(payload.keyword_id)

        entity = self._repo.create(
            title=payload.title,
            slug=payload.slug,
            keyword_id=payload.keyword_id,
        )
        self._commit(on_conflict=("slug", payload.slug))
        return self._to_read(entity)

    def update_article(self, article_id: int, payload: ArticleUpdate) -> ArticleRead:
        entity = self._repo.get_by_id(article_id)
        if entity is None:
            raise EntityNotFoundError(_ENTITY, article_id)

        data = payload.model_dump(exclude_unset=True)
        new_slug = data.get("slug")
        if new_slug is not None and new_slug != entity.slug:
            existing = self._repo.get_by_slug(new_slug)
            if existing is not None and existing.id != entity.id:
                raise DuplicateEntityError(_ENTITY, "slug", new_slug)

        values: dict[str, Any] = {
            _UPDATE_FIELD_MAP[field]: value for field, value in data.items()
        }
        if values:
            self._repo.update(entity, values)
            self._commit(on_conflict=("slug", new_slug) if new_slug is not None else None)
        return self._to_read(entity)

    def delete_article(self, article_id: int) -> None:
        entity = self._repo.get_by_id(article_id)
        if entity is None:
            raise EntityNotFoundError(_ENTITY, article_id)
        self._repo.delete(entity)
        self._commit()

    def change_status(self, article_id: int, target: ArticleStatus) -> ArticleRead:
        entity = self._repo.get_by_id(article_id)
        if entity is None:
            raise EntityNotFoundError(_ENTITY, article_id)

        current = ArticleStatus(entity.status)
        target = ArticleStatus(target)
        ensure_transition_allowed(_ENTITY, current, target, ARTICLE_TRANSITIONS)

        if target != current:
            values: dict[str, Any] = {"status": target}
            if target is ArticleStatus.PUBLISHED and entity.published_at is None:
                values["published_at"] = datetime.now(UTC)
            self._repo.update(entity, values)
            self._commit()
        return self._to_read(entity)

    # -- helpers ----------------------------------------------------------
    def _ensure_keyword_exists(self, keyword_id: int | None) -> None:
        if keyword_id is None:
            return
        if self._keywords.get_by_id(keyword_id) is None:
            raise EntityNotFoundError("Keyword", keyword_id)

    def _commit(self, *, on_conflict: tuple[str, object] | None = None) -> None:
        try:
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            if on_conflict is not None:
                field, value = on_conflict
                raise DuplicateEntityError(_ENTITY, field, value) from exc
            raise

    @staticmethod
    def _to_read(entity: Article) -> ArticleRead:
        return ArticleRead(
            id=entity.id,
            keyword_id=entity.keyword_id,
            title=entity.title,
            slug=entity.slug,
            status=ArticleStatus(entity.status),
            draft_content=entity.body,
            published_url=entity.published_url,
            wordpress_id=entity.wordpress_post_id,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
            published_at=entity.published_at,
        )
