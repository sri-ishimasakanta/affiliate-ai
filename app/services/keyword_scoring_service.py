"""Keyword Opportunity Score のビジネスロジック。

- Keyword 存在確認
- 純粋関数 (:func:`app.keyword.scoring.calculate_opportunity_score`) でスコア計算
- KeywordScore 履歴の追加 + Keyword.opportunity_score キャッシュ更新 +
  status 自動遷移 (discovered -> analyzed のみ) を **1 トランザクション** で行う
- signals から計算する場合は使用した KeywordSignal を KeywordScoreSignal で紐付ける
- 失敗時は rollback

DB アクセスは Repository に委譲する。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.exceptions import EntityNotFoundError, IncompleteSignalSetError
from app.keyword.schemas import KeywordScoreCreate, KeywordScoreRead, KeywordSignalRead
from app.keyword.scoring import (
    COMPONENT_NAMES,
    OpportunityScoreInput,
    calculate_opportunity_score,
)
from app.models import Keyword, KeywordScore, KeywordSignal
from app.models.enums import KeywordStatus
from app.repositories.keyword_repository import KeywordRepository
from app.repositories.keyword_score_repository import KeywordScoreRepository
from app.repositories.keyword_score_signal_repository import KeywordScoreSignalRepository
from app.repositories.keyword_signal_repository import KeywordSignalRepository
from app.services.keyword_signal_service import to_signal_read

_KEYWORD_ENTITY = "Keyword"
_SCORE_ENTITY = "KeywordScore"


class KeywordScoringService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._keywords = KeywordRepository(session)
        self._scores = KeywordScoreRepository(session)
        self._signals = KeywordSignalRepository(session)
        self._score_signals = KeywordScoreSignalRepository(session)

    # -- write --------------------------------------------------------------
    def score_keyword(
        self,
        keyword_id: int,
        payload: KeywordScoreCreate,
    ) -> KeywordScoreRead:
        keyword = self._keywords.get_by_id(keyword_id)
        if keyword is None:
            raise EntityNotFoundError(_KEYWORD_ENTITY, keyword_id)

        component_values = {
            "search_demand": payload.search_demand,
            "commercial_intent": payload.commercial_intent,
            "affiliate_opportunity": payload.affiliate_opportunity,
            "competition_ease": payload.competition_ease,
            "trend": payload.trend,
            "originality": payload.originality,
            "site_relevance": payload.site_relevance,
        }
        result = calculate_opportunity_score(OpportunityScoreInput(**component_values))

        try:
            entity = self._scores.create(
                keyword_id=keyword_id,
                total_score=result.total,
                score_version=result.version,
                input_source=payload.input_source,
                **component_values,
            )
            self._apply_cache_and_status(keyword, result.total)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        self._session.refresh(entity)
        return self._to_read(entity)

    def score_keyword_from_latest_signals(self, keyword_id: int) -> KeywordScoreRead:
        """7 component それぞれの最新 KeywordSignal から Opportunity Score を作成する。

        1 つでも不足していれば :class:`IncompleteSignalSetError` を送出し、
        KeywordScore は作成しない。全体を 1 トランザクションとして扱う。
        """

        keyword = self._keywords.get_by_id(keyword_id)
        if keyword is None:
            raise EntityNotFoundError(_KEYWORD_ENTITY, keyword_id)

        latest: dict[str, KeywordSignal] = {}
        missing: list[str] = []
        for component in COMPONENT_NAMES:
            signal = self._signals.get_latest(keyword_id, component)
            if signal is None:
                missing.append(component)
            else:
                latest[component] = signal
        if missing:
            raise IncompleteSignalSetError(keyword_id, missing)

        component_values = {
            name: latest[name].normalized_value for name in COMPONENT_NAMES
        }
        result = calculate_opportunity_score(OpportunityScoreInput(**component_values))

        try:
            entity = self._scores.create(
                keyword_id=keyword_id,
                total_score=result.total,
                score_version=result.version,
                input_source="signals",
                **component_values,
            )
            self._apply_cache_and_status(keyword, result.total)
            for name in COMPONENT_NAMES:
                self._score_signals.create(
                    keyword_score_id=entity.id,
                    keyword_signal_id=latest[name].id,
                )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        self._session.refresh(entity)
        return self._to_read(entity)

    # -- read ---------------------------------------------------------------
    def get_latest_score(self, keyword_id: int) -> KeywordScoreRead:
        self._ensure_keyword_exists(keyword_id)
        entity = self._scores.get_latest(keyword_id)
        if entity is None:
            raise EntityNotFoundError(_SCORE_ENTITY, keyword_id)
        return self._to_read(entity)

    def list_score_history(
        self,
        keyword_id: int,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[KeywordScoreRead]:
        self._ensure_keyword_exists(keyword_id)
        return [
            self._to_read(entity)
            for entity in self._scores.list_by_keyword(
                keyword_id, limit=limit, offset=offset
            )
        ]

    def list_score_signals(
        self,
        keyword_id: int,
        score_id: int,
    ) -> list[KeywordSignalRead]:
        """指定スコアの provenance (使用した Signal 一覧) を返す。"""

        self._ensure_keyword_exists(keyword_id)
        score = self._scores.get_by_id(score_id)
        if score is None or score.keyword_id != keyword_id:
            raise EntityNotFoundError(_SCORE_ENTITY, score_id)
        return [
            to_signal_read(signal)
            for signal in self._score_signals.list_signals_for_score(score_id)
        ]

    # -- helpers ----------------------------------------------------------
    def _apply_cache_and_status(self, keyword: Keyword, total: float) -> None:
        updates: dict[str, object] = {"opportunity_score": total}
        # discovered のときのみ analyzed へ。再スコアリングでは status を変えない。
        # score によって selected / rejected へ自動遷移することはない。
        if KeywordStatus(keyword.status) is KeywordStatus.DISCOVERED:
            updates["status"] = KeywordStatus.ANALYZED
        self._keywords.update(keyword, updates)

    def _ensure_keyword_exists(self, keyword_id: int) -> None:
        if self._keywords.get_by_id(keyword_id) is None:
            raise EntityNotFoundError(_KEYWORD_ENTITY, keyword_id)

    @staticmethod
    def _to_read(entity: KeywordScore) -> KeywordScoreRead:
        return KeywordScoreRead(
            id=entity.id,
            keyword_id=entity.keyword_id,
            search_demand=entity.search_demand,
            commercial_intent=entity.commercial_intent,
            affiliate_opportunity=entity.affiliate_opportunity,
            competition_ease=entity.competition_ease,
            trend=entity.trend,
            originality=entity.originality,
            site_relevance=entity.site_relevance,
            total_score=entity.total_score,
            score_version=entity.score_version,
            input_source=entity.input_source,
            created_at=entity.created_at,
        )
