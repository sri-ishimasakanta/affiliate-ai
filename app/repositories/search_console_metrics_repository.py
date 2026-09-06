"""Search Console 日次メトリクスの永続化アクセス (UPSERT)。

``commit`` は行わず ``flush`` のみ。同一 unique identity の行は更新する
(Search Console の直近データは後日 settle して変化するため)。
``source_import_run_id`` は現在その行の値を占めている最新 run を指す。

移植性のため native UPSERT 構文は使わず、per-row の SELECT → INSERT/UPDATE で行う
(この規模ではメトリクス行数は小さい)。
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SearchConsolePageDaily, SearchConsoleQueryDaily
from app.search_console.rows import SearchConsolePageRow, SearchConsoleQueryRow


class SearchConsoleMetricsRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_page_daily(
        self,
        rows: Iterable[SearchConsolePageRow],
        *,
        property_uri: str,
        source_import_run_id: int,
    ) -> int:
        upserted = 0
        for row in rows:
            existing = self._session.scalars(
                select(SearchConsolePageDaily).where(
                    SearchConsolePageDaily.property_uri == property_uri,
                    SearchConsolePageDaily.metric_date == row.metric_date,
                    SearchConsolePageDaily.page == row.page,
                )
            ).first()
            if existing is None:
                self._session.add(
                    SearchConsolePageDaily(
                        property_uri=property_uri,
                        metric_date=row.metric_date,
                        page=row.page,
                        clicks=row.clicks,
                        impressions=row.impressions,
                        ctr=float(row.ctr),
                        position=float(row.position),
                        source_import_run_id=source_import_run_id,
                    )
                )
            else:
                existing.clicks = row.clicks
                existing.impressions = row.impressions
                existing.ctr = float(row.ctr)
                existing.position = float(row.position)
                existing.source_import_run_id = source_import_run_id
            upserted += 1
        self._session.flush()
        return upserted

    def upsert_query_daily(
        self,
        rows: Iterable[SearchConsoleQueryRow],
        *,
        property_uri: str,
        source_import_run_id: int,
    ) -> int:
        upserted = 0
        for row in rows:
            existing = self._session.scalars(
                select(SearchConsoleQueryDaily).where(
                    SearchConsoleQueryDaily.property_uri == property_uri,
                    SearchConsoleQueryDaily.metric_date == row.metric_date,
                    SearchConsoleQueryDaily.page == row.page,
                    SearchConsoleQueryDaily.query == row.query,
                )
            ).first()
            if existing is None:
                self._session.add(
                    SearchConsoleQueryDaily(
                        property_uri=property_uri,
                        metric_date=row.metric_date,
                        page=row.page,
                        query=row.query,
                        clicks=row.clicks,
                        impressions=row.impressions,
                        ctr=float(row.ctr),
                        position=float(row.position),
                        source_import_run_id=source_import_run_id,
                    )
                )
            else:
                existing.clicks = row.clicks
                existing.impressions = row.impressions
                existing.ctr = float(row.ctr)
                existing.position = float(row.position)
                existing.source_import_run_id = source_import_run_id
            upserted += 1
        self._session.flush()
        return upserted
