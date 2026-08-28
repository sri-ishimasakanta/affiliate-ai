"""KeywordScoreSignal (score -> signal の provenance) の永続化アクセス。

責務は DB アクセスのみ。``commit`` は行わず ``flush`` のみ。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import KeywordScoreSignal, KeywordSignal


class KeywordScoreSignalRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        keyword_score_id: int,
        keyword_signal_id: int,
    ) -> KeywordScoreSignal:
        entity = KeywordScoreSignal(
            keyword_score_id=keyword_score_id,
            keyword_signal_id=keyword_signal_id,
        )
        self._session.add(entity)
        self._session.flush()
        return entity

    def list_signals_for_score(self, keyword_score_id: int) -> list[KeywordSignal]:
        """指定スコアに紐づく KeywordSignal を新しい順で返す。"""

        statement = (
            select(KeywordSignal)
            .join(
                KeywordScoreSignal,
                KeywordScoreSignal.keyword_signal_id == KeywordSignal.id,
            )
            .where(KeywordScoreSignal.keyword_score_id == keyword_score_id)
            .order_by(KeywordSignal.id.desc())
        )
        return list(self._session.scalars(statement).all())
