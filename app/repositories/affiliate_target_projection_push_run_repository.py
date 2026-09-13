"""AffiliateTargetProjectionPushRun の永続化アクセス。

``commit`` は行わず ``flush`` のみ (commit は service が transaction 境界を制御する —
D-D0.3 の Transaction A / Transaction B 契約)。汎用 ``update`` は持たない。running
作成後の変更は狭い terminal 遷移メソッド (``mark_succeeded`` / ``mark_failed`` /
``mark_outcome_unknown``) のみ。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.exceptions import AffiliateProjectionPushError
from app.models import AffiliateTargetProjectionPushRun
from app.models.affiliate_target_projection_push_run import (
    ATPP_FAILED,
    ATPP_OUTCOME_UNKNOWN,
    ATPP_RUNNING,
    ATPP_SUCCEEDED,
    atpp_transition_allowed,
)

_LATEST_ORDER = (
    AffiliateTargetProjectionPushRun.created_at.desc(),
    AffiliateTargetProjectionPushRun.id.desc(),
)


class AffiliateTargetProjectionPushRunRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_running(
        self,
        *,
        snapshot_scope: str,
        runtime_origin: str,
        requested_snapshot_hash: str,
        requested_target_count: int,
        request_manifest_json: str,
        started_at: datetime,
    ) -> AffiliateTargetProjectionPushRun:
        entity = AffiliateTargetProjectionPushRun(
            status=ATPP_RUNNING,
            snapshot_scope=snapshot_scope,
            runtime_origin=runtime_origin,
            requested_snapshot_hash=requested_snapshot_hash,
            requested_target_count=requested_target_count,
            request_manifest_json=request_manifest_json,
            started_at=started_at,
        )
        self._session.add(entity)
        self._session.flush()
        return entity

    def get_by_id(self, run_id: int) -> AffiliateTargetProjectionPushRun | None:
        return self._session.get(AffiliateTargetProjectionPushRun, run_id)

    def latest_for_origin(
        self, runtime_origin: str, *, limit: int = 50
    ) -> list[AffiliateTargetProjectionPushRun]:
        """1 runtime_origin の run を新しい順に返す (acknowledgement resolver 用)。"""

        stmt = (
            select(AffiliateTargetProjectionPushRun)
            .where(AffiliateTargetProjectionPushRun.runtime_origin == runtime_origin)
            .order_by(*_LATEST_ORDER)
            .limit(limit)
        )
        return list(self._session.scalars(stmt).all())

    def latest_succeeded_for_origin(
        self, runtime_origin: str
    ) -> AffiliateTargetProjectionPushRun | None:
        stmt = (
            select(AffiliateTargetProjectionPushRun)
            .where(
                AffiliateTargetProjectionPushRun.runtime_origin == runtime_origin,
                AffiliateTargetProjectionPushRun.status == ATPP_SUCCEEDED,
            )
            .order_by(*_LATEST_ORDER)
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    # -- narrow terminal transitions -------------------------------------
    def mark_succeeded(
        self,
        run: AffiliateTargetProjectionPushRun,
        *,
        http_status: int,
        response_projection_snapshot_hash: str,
        received_count: int,
        inserted_count: int,
        updated_count: int,
        unchanged_count: int,
        finished_at: datetime,
    ) -> AffiliateTargetProjectionPushRun:
        self._require_transition(run, ATPP_SUCCEEDED)
        run.status = ATPP_SUCCEEDED
        run.http_status = http_status
        run.response_projection_snapshot_hash = response_projection_snapshot_hash
        run.received_count = received_count
        run.inserted_count = inserted_count
        run.updated_count = updated_count
        run.unchanged_count = unchanged_count
        run.error_message = None
        run.finished_at = finished_at
        self._session.flush()
        return run

    def mark_failed(
        self,
        run: AffiliateTargetProjectionPushRun,
        *,
        error_message: str,
        finished_at: datetime,
        http_status: int | None = None,
        server_code: str | None = None,
    ) -> AffiliateTargetProjectionPushRun:
        self._require_transition(run, ATPP_FAILED)
        run.status = ATPP_FAILED
        run.error_message = error_message
        run.finished_at = finished_at
        run.http_status = http_status
        run.server_code = server_code
        self._session.flush()
        return run

    def mark_outcome_unknown(
        self,
        run: AffiliateTargetProjectionPushRun,
        *,
        error_message: str,
        finished_at: datetime,
        http_status: int | None = None,
        server_code: str | None = None,
    ) -> AffiliateTargetProjectionPushRun:
        self._require_transition(run, ATPP_OUTCOME_UNKNOWN)
        run.status = ATPP_OUTCOME_UNKNOWN
        run.error_message = error_message
        run.finished_at = finished_at
        run.http_status = http_status
        run.server_code = server_code
        self._session.flush()
        return run

    @staticmethod
    def _require_transition(
        run: AffiliateTargetProjectionPushRun, target: str
    ) -> None:
        if not atpp_transition_allowed(run.status, target):
            raise AffiliateProjectionPushError(
                f"run {run.id}: '{run.status}' -> '{target}' is not allowed"
            )
