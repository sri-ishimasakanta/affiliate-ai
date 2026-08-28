"""Keyword のビジネスロジック。

- Schema (外部入出力) とモデルの対応付け
- 重複チェック
- status 遷移ルールの適用
- トランザクション境界 (commit / rollback) の制御

DB アクセス自体は :class:`KeywordRepository` に委譲する。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.exceptions import DuplicateEntityError, EntityNotFoundError
from app.keyword.schemas import KeywordCreate, KeywordRead, KeywordUpdate
from app.models import Keyword
from app.models.enums import KeywordStatus
from app.repositories.keyword_repository import KeywordRepository
from app.services.status_transitions import (
    KEYWORD_TRANSITIONS,
    ensure_transition_allowed,
)

_ENTITY = "Keyword"

# Schema フィールド名 -> モデル属性名
_UPDATE_FIELD_MAP = {
    "search_intent": "intent",
    "category": "category",
}


class KeywordService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._repo = KeywordRepository(session)

    # -- read ---------------------------------------------------------------
    def get_keyword(self, keyword_id: int) -> KeywordRead:
        entity = self._repo.get_by_id(keyword_id)
        if entity is None:
            raise EntityNotFoundError(_ENTITY, keyword_id)
        return self._to_read(entity)

    def list_keywords(self, *, limit: int = 100, offset: int = 0) -> list[KeywordRead]:
        return [self._to_read(entity) for entity in self._repo.list(limit=limit, offset=offset)]

    # -- write --------------------------------------------------------------
    def create_keyword(self, payload: KeywordCreate) -> KeywordRead:
        if self._repo.get_by_keyword(payload.keyword) is not None:
            raise DuplicateEntityError(_ENTITY, "keyword", payload.keyword)

        entity = self._repo.create(
            keyword=payload.keyword,
            intent=payload.search_intent,
            category=payload.category,
        )
        self._commit(on_conflict=("keyword", payload.keyword))
        return self._to_read(entity)

    def update_keyword(self, keyword_id: int, payload: KeywordUpdate) -> KeywordRead:
        entity = self._repo.get_by_id(keyword_id)
        if entity is None:
            raise EntityNotFoundError(_ENTITY, keyword_id)

        values: dict[str, Any] = {
            _UPDATE_FIELD_MAP[field]: value
            for field, value in payload.model_dump(exclude_unset=True).items()
        }
        if values:
            self._repo.update(entity, values)
            self._commit()
        return self._to_read(entity)

    def delete_keyword(self, keyword_id: int) -> None:
        entity = self._repo.get_by_id(keyword_id)
        if entity is None:
            raise EntityNotFoundError(_ENTITY, keyword_id)
        self._repo.delete(entity)
        self._commit()

    def change_status(self, keyword_id: int, target: KeywordStatus) -> KeywordRead:
        entity = self._repo.get_by_id(keyword_id)
        if entity is None:
            raise EntityNotFoundError(_ENTITY, keyword_id)

        current = KeywordStatus(entity.status)
        target = KeywordStatus(target)
        ensure_transition_allowed(_ENTITY, current, target, KEYWORD_TRANSITIONS)

        if target != current:
            self._repo.update(entity, {"status": target})
            self._commit()
        return self._to_read(entity)

    # -- helpers ----------------------------------------------------------
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
    def _to_read(entity: Keyword) -> KeywordRead:
        return KeywordRead(
            id=entity.id,
            keyword=entity.keyword,
            search_intent=entity.intent,
            category=entity.category,
            status=KeywordStatus(entity.status),
            opportunity_score=entity.opportunity_score,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )
