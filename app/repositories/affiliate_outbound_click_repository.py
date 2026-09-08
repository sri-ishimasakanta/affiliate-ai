"""AffiliateOutboundClick の永続化アクセス (append-only replica)。

``commit`` は行わず ``flush`` のみ。``update`` / ``delete`` / upsert は持たない
— source (WordPress) が唯一の source of truth であり、既存行は決して書き換えない
(同一なら no-op、内容が違えば service が source drift として page 全体を rollback する)。
``clicked_at`` は呼び出し側が :func:`app.article.fact_freshness.to_storage_utc` で
naive UTC wall-clock へ正規化してから渡す。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import TypedDict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AffiliateOutboundClick


class NewOutboundClick(TypedDict):
    source_click_id: int
    token: str
    clicked_at: datetime  # naive UTC wall-clock (to_storage_utc 済み)
    source_import_run_id: int


class AffiliateOutboundClickRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_source_id(
        self, source_click_id: int
    ) -> AffiliateOutboundClick | None:
        return self._session.scalars(
            select(AffiliateOutboundClick).where(
                AffiliateOutboundClick.source_click_id == source_click_id
            )
        ).first()

    def get_map_by_source_ids(
        self, source_click_ids: Sequence[int]
    ) -> dict[int, AffiliateOutboundClick]:
        """指定 source_click_id 群の既存行を ``{source_click_id: row}`` で返す。"""

        if not source_click_ids:
            return {}
        rows = self._session.scalars(
            select(AffiliateOutboundClick).where(
                AffiliateOutboundClick.source_click_id.in_(tuple(source_click_ids))
            )
        ).all()
        return {row.source_click_id: row for row in rows}

    def insert_many(self, rows: Iterable[NewOutboundClick]) -> int:
        """新規 click 行を追加する (既存判定は呼び出し側の責務)。追加件数を返す。"""

        count = 0
        for row in rows:
            self._session.add(
                AffiliateOutboundClick(
                    source_click_id=row["source_click_id"],
                    token=row["token"],
                    clicked_at=row["clicked_at"],
                    source_import_run_id=row["source_import_run_id"],
                )
            )
            count += 1
        self._session.flush()
        return count

    def max_source_click_id(self) -> int | None:
        """replica が保持する最大 source_click_id。空なら ``None``。"""

        return self._session.scalar(
            select(func.max(AffiliateOutboundClick.source_click_id))
        )

    def count_all(self) -> int:
        return self._session.scalar(
            select(func.count()).select_from(AffiliateOutboundClick)
        )
