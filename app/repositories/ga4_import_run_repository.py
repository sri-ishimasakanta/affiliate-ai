"""Ga4ImportRun の永続化アクセス。

``commit`` は行わず ``flush`` のみ (transaction 境界は import service が持つ)。
汎用 ``update`` / ``delete`` は持たず、狭い lifecycle 遷移メソッドのみを公開する。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.exceptions import Ga4ImportStateError
from app.models import Ga4ImportRun
from app.models.ga4_import_run import (
    GA4_IMPORT_ACTIVE_STATUSES,
    GA4_IMPORT_FAILED,
    GA4_IMPORT_PREPARED,
    GA4_IMPORT_RUNNING,
    GA4_IMPORT_SUCCEEDED,
    ga4_import_transition_allowed,
)

_LATEST_ORDER = (Ga4ImportRun.created_at.desc(), Ga4ImportRun.id.desc())


class Ga4ImportRunRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, run_id: int) -> Ga4ImportRun | None:
        return self._session.get(Ga4ImportRun, run_id)

    def get_by_idempotency_key(self, key: str) -> Ga4ImportRun | None:
        stmt = select(Ga4ImportRun).where(Ga4ImportRun.idempotency_key == key)
        return self._session.scalars(stmt).first()

    def find_active_by_identity(self, identity_hash: str) -> Ga4ImportRun | None:
        stmt = (
            select(Ga4ImportRun)
            .where(
                Ga4ImportRun.import_identity_hash == identity_hash,
                Ga4ImportRun.status.in_(tuple(GA4_IMPORT_ACTIVE_STATUSES)),
            )
            .order_by(*_LATEST_ORDER)
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    def latest_succeeded(self, property_id: str) -> Ga4ImportRun | None:
        stmt = (
            select(Ga4ImportRun)
            .where(
                Ga4ImportRun.property_id == property_id,
                Ga4ImportRun.status == GA4_IMPORT_SUCCEEDED,
            )
            .order_by(*_LATEST_ORDER)
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    @staticmethod
    def identity_of(run: Ga4ImportRun) -> dict[str, Any]:
        return {
            "property_id": run.property_id,
            "start_date": run.start_date,
            "end_date": run.end_date,
            "import_identity_hash": run.import_identity_hash,
        }

    def add_prepared(
        self,
        *,
        property_id: str,
        start_date: date,
        end_date: date,
        request_json: str,
        import_identity_hash: str,
        idempotency_key: str | None,
        created_at: datetime,
    ) -> Ga4ImportRun:
        run = Ga4ImportRun(
            property_id=property_id,
            start_date=start_date,
            end_date=end_date,
            status=GA4_IMPORT_PREPARED,
            request_json=request_json,
            import_identity_hash=import_identity_hash,
            idempotency_key=idempotency_key,
            created_at=created_at,
        )
        self._session.add(run)
        self._session.flush()
        return run

    def mark_running(self, run: Ga4ImportRun, *, started_at: datetime) -> Ga4ImportRun:
        self._require_transition(run, GA4_IMPORT_RUNNING)
        run.status = GA4_IMPORT_RUNNING
        run.started_at = started_at
        self._session.flush()
        return run

    def mark_succeeded(
        self,
        run: Ga4ImportRun,
        *,
        page_rows_received: int,
        page_rows_upserted: int,
        data_through_date: date | None,
        property_timezone: str | None,
        response_snapshot: dict[str, Any] | None,
        finished_at: datetime,
    ) -> Ga4ImportRun:
        self._require_transition(run, GA4_IMPORT_SUCCEEDED)
        run.status = GA4_IMPORT_SUCCEEDED
        run.page_rows_received = page_rows_received
        run.page_rows_upserted = page_rows_upserted
        run.data_through_date = data_through_date
        run.property_timezone = property_timezone
        run.response_snapshot = response_snapshot
        run.error_message = None
        run.finished_at = finished_at
        self._session.flush()
        return run

    def mark_failed(
        self, run: Ga4ImportRun, *, error_message: str, finished_at: datetime
    ) -> Ga4ImportRun:
        self._require_transition(run, GA4_IMPORT_FAILED)
        run.status = GA4_IMPORT_FAILED
        run.error_message = error_message
        run.finished_at = finished_at
        self._session.flush()
        return run

    @staticmethod
    def _require_transition(run: Ga4ImportRun, target: str) -> None:
        if not ga4_import_transition_allowed(run.status, target):
            raise Ga4ImportStateError(f"run {run.id}: '{run.status}' -> '{target}' is not allowed")
