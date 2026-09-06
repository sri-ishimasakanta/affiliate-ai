"""WordPressPublicationRun の永続化アクセス。

``commit`` は行わず ``flush`` のみ。汎用 ``update`` / ``delete`` は持たない。
prepare 後の変更は狭い lifecycle 遷移メソッドのみ
(prepared -> running -> succeeded/failed)。各メソッドは
:func:`wp_publication_run_transition_allowed` で妥当性を検証してから mutate する。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.exceptions import WordPressPublicationRunExecutionError
from app.models import WordPressPublicationRun
from app.models.wordpress_publication_run import (
    WP_PUBRUN_ACTIVE_STATUSES,
    WP_PUBRUN_FAILED,
    WP_PUBRUN_PREPARED,
    WP_PUBRUN_RUNNING,
    WP_PUBRUN_SUCCEEDED,
    wp_publication_run_transition_allowed,
)

_LATEST_ORDER = (
    WordPressPublicationRun.created_at.desc(),
    WordPressPublicationRun.id.desc(),
)

_IDENTITY_FIELDS = (
    "article_id",
    "source_wordpress_draft_run_id",
    "wordpress_post_id",
    "publish_payload_hash",
    "publication_request_identity_hash",
    "target_publication_request_identity_hash",
)


class WordPressPublicationRunRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_prepared(self, **fields) -> WordPressPublicationRun:
        entity = WordPressPublicationRun(status=WP_PUBRUN_PREPARED, **fields)
        self._session.add(entity)
        self._session.flush()
        return entity

    def get_by_id(self, run_id: int) -> WordPressPublicationRun | None:
        return self._session.get(WordPressPublicationRun, run_id)

    def list_by_article(self, article_id: int) -> list[WordPressPublicationRun]:
        stmt = (
            select(WordPressPublicationRun)
            .where(WordPressPublicationRun.article_id == article_id)
            .order_by(*_LATEST_ORDER)
        )
        return list(self._session.scalars(stmt).all())

    def get_by_idempotency_key(self, key: str) -> WordPressPublicationRun | None:
        stmt = select(WordPressPublicationRun).where(
            WordPressPublicationRun.idempotency_key == key
        )
        return self._session.scalars(stmt).first()

    def find_active_by_publication_identity(
        self, article_id: int, target_publication_request_identity_hash: str
    ) -> WordPressPublicationRun | None:
        stmt = select(WordPressPublicationRun).where(
            WordPressPublicationRun.article_id == article_id,
            WordPressPublicationRun.target_publication_request_identity_hash
            == target_publication_request_identity_hash,
            WordPressPublicationRun.status.in_(tuple(WP_PUBRUN_ACTIVE_STATUSES)),
        )
        return self._session.scalars(stmt).first()

    def succeeded_exists_for_article(self, article_id: int) -> bool:
        stmt = (
            select(func.count())
            .select_from(WordPressPublicationRun)
            .where(
                WordPressPublicationRun.article_id == article_id,
                WordPressPublicationRun.status == WP_PUBRUN_SUCCEEDED,
            )
        )
        return bool(self._session.scalar(stmt))

    @staticmethod
    def identity_of(run: WordPressPublicationRun) -> dict:
        return {f: getattr(run, f) for f in _IDENTITY_FIELDS}

    # -- narrow lifecycle transitions (execute) ----------------------------
    def mark_running(
        self, run: WordPressPublicationRun, *, started_at: datetime
    ) -> WordPressPublicationRun:
        self._require_transition(run, WP_PUBRUN_RUNNING)
        run.status = WP_PUBRUN_RUNNING
        run.started_at = started_at
        self._session.flush()
        return run

    def mark_succeeded(
        self,
        run: WordPressPublicationRun,
        *,
        wordpress_post_status: str,
        wordpress_post_url: str,
        published_at_source: str,
        response_snapshot: dict[str, Any],
        finished_at: datetime,
    ) -> WordPressPublicationRun:
        self._require_transition(run, WP_PUBRUN_SUCCEEDED)
        run.status = WP_PUBRUN_SUCCEEDED
        run.wordpress_post_status = wordpress_post_status
        run.wordpress_post_url = wordpress_post_url
        run.published_at_source = published_at_source
        run.response_snapshot = response_snapshot
        run.finished_at = finished_at
        run.error_message = None
        self._session.flush()
        return run

    def mark_failed(
        self, run: WordPressPublicationRun, *, error_message: str, finished_at: datetime
    ) -> WordPressPublicationRun:
        self._require_transition(run, WP_PUBRUN_FAILED)
        run.status = WP_PUBRUN_FAILED
        run.error_message = error_message
        run.finished_at = finished_at
        self._session.flush()
        return run

    @staticmethod
    def _require_transition(run: WordPressPublicationRun, target: str) -> None:
        if not wp_publication_run_transition_allowed(run.status, target):
            raise WordPressPublicationRunExecutionError(
                f"run {run.id}: '{run.status}' -> '{target}' is not allowed"
            )
