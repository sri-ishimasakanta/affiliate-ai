"""WordPressContentUpdateReconciliation の永続化アクセス。

``commit`` は行わず ``flush`` のみ (transaction 境界は service が持つ)。
行は append-only -- 一度書いた verdict を書き換える手段は提供しない。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import WordPressContentUpdateReconciliation
from app.models.wordpress_content_update_reconciliation import (
    WP_CU_RECONCILIATION_VERDICTS,
)


class WordPressContentUpdateReconciliationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_for_run(self, run_id: int) -> WordPressContentUpdateReconciliation | None:
        stmt = select(WordPressContentUpdateReconciliation).where(
            WordPressContentUpdateReconciliation.wordpress_content_update_run_id == run_id
        )
        return self._session.scalars(stmt).first()

    def list_by_article(self, article_id: int) -> list[WordPressContentUpdateReconciliation]:
        stmt = (
            select(WordPressContentUpdateReconciliation)
            .where(WordPressContentUpdateReconciliation.article_id == article_id)
            .order_by(
                WordPressContentUpdateReconciliation.created_at.desc(),
                WordPressContentUpdateReconciliation.id.desc(),
            )
        )
        return list(self._session.scalars(stmt).all())

    def add(
        self,
        *,
        wordpress_content_update_run_id: int,
        article_id: int,
        wordpress_post_id: str,
        verdict: str,
        reason: str,
        pre_update_wordpress_raw_content_hash: str,
        request_content_hash: str,
        comparison_json: dict[str, Any],
        unresolved_reason_code: str | None = None,
        observed_wordpress_status: str | None = None,
        observed_wordpress_raw_content_hash: str | None = None,
        observed_modified_gmt_raw: str | None = None,
        idempotency_key: str | None = None,
    ) -> WordPressContentUpdateReconciliation:
        if verdict not in WP_CU_RECONCILIATION_VERDICTS:
            raise ValueError(f"unsupported reconciliation verdict: {verdict!r}")
        row = WordPressContentUpdateReconciliation(
            wordpress_content_update_run_id=wordpress_content_update_run_id,
            article_id=article_id,
            wordpress_post_id=wordpress_post_id,
            verdict=verdict,
            unresolved_reason_code=unresolved_reason_code,
            reason=reason,
            observed_wordpress_status=observed_wordpress_status,
            observed_wordpress_raw_content_hash=observed_wordpress_raw_content_hash,
            observed_modified_gmt_raw=observed_modified_gmt_raw,
            pre_update_wordpress_raw_content_hash=pre_update_wordpress_raw_content_hash,
            request_content_hash=request_content_hash,
            comparison_json=comparison_json,
            idempotency_key=idempotency_key,
        )
        self._session.add(row)
        self._session.flush()
        return row
