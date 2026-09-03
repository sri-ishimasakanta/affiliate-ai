"""DraftGenerationRun の永続化アクセス。

``commit`` は行わず ``flush`` のみ。汎用 ``update`` / ``delete`` は持たない。
prepare 後の変更は狭い lifecycle 遷移 (``mark_running`` 等) だけ。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DraftGenerationRun
from app.models.draft_generation_run import (
    RUN_CANCELLED,
    RUN_FAILED,
    RUN_PREPARED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    RUN_TERMINAL_STATUSES,
)

_LATEST_ORDER = (
    DraftGenerationRun.created_at.desc(),
    DraftGenerationRun.id.desc(),
)

# 「execution identity」の同一性判定に使うフィールド (§38)。
_IDENTITY_FIELDS = (
    "article_id",
    "snapshot_id",
    "prompt_input_hash",
    "rendered_prompt_hash",
    "execution_mode",
    "provider",
    "model",
)


class DraftGenerationRunRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def append(self, **fields) -> DraftGenerationRun:
        entity = DraftGenerationRun(status=RUN_PREPARED, **fields)
        self._session.add(entity)
        self._session.flush()
        return entity

    # -- read ----------------------------------------------------------
    def get_by_id(self, run_id: int) -> DraftGenerationRun | None:
        return self._session.get(DraftGenerationRun, run_id)

    def list_by_article(self, article_id: int) -> list[DraftGenerationRun]:
        stmt = (
            select(DraftGenerationRun)
            .where(DraftGenerationRun.article_id == article_id)
            .order_by(*_LATEST_ORDER)
        )
        return list(self._session.scalars(stmt).all())

    def get_latest(self, article_id: int) -> DraftGenerationRun | None:
        stmt = (
            select(DraftGenerationRun)
            .where(DraftGenerationRun.article_id == article_id)
            .order_by(*_LATEST_ORDER)
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    def find_by_idempotency_key(self, key: str) -> DraftGenerationRun | None:
        stmt = select(DraftGenerationRun).where(
            DraftGenerationRun.idempotency_key == key
        )
        return self._session.scalars(stmt).first()

    def find_running_by_article(self, article_id: int) -> DraftGenerationRun | None:
        stmt = select(DraftGenerationRun).where(
            DraftGenerationRun.article_id == article_id,
            DraftGenerationRun.status == RUN_RUNNING,
        )
        return self._session.scalars(stmt).first()

    def find_non_terminal_by_identity(self, identity: dict) -> DraftGenerationRun | None:
        """同一 execution identity の prepared / running run (あれば)。"""

        conds = [
            getattr(DraftGenerationRun, f) == identity[f] for f in _IDENTITY_FIELDS
        ]
        stmt = select(DraftGenerationRun).where(
            *conds,
            DraftGenerationRun.status.notin_(tuple(RUN_TERMINAL_STATUSES)),
        )
        return self._session.scalars(stmt).first()

    @staticmethod
    def identity_of(run: DraftGenerationRun) -> dict:
        return {f: getattr(run, f) for f in _IDENTITY_FIELDS}

    # -- narrow lifecycle transitions --------------------------------
    def mark_running(self, run: DraftGenerationRun, *, started_at: datetime) -> None:
        run.status = RUN_RUNNING
        run.started_at = started_at
        self._session.flush()

    def mark_succeeded(
        self,
        run: DraftGenerationRun,
        *,
        raw_output: str,
        parsed_body: str,
        parsed_meta_description: str,
        generation_notes: list[str],
        validation_report: dict,
        token_usage: dict | None,
        finished_at: datetime,
    ) -> None:
        run.status = RUN_SUCCEEDED
        run.raw_output = raw_output
        run.parsed_body = parsed_body
        run.parsed_meta_description = parsed_meta_description
        run.generation_notes = generation_notes
        run.validation_report = validation_report
        run.token_usage = token_usage
        run.finished_at = finished_at
        self._session.flush()

    def mark_failed(
        self,
        run: DraftGenerationRun,
        *,
        error_message: str,
        finished_at: datetime,
        raw_output: str | None = None,
    ) -> None:
        run.status = RUN_FAILED
        run.error_message = error_message
        run.raw_output = raw_output
        run.finished_at = finished_at
        self._session.flush()

    def mark_cancelled(
        self, run: DraftGenerationRun, *, finished_at: datetime, reason: str | None = None
    ) -> None:
        run.status = RUN_CANCELLED
        if reason:
            run.error_message = reason
        run.finished_at = finished_at
        self._session.flush()
