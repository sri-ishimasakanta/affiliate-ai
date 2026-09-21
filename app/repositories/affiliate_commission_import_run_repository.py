"""AffiliateCommissionImportRun の永続化アクセス。

``commit`` は行わず ``flush`` のみ。汎用 ``update`` / ``delete`` は持たない。
running 後の変更は狭い lifecycle 遷移メソッド (``mark_succeeded`` / ``mark_failed``)
のみ。``provider`` / ``affiliate_program_id`` / ``requested_date_from`` /
``requested_date_to`` は run 作成後 immutable。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.exceptions import AffiliateCommissionImportError
from app.models import AffiliateCommissionImportRun
from app.models.affiliate_commission_import_run import (
    ACIR_FAILED,
    ACIR_RUNNING,
    ACIR_SUCCEEDED,
    acir_transition_allowed,
)


class AffiliateCommissionImportRunRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_running(
        self,
        *,
        provider: str,
        affiliate_program_id: int,
        requested_date_from: date | None,
        requested_date_to: date | None,
        started_at: datetime,
    ) -> AffiliateCommissionImportRun:
        entity = AffiliateCommissionImportRun(
            status=ACIR_RUNNING,
            provider=provider,
            affiliate_program_id=affiliate_program_id,
            requested_date_from=requested_date_from,
            requested_date_to=requested_date_to,
            started_at=started_at,
        )
        self._session.add(entity)
        self._session.flush()
        return entity

    def get_by_id(self, run_id: int) -> AffiliateCommissionImportRun | None:
        return self._session.get(AffiliateCommissionImportRun, run_id)

    def latest_succeeded(
        self, *, provider: str, affiliate_program_id: int
    ) -> AffiliateCommissionImportRun | None:
        stmt = (
            select(AffiliateCommissionImportRun)
            .where(
                AffiliateCommissionImportRun.provider == provider,
                AffiliateCommissionImportRun.affiliate_program_id
                == affiliate_program_id,
                AffiliateCommissionImportRun.status == ACIR_SUCCEEDED,
            )
            .order_by(
                AffiliateCommissionImportRun.created_at.desc(),
                AffiliateCommissionImportRun.id.desc(),
            )
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    # -- narrow lifecycle transitions ------------------------------------
    def mark_succeeded(
        self,
        run: AffiliateCommissionImportRun,
        *,
        http_status: int,
        page_count: int,
        response_count: int,
        inserted_count: int,
        updated_count: int,
        unchanged_count: int,
        response_snapshot: dict[str, Any],
        finished_at: datetime,
    ) -> AffiliateCommissionImportRun:
        self._require_transition(run, ACIR_SUCCEEDED)
        run.status = ACIR_SUCCEEDED
        run.http_status = http_status
        run.page_count = page_count
        run.response_count = response_count
        run.inserted_count = inserted_count
        run.updated_count = updated_count
        run.unchanged_count = unchanged_count
        run.response_snapshot = response_snapshot
        run.error_message = None
        run.finished_at = finished_at
        self._session.flush()
        return run

    def mark_failed(
        self,
        run: AffiliateCommissionImportRun,
        *,
        error_message: str,
        finished_at: datetime,
        http_status: int | None = None,
    ) -> AffiliateCommissionImportRun:
        self._require_transition(run, ACIR_FAILED)
        run.status = ACIR_FAILED
        run.error_message = error_message
        run.finished_at = finished_at
        if http_status is not None:
            run.http_status = http_status
        self._session.flush()
        return run

    @staticmethod
    def _require_transition(run: AffiliateCommissionImportRun, target: str) -> None:
        if not acir_transition_allowed(run.status, target):
            raise AffiliateCommissionImportError(
                f"run {run.id}: '{run.status}' -> '{target}' is not allowed"
            )
