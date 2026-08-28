"""DraftInputSnapshot の永続化アクセス。

``commit`` は行わず ``flush`` のみ。Snapshot は immutable のため update / delete
メソッドを持たない (内容変更は新しい行の append)。latest は ``frozen_at DESC, id DESC``。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DraftInputSnapshot

_LATEST_ORDER = (DraftInputSnapshot.frozen_at.desc(), DraftInputSnapshot.id.desc())


class DraftInputSnapshotRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def append(
        self,
        *,
        article_id: int,
        snapshot_version: str,
        builder_version: str,
        plan_snapshot_origin: str,
        primary_affiliate_program_id: int | None,
        comparison_program_ids: list[int],
        drafting_allowed_at_freeze: bool,
        payload: dict,
        content_hash: str,
        frozen_at,
    ) -> DraftInputSnapshot:
        entity = DraftInputSnapshot(
            article_id=article_id,
            snapshot_version=snapshot_version,
            builder_version=builder_version,
            plan_snapshot_origin=plan_snapshot_origin,
            primary_affiliate_program_id=primary_affiliate_program_id,
            comparison_program_ids=comparison_program_ids,
            drafting_allowed_at_freeze=drafting_allowed_at_freeze,
            payload=payload,
            content_hash=content_hash,
            frozen_at=frozen_at,
        )
        self._session.add(entity)
        self._session.flush()
        return entity

    def get_by_id(self, snapshot_id: int) -> DraftInputSnapshot | None:
        return self._session.get(DraftInputSnapshot, snapshot_id)

    def list_by_article(self, article_id: int) -> list[DraftInputSnapshot]:
        statement = (
            select(DraftInputSnapshot)
            .where(DraftInputSnapshot.article_id == article_id)
            .order_by(*_LATEST_ORDER)
        )
        return list(self._session.scalars(statement).all())

    def get_latest(self, article_id: int) -> DraftInputSnapshot | None:
        statement = (
            select(DraftInputSnapshot)
            .where(DraftInputSnapshot.article_id == article_id)
            .order_by(*_LATEST_ORDER)
            .limit(1)
        )
        return self._session.scalars(statement).first()

    def find_by_article_and_hash(
        self, article_id: int, content_hash: str
    ) -> DraftInputSnapshot | None:
        statement = select(DraftInputSnapshot).where(
            DraftInputSnapshot.article_id == article_id,
            DraftInputSnapshot.content_hash == content_hash,
        )
        return self._session.scalars(statement).first()
