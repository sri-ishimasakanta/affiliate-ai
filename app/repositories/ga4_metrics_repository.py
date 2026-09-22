"""Ga4PageDaily の永続化アクセス (UPSERT)。

``commit`` は行わず ``flush`` のみ。同じ ``(property, date, page_path,
channel_scope)`` は 1 行だけで、再取り込みは上書きになる
(:class:`SearchConsoleMetricsRepository` と同じ refresh セマンティクス)。
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.rows import Ga4PageRow
from app.models import Ga4PageDaily


class Ga4MetricsRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_page_daily(
        self,
        rows: Iterable[Ga4PageRow],
        *,
        property_id: str,
        source_import_run_id: int,
    ) -> int:
        upserted = 0
        for row in rows:
            existing = self._session.scalars(
                select(Ga4PageDaily).where(
                    Ga4PageDaily.property_id == property_id,
                    Ga4PageDaily.metric_date == row.metric_date,
                    Ga4PageDaily.page_path == row.page_path,
                    Ga4PageDaily.channel_scope == row.channel_scope,
                )
            ).first()
            if existing is None:
                self._session.add(
                    Ga4PageDaily(
                        property_id=property_id,
                        metric_date=row.metric_date,
                        page_path=row.page_path,
                        channel_scope=row.channel_scope,
                        sessions=row.sessions,
                        active_users=row.active_users,
                        new_users=row.new_users,
                        engaged_sessions=row.engaged_sessions,
                        engagement_rate=float(row.engagement_rate),
                        average_engagement_time_seconds=float(row.average_engagement_time_seconds),
                        screen_page_views=row.screen_page_views,
                        source_import_run_id=source_import_run_id,
                    )
                )
            else:
                existing.sessions = row.sessions
                existing.active_users = row.active_users
                existing.new_users = row.new_users
                existing.engaged_sessions = row.engaged_sessions
                existing.engagement_rate = float(row.engagement_rate)
                existing.average_engagement_time_seconds = float(
                    row.average_engagement_time_seconds
                )
                existing.screen_page_views = row.screen_page_views
                existing.source_import_run_id = source_import_run_id
            upserted += 1
        self._session.flush()
        return upserted
