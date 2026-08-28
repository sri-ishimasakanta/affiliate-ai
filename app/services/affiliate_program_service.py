"""AffiliateProgram (アフィリエイト案件カタログ) のビジネスロジック。

- Schema (外部入出力) とモデルの対応付け (フィールド名は一致しているため素直な写像)
- 重複チェック (同一 ``name`` + ``provider``)
- トランザクション境界 (commit / rollback) の制御

DB アクセス自体は :class:`AffiliateProgramRepository` に委譲する。
``tracking_url`` 等の値はログ・例外メッセージへ出力しない。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.affiliate.schemas import (
    AffiliateProgramCreate,
    AffiliateProgramRead,
    AffiliateProgramUpdate,
)
from app.exceptions import DuplicateEntityError, EntityNotFoundError
from app.models import AffiliateProgram
from app.models.enums import AffiliateProgramStatus
from app.repositories.affiliate_program_repository import AffiliateProgramRepository

_ENTITY = "AffiliateProgram"

_CREATE_FIELDS = (
    "name",
    "provider",
    "category",
    "commission_type",
    "commission_value",
    "currency",
    "landing_page_url",
    "tracking_url",
    "notes",
    "match_terms",
    "status",
)


class AffiliateProgramService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._repo = AffiliateProgramRepository(session)

    # -- read ---------------------------------------------------------------
    def get_program(self, program_id: int) -> AffiliateProgramRead:
        entity = self._repo.get_by_id(program_id)
        if entity is None:
            raise EntityNotFoundError(_ENTITY, program_id)
        return self._to_read(entity)

    def list_programs(
        self,
        *,
        status: AffiliateProgramStatus | None = None,
        provider: str | None = None,
        category: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AffiliateProgramRead]:
        rows = self._repo.list(
            status=status,
            provider=provider,
            category=category,
            limit=limit,
            offset=offset,
        )
        return [self._to_read(entity) for entity in rows]

    # -- write --------------------------------------------------------------
    def create_program(
        self, payload: AffiliateProgramCreate
    ) -> AffiliateProgramRead:
        if (
            self._repo.get_by_name_and_provider(payload.name, payload.provider)
            is not None
        ):
            raise DuplicateEntityError(
                _ENTITY, "name+provider", f"{payload.name} / {payload.provider}"
            )

        entity = self._repo.create(
            **{field: getattr(payload, field) for field in _CREATE_FIELDS}
        )
        self._commit()
        return self._to_read(entity)

    def update_program(
        self, program_id: int, payload: AffiliateProgramUpdate
    ) -> AffiliateProgramRead:
        entity = self._repo.get_by_id(program_id)
        if entity is None:
            raise EntityNotFoundError(_ENTITY, program_id)

        values: dict[str, Any] = payload.model_dump(exclude_unset=True)
        if "status" in values and values["status"] is not None:
            values["status"] = str(values["status"])

        if values:
            self._repo.update(entity, values)
            self._commit()
        return self._to_read(entity)

    def delete_program(self, program_id: int) -> None:
        entity = self._repo.get_by_id(program_id)
        if entity is None:
            raise EntityNotFoundError(_ENTITY, program_id)
        self._repo.delete(entity)
        self._commit()

    # -- helpers ----------------------------------------------------------
    def _commit(self) -> None:
        # 重複は create_program で事前チェックするため DB 制約はなく、
        # commit 失敗時は種類を問わず rollback して再送出する。
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    @staticmethod
    def _to_read(entity: AffiliateProgram) -> AffiliateProgramRead:
        return AffiliateProgramRead(
            id=entity.id,
            name=entity.name,
            provider=entity.provider,
            category=entity.category,
            commission_type=entity.commission_type,
            commission_value=entity.commission_value,
            currency=entity.currency,
            landing_page_url=entity.landing_page_url,
            tracking_url=entity.tracking_url,
            notes=entity.notes,
            match_terms=list(entity.match_terms) if entity.match_terms else [],
            status=AffiliateProgramStatus(entity.status),
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )
