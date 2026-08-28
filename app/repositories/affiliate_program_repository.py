"""AffiliateProgram (アフィリエイト案件カタログ) の永続化アクセス。

責務は SQLAlchemy ``Session`` を用いた DB アクセスのみ。
重複チェックや正規化などのビジネスルールは持たない。
``commit`` は行わず ``flush`` のみ行い、トランザクション境界は Service 側に委ねる。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AffiliateProgram
from app.models.enums import AffiliateProgramStatus


class AffiliateProgramRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        name: str,
        provider: str | None = None,
        category: str | None = None,
        commission_type: str | None = None,
        commission_value: float | None = None,
        currency: str | None = None,
        landing_page_url: str | None = None,
        tracking_url: str | None = None,
        notes: str | None = None,
        match_terms: list[str] | None = None,
        status: AffiliateProgramStatus | str = AffiliateProgramStatus.ACTIVE,
    ) -> AffiliateProgram:
        entity = AffiliateProgram(
            name=name,
            provider=provider,
            category=category,
            commission_type=commission_type,
            commission_value=commission_value,
            currency=currency,
            landing_page_url=landing_page_url,
            tracking_url=tracking_url,
            notes=notes,
            match_terms=match_terms,
            status=str(status),
        )
        self._session.add(entity)
        self._session.flush()
        return entity

    def get_by_id(self, program_id: int) -> AffiliateProgram | None:
        return self._session.get(AffiliateProgram, program_id)

    def get_by_name_and_provider(
        self, name: str, provider: str | None
    ) -> AffiliateProgram | None:
        statement = select(AffiliateProgram).where(AffiliateProgram.name == name)
        if provider is None:
            statement = statement.where(AffiliateProgram.provider.is_(None))
        else:
            statement = statement.where(AffiliateProgram.provider == provider)
        return self._session.scalars(statement).first()

    def list(
        self,
        *,
        status: AffiliateProgramStatus | str | None = None,
        provider: str | None = None,
        category: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AffiliateProgram]:
        statement = select(AffiliateProgram)
        if status is not None:
            statement = statement.where(AffiliateProgram.status == str(status))
        if provider is not None:
            statement = statement.where(AffiliateProgram.provider == provider)
        if category is not None:
            statement = statement.where(AffiliateProgram.category == category)
        statement = statement.order_by(AffiliateProgram.id).limit(limit).offset(offset)
        return list(self._session.scalars(statement).all())

    def list_active(
        self, *, limit: int = 100, offset: int = 0
    ) -> list[AffiliateProgram]:
        return self.list(
            status=AffiliateProgramStatus.ACTIVE, limit=limit, offset=offset
        )

    def count(
        self, *, status: AffiliateProgramStatus | str | None = None
    ) -> int:
        statement = select(func.count()).select_from(AffiliateProgram)
        if status is not None:
            statement = statement.where(AffiliateProgram.status == str(status))
        return int(self._session.scalar(statement) or 0)

    def update(
        self, entity: AffiliateProgram, values: Mapping[str, Any]
    ) -> AffiliateProgram:
        for field, value in values.items():
            setattr(entity, field, value)
        self._session.flush()
        return entity

    def delete(self, entity: AffiliateProgram) -> None:
        self._session.delete(entity)
        self._session.flush()
