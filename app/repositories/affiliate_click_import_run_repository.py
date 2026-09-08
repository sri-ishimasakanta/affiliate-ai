"""AffiliateClickImportRun の永続化アクセス。

``commit`` は行わず ``flush`` のみ。汎用 ``update`` / ``delete`` は持たない。
running 後の変更は狭い lifecycle 遷移メソッド (``mark_succeeded`` / ``mark_failed``)
のみ。``requested_since_id`` / ``requested_limit`` は run 作成後 immutable。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.exceptions import AffiliateClickImportError
from app.models import AffiliateClickImportRun
from app.models.affiliate_click_import_run import (
    ACI_FAILED,
    ACI_RUNNING,
    ACI_SUCCEEDED,
    aci_transition_allowed,
)

_LATEST_ORDER = (
    AffiliateClickImportRun.created_at.desc(),
    AffiliateClickImportRun.id.desc(),
)


class AffiliateClickImportRunRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_running(
        self,
        *,
        requested_since_id: int,
        requested_limit: int,
        started_at: datetime,
    ) -> AffiliateClickImportRun:
        entity = AffiliateClickImportRun(
            status=ACI_RUNNING,
            requested_since_id=requested_since_id,
            requested_limit=requested_limit,
            started_at=started_at,
        )
        self._session.add(entity)
        self._session.flush()
        return entity

    def get_by_id(self, run_id: int) -> AffiliateClickImportRun | None:
        return self._session.get(AffiliateClickImportRun, run_id)

    def latest_succeeded(self) -> AffiliateClickImportRun | None:
        """最後に成功した run。無ければ ``None`` (service 側で cursor 0 に写像する)。"""

        stmt = (
            select(AffiliateClickImportRun)
            .where(AffiliateClickImportRun.status == ACI_SUCCEEDED)
            .order_by(*_LATEST_ORDER)
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    # -- narrow lifecycle transitions ------------------------------------
    def mark_succeeded(
        self,
        run: AffiliateClickImportRun,
        *,
        http_status: int,
        response_count: int,
        response_next_since_id: int,
        inserted_count: int,
        duplicate_count: int,
        unresolved_token_count: int,
        first_source_click_id: int | None,
        last_source_click_id: int | None,
        has_more: bool,
        response_snapshot: dict[str, Any],
        finished_at: datetime,
    ) -> AffiliateClickImportRun:
        self._require_transition(run, ACI_SUCCEEDED)
        run.status = ACI_SUCCEEDED
        run.http_status = http_status
        run.response_count = response_count
        run.response_next_since_id = response_next_since_id
        run.inserted_count = inserted_count
        run.duplicate_count = duplicate_count
        run.unresolved_token_count = unresolved_token_count
        run.first_source_click_id = first_source_click_id
        run.last_source_click_id = last_source_click_id
        run.has_more = has_more
        run.response_snapshot = response_snapshot
        run.error_message = None
        run.finished_at = finished_at
        self._session.flush()
        return run

    def mark_failed(
        self,
        run: AffiliateClickImportRun,
        *,
        error_message: str,
        finished_at: datetime,
        http_status: int | None = None,
        server_code: str | None = None,
    ) -> AffiliateClickImportRun:
        self._require_transition(run, ACI_FAILED)
        run.status = ACI_FAILED
        run.error_message = error_message
        run.finished_at = finished_at
        if http_status is not None:
            run.http_status = http_status
        if server_code is not None:
            run.server_code = server_code
        self._session.flush()
        return run

    @staticmethod
    def _require_transition(run: AffiliateClickImportRun, target: str) -> None:
        if not aci_transition_allowed(run.status, target):
            raise AffiliateClickImportError(
                f"run {run.id}: '{run.status}' -> '{target}' is not allowed"
            )
