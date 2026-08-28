"""KeywordScore (Opportunity Score 履歴) の永続化アクセス。

責務は SQLAlchemy ``Session`` を用いた DB アクセスのみ。
スコア計算・status 変更・存在チェック等のビジネスロジックは持たない。
``commit`` は行わず ``flush`` のみ。トランザクション境界は Service 側。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import KeywordScore


class KeywordScoreRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        keyword_id: int,
        search_demand: float,
        commercial_intent: float,
        affiliate_opportunity: float,
        competition_ease: float,
        trend: float,
        originality: float,
        site_relevance: float,
        total_score: float,
        score_version: str,
        input_source: str,
    ) -> KeywordScore:
        entity = KeywordScore(
            keyword_id=keyword_id,
            search_demand=search_demand,
            commercial_intent=commercial_intent,
            affiliate_opportunity=affiliate_opportunity,
            competition_ease=competition_ease,
            trend=trend,
            originality=originality,
            site_relevance=site_relevance,
            total_score=total_score,
            score_version=score_version,
            input_source=input_source,
        )
        self._session.add(entity)
        self._session.flush()
        return entity

    def get_by_id(self, score_id: int) -> KeywordScore | None:
        return self._session.get(KeywordScore, score_id)

    def get_latest(self, keyword_id: int) -> KeywordScore | None:
        statement = (
            select(KeywordScore)
            .where(KeywordScore.keyword_id == keyword_id)
            .order_by(KeywordScore.id.desc())
            .limit(1)
        )
        return self._session.scalars(statement).first()

    def list_by_keyword(
        self,
        keyword_id: int,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[KeywordScore]:
        statement = (
            select(KeywordScore)
            .where(KeywordScore.keyword_id == keyword_id)
            .order_by(KeywordScore.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(statement).all())
