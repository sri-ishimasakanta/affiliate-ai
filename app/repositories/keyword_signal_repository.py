"""KeywordSignal (component 値の根拠となる観測データ) の永続化アクセス。

責務は SQLAlchemy ``Session`` を用いた DB アクセスのみ。
正規化ロジックや存在チェック等のビジネスロジックは持たない。
``commit`` は行わず ``flush`` のみ。トランザクション境界は Service 側。

**履歴の並び順は一貫して ``observed_at DESC, id DESC``**。
"最新" = 最も新しく「観測された」Signal。同一 ``observed_at`` なら ``id`` が大きい方。
外部 Provider から過去データをバックフィルしても "最新" の意味が崩れないようにする。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import KeywordSignal
from app.models.enums import KeywordSignalComponent


class KeywordSignalRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        keyword_id: int,
        component: KeywordSignalComponent | str,
        normalized_value: float,
        provider: str,
        observed_at: datetime,
        raw_data: dict[str, Any] | list[Any] | None = None,
        source_reference: str | None = None,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
    ) -> KeywordSignal:
        entity = KeywordSignal(
            keyword_id=keyword_id,
            component=str(component),
            normalized_value=normalized_value,
            provider=provider,
            observed_at=observed_at,
            raw_data=raw_data,
            source_reference=source_reference,
            period_start=period_start,
            period_end=period_end,
        )
        self._session.add(entity)
        self._session.flush()
        return entity

    def get_by_id(self, signal_id: int) -> KeywordSignal | None:
        return self._session.get(KeywordSignal, signal_id)

    def get_latest(
        self,
        keyword_id: int,
        component: KeywordSignalComponent | str,
    ) -> KeywordSignal | None:
        statement = (
            select(KeywordSignal)
            .where(
                KeywordSignal.keyword_id == keyword_id,
                KeywordSignal.component == str(component),
            )
            .order_by(KeywordSignal.observed_at.desc(), KeywordSignal.id.desc())
            .limit(1)
        )
        return self._session.scalars(statement).first()

    def list_by_keyword(
        self,
        keyword_id: int,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[KeywordSignal]:
        statement = (
            select(KeywordSignal)
            .where(KeywordSignal.keyword_id == keyword_id)
            .order_by(KeywordSignal.observed_at.desc(), KeywordSignal.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(statement).all())

    def list_by_component(
        self,
        keyword_id: int,
        component: KeywordSignalComponent | str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[KeywordSignal]:
        statement = (
            select(KeywordSignal)
            .where(
                KeywordSignal.keyword_id == keyword_id,
                KeywordSignal.component == str(component),
            )
            .order_by(KeywordSignal.observed_at.desc(), KeywordSignal.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(statement).all())
