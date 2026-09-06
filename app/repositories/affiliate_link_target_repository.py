"""AffiliateLinkTarget の永続化アクセス。

``commit`` は行わず ``flush`` のみ。generic な ``update`` / ``delete`` /
destination 変更メソッドは持たない。変更は狭い lifecycle メソッドのみ。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AffiliateLinkTarget
from app.models.affiliate_link_target import (
    ALT_ACTIVE,
    ALT_DISABLED,
    ALT_SUPERSEDED,
)


class AffiliateLinkTargetRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, **fields) -> AffiliateLinkTarget:
        entity = AffiliateLinkTarget(**fields)
        self._session.add(entity)
        self._session.flush()
        return entity

    def get_by_id(self, target_id: int) -> AffiliateLinkTarget | None:
        return self._session.get(AffiliateLinkTarget, target_id)

    def get_by_token(self, token: str) -> AffiliateLinkTarget | None:
        return self._session.scalars(
            select(AffiliateLinkTarget).where(AffiliateLinkTarget.token == token)
        ).first()

    def token_exists(self, token: str) -> bool:
        return (
            self._session.scalars(
                select(AffiliateLinkTarget.id).where(
                    AffiliateLinkTarget.token == token
                )
            ).first()
            is not None
        )

    def get_by_idempotency_key(self, key: str) -> AffiliateLinkTarget | None:
        return self._session.scalars(
            select(AffiliateLinkTarget).where(
                AffiliateLinkTarget.idempotency_key == key
            )
        ).first()

    def get_active_for_article_program(
        self, article_id: int, affiliate_program_id: int
    ) -> AffiliateLinkTarget | None:
        return self._session.scalars(
            select(AffiliateLinkTarget).where(
                AffiliateLinkTarget.article_id == article_id,
                AffiliateLinkTarget.affiliate_program_id == affiliate_program_id,
                AffiliateLinkTarget.status == ALT_ACTIVE,
            )
        ).first()

    def list_active_for_article(
        self, article_id: int
    ) -> list[AffiliateLinkTarget]:
        return list(
            self._session.scalars(
                select(AffiliateLinkTarget)
                .where(
                    AffiliateLinkTarget.article_id == article_id,
                    AffiliateLinkTarget.status == ALT_ACTIVE,
                )
                .order_by(AffiliateLinkTarget.id)
            ).all()
        )

    # -- narrow lifecycle transitions --------------------------------
    def mark_disabled(
        self, entity: AffiliateLinkTarget, *, disabled_at: datetime
    ) -> AffiliateLinkTarget:
        entity.status = ALT_DISABLED
        entity.disabled_at = disabled_at
        self._session.flush()
        return entity

    def begin_supersede(
        self, entity: AffiliateLinkTarget, *, disabled_at: datetime
    ) -> AffiliateLinkTarget:
        """old を active から外す (partial unique index を空ける)。pointer はまだ張らない。"""

        entity.status = ALT_SUPERSEDED
        entity.disabled_at = disabled_at
        self._session.flush()
        return entity

    def link_supersede(
        self, entity: AffiliateLinkTarget, *, superseded_by_id: int
    ) -> AffiliateLinkTarget:
        entity.superseded_by_id = superseded_by_id
        self._session.flush()
        return entity
