"""DraftInputSnapshot の preview / freeze オーケストレーション。

- :meth:`preview` は完全 read-only (build するだけ)。
- :meth:`freeze` が **transaction owner**:
  build → gate 検証 → expected_content_hash 照合 → duplicate lookup → append → commit
  を 1 transaction で行う。途中失敗は full rollback。nested commit は作らない。
- 同一 (article_id, content_hash) の再 freeze は新しい行を作らず既存を返す
  (service prelookup + DB UNIQUE を最終防御)。
- Article.status は変更しない (freeze != drafting 開始)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.article.fact_freshness import to_storage_utc
from app.article.schemas import (
    DraftInputFreezeResponse,
    DraftInputGateStatus,
    DraftInputPreviewRead,
    DraftInputSnapshotRead,
    DraftInputSnapshotSummaryRead,
)
from app.exceptions import (
    DraftInputNotReadyError,
    EntityNotFoundError,
    SnapshotInputChangedError,
)
from app.models import DraftInputSnapshot
from app.repositories.article_repository import ArticleRepository
from app.repositories.draft_input_snapshot_repository import (
    DraftInputSnapshotRepository,
)
from app.services.draft_input_snapshot_builder import DraftInputSnapshotBuilder

_ENTITY = "DraftInputSnapshot"


class DraftInputSnapshotService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._articles = ArticleRepository(session)
        self._repo = DraftInputSnapshotRepository(session)
        self._builder = DraftInputSnapshotBuilder(session)

    # -- read -------------------------------------------------------
    def preview(
        self, article_id: int, *, now: datetime | None = None
    ) -> DraftInputPreviewRead:
        result = self._builder.build(article_id, now=now)
        return DraftInputPreviewRead(
            article_id=result.article_id,
            snapshot_version=result.snapshot_version,
            builder_version=result.builder_version,
            content_hash=result.content_hash,
            payload=result.payload,
            readiness=result.readiness,
            gate_status=DraftInputGateStatus(**result.gate_status),
        )

    def list_for_article(
        self, article_id: int
    ) -> list[DraftInputSnapshotSummaryRead]:
        self._ensure_article(article_id)
        return [
            self._to_summary(row) for row in self._repo.list_by_article(article_id)
        ]

    def get(self, article_id: int, snapshot_id: int) -> DraftInputSnapshotRead:
        self._ensure_article(article_id)
        row = self._repo.get_by_id(snapshot_id)
        if row is None or row.article_id != article_id:
            raise EntityNotFoundError(_ENTITY, snapshot_id)
        return self._to_read(row)

    # -- write (transaction owner) --------------------------------
    def freeze(
        self,
        article_id: int,
        expected_content_hash: str,
        *,
        now: datetime | None = None,
    ) -> DraftInputFreezeResponse:
        now = now or datetime.now(UTC)
        result = self._builder.build(article_id, now=now)

        if not result.can_freeze:
            raise DraftInputNotReadyError(
                "; ".join(result.gate_status["failed_gates"])
            )

        if result.content_hash != expected_content_hash:
            raise SnapshotInputChangedError(
                expected_content_hash, result.content_hash
            )

        existing = self._repo.find_by_article_and_hash(
            article_id, result.content_hash
        )
        if existing is not None:
            return DraftInputFreezeResponse(
                snapshot=self._to_read(existing), already_frozen=True
            )

        try:
            entity = self._repo.append(
                article_id=article_id,
                snapshot_version=result.snapshot_version,
                builder_version=result.builder_version,
                plan_snapshot_origin=result.plan_snapshot_origin,
                primary_affiliate_program_id=result.primary_affiliate_program_id,
                comparison_program_ids=result.comparison_program_ids,
                drafting_allowed_at_freeze=result.drafting_allowed_at_freeze,
                payload=result.payload,
                content_hash=result.content_hash,
                frozen_at=to_storage_utc(now),
            )
            self._session.commit()
        except IntegrityError:
            # 競合で UNIQUE(article_id, content_hash) に当たったら idempotent 扱い。
            self._session.rollback()
            existing = self._repo.find_by_article_and_hash(
                article_id, result.content_hash
            )
            if existing is None:
                raise
            return DraftInputFreezeResponse(
                snapshot=self._to_read(existing), already_frozen=True
            )
        except Exception:
            self._session.rollback()
            raise

        self._session.refresh(entity)
        return DraftInputFreezeResponse(
            snapshot=self._to_read(entity), already_frozen=False
        )

    # -- helpers --------------------------------------------------
    def _ensure_article(self, article_id: int) -> None:
        if self._articles.get_by_id(article_id) is None:
            raise EntityNotFoundError("Article", article_id)

    @staticmethod
    def _to_summary(row: DraftInputSnapshot) -> DraftInputSnapshotSummaryRead:
        return DraftInputSnapshotSummaryRead(
            id=row.id,
            article_id=row.article_id,
            snapshot_version=row.snapshot_version,
            builder_version=row.builder_version,
            plan_snapshot_origin=row.plan_snapshot_origin,
            content_hash=row.content_hash,
            primary_affiliate_program_id=row.primary_affiliate_program_id,
            comparison_program_ids=list(row.comparison_program_ids or []),
            drafting_allowed_at_freeze=row.drafting_allowed_at_freeze,
            frozen_at=row.frozen_at,
            created_at=row.created_at,
        )

    @classmethod
    def _to_read(cls, row: DraftInputSnapshot) -> DraftInputSnapshotRead:
        return DraftInputSnapshotRead(
            **cls._to_summary(row).model_dump(),
            payload=row.payload,
        )
