"""WordPressDraftRun の永続化アクセス。

``commit`` は行わず ``flush`` のみ。汎用 ``update`` / ``delete`` は持たない。
prepare 後の変更は狭い lifecycle 遷移だけ (このフェーズでは prepare のみ実装)。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import WordPressDraftRun
from app.models.wordpress_draft_run import WP_RUN_ACTIVE_STATUSES, WP_RUN_PREPARED

_LATEST_ORDER = (WordPressDraftRun.created_at.desc(), WordPressDraftRun.id.desc())

_IDENTITY_FIELDS = (
    "article_id",
    "source_promotion_id",
    "target_request_identity_hash",
    "request_identity_hash",
    "payload_hash",
)


class WordPressDraftRunRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_prepared(self, **fields) -> WordPressDraftRun:
        entity = WordPressDraftRun(status=WP_RUN_PREPARED, **fields)
        self._session.add(entity)
        self._session.flush()
        return entity

    def get_by_id(self, run_id: int) -> WordPressDraftRun | None:
        return self._session.get(WordPressDraftRun, run_id)

    def list_by_article(self, article_id: int) -> list[WordPressDraftRun]:
        stmt = (
            select(WordPressDraftRun)
            .where(WordPressDraftRun.article_id == article_id)
            .order_by(*_LATEST_ORDER)
        )
        return list(self._session.scalars(stmt).all())

    def get_by_idempotency_key(self, key: str) -> WordPressDraftRun | None:
        stmt = select(WordPressDraftRun).where(
            WordPressDraftRun.idempotency_key == key
        )
        return self._session.scalars(stmt).first()

    def find_active_by_target_identity(
        self, article_id: int, target_request_identity_hash: str
    ) -> WordPressDraftRun | None:
        stmt = select(WordPressDraftRun).where(
            WordPressDraftRun.article_id == article_id,
            WordPressDraftRun.target_request_identity_hash
            == target_request_identity_hash,
            WordPressDraftRun.status.in_(tuple(WP_RUN_ACTIVE_STATUSES)),
        )
        return self._session.scalars(stmt).first()

    @staticmethod
    def identity_of(run: WordPressDraftRun) -> dict:
        return {f: getattr(run, f) for f in _IDENTITY_FIELDS}
