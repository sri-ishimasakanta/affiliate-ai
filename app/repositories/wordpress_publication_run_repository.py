"""WordPressPublicationRun の永続化アクセス。

``commit`` は行わず ``flush`` のみ。汎用 ``update`` / ``delete`` は持たない。
このフェーズは **prepare のみ** — lifecycle mutation メソッドは持たない。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import WordPressPublicationRun
from app.models.wordpress_publication_run import (
    WP_PUBRUN_ACTIVE_STATUSES,
    WP_PUBRUN_PREPARED,
    WP_PUBRUN_SUCCEEDED,
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
