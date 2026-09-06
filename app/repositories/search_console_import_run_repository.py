"""SearchConsoleImportRun の永続化アクセス。

``commit`` は行わず ``flush`` のみ。汎用 ``update`` / ``delete`` は持たない。
prepare 後の変更は狭い lifecycle 遷移メソッドのみ。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.exceptions import SearchConsoleImportStateError
from app.models import SearchConsoleImportRun
from app.models.search_console_import_run import (
    SC_IMPORT_ACTIVE_STATUSES,
    SC_IMPORT_CANCELLED,
    SC_IMPORT_FAILED,
    SC_IMPORT_PREPARED,
    SC_IMPORT_RUNNING,
    SC_IMPORT_SUCCEEDED,
    sc_import_transition_allowed,
)

_LATEST_ORDER = (
    SearchConsoleImportRun.created_at.desc(),
    SearchConsoleImportRun.id.desc(),
)

_IDENTITY_FIELDS = (
    "property_uri",
    "start_date",
    "end_date",
    "import_identity_hash",
)


class SearchConsoleImportRunRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_prepared(self, **fields) -> SearchConsoleImportRun:
        entity = SearchConsoleImportRun(status=SC_IMPORT_PREPARED, **fields)
        self._session.add(entity)
        self._session.flush()
        return entity

    def get_by_id(self, run_id: int) -> SearchConsoleImportRun | None:
        return self._session.get(SearchConsoleImportRun, run_id)

    def get_by_idempotency_key(self, key: str) -> SearchConsoleImportRun | None:
        stmt = select(SearchConsoleImportRun).where(
            SearchConsoleImportRun.idempotency_key == key
        )
        return self._session.scalars(stmt).first()

    def find_active_by_identity(
        self, import_identity_hash: str
    ) -> SearchConsoleImportRun | None:
        stmt = select(SearchConsoleImportRun).where(
            SearchConsoleImportRun.import_identity_hash == import_identity_hash,
            SearchConsoleImportRun.status.in_(tuple(SC_IMPORT_ACTIVE_STATUSES)),
        )
        return self._session.scalars(stmt).first()

    def list_recent(self, limit: int = 50) -> list[SearchConsoleImportRun]:
        stmt = select(SearchConsoleImportRun).order_by(*_LATEST_ORDER).limit(limit)
        return list(self._session.scalars(stmt).all())

    # -- narrow lifecycle transitions --------------------------------------
    def mark_running(
        self, run: SearchConsoleImportRun, *, started_at: datetime
    ) -> SearchConsoleImportRun:
        self._require_transition(run, SC_IMPORT_RUNNING)
        run.status = SC_IMPORT_RUNNING
        run.started_at = started_at
        self._session.flush()
        return run

    def mark_succeeded(
        self,
        run: SearchConsoleImportRun,
        *,
        page_rows_received: int,
        query_rows_received: int,
        page_rows_upserted: int,
        query_rows_upserted: int,
        response_snapshot: dict[str, Any],
        finished_at: datetime,
    ) -> SearchConsoleImportRun:
        self._require_transition(run, SC_IMPORT_SUCCEEDED)
        run.status = SC_IMPORT_SUCCEEDED
        run.page_rows_received = page_rows_received
        run.query_rows_received = query_rows_received
        run.page_rows_upserted = page_rows_upserted
        run.query_rows_upserted = query_rows_upserted
        run.response_snapshot = response_snapshot
        run.finished_at = finished_at
        run.error_message = None
        self._session.flush()
        return run

    def mark_failed(
        self, run: SearchConsoleImportRun, *, error_message: str, finished_at: datetime
    ) -> SearchConsoleImportRun:
        self._require_transition(run, SC_IMPORT_FAILED)
        run.status = SC_IMPORT_FAILED
        run.error_message = error_message
        run.finished_at = finished_at
        self._session.flush()
        return run

    def mark_cancelled(
        self, run: SearchConsoleImportRun, *, finished_at: datetime
    ) -> SearchConsoleImportRun:
        self._require_transition(run, SC_IMPORT_CANCELLED)
        run.status = SC_IMPORT_CANCELLED
        run.finished_at = finished_at
        self._session.flush()
        return run

    @staticmethod
    def identity_of(run: SearchConsoleImportRun) -> dict:
        return {f: getattr(run, f) for f in _IDENTITY_FIELDS}

    @staticmethod
    def _require_transition(run: SearchConsoleImportRun, target: str) -> None:
        if not sc_import_transition_allowed(run.status, target):
            raise SearchConsoleImportStateError(
                f"run {run.id}: '{run.status}' -> '{target}' is not allowed"
            )
