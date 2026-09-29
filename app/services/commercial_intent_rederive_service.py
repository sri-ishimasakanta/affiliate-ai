"""CommercialIntentRederiveService — 保存済みの Google Ads の値から導き直す (C10-A)。

PLAN / EXECUTE の 2 モード (``AffiliateSignalTransitionService`` と同じ規約)。

- 入力は **保存済みの** Google Ads の値だけ (最新の commercial_intent の ``raw_data`` にある
  low/high の入札・competition・competition_index)。**Google Ads を呼ばない。**
- :meth:`plan` は SELECT だけ。新しい値は ``calculate_commercial_intent`` (V2) で計算するだけ。
- :meth:`execute` は期待値 (導き直す数・再スコアの数) を書き込み前に確かめ、違えば何も書かずに
  断る。新しい行の ``observed_at`` は元の Google Ads の観測の時刻のまま (観測は新しくなって
  いない)。履歴は追記だけ (前の行は消さない)。
- 再スコアは、既に score があり・値が変わり・7 component がそろう keyword だけ。
- 冪等: 最新が既に V2 で同じ値なら ``current`` (2 回目は何も書かない)。
- 保存済みの値が無い keyword は ``no_stored_metrics`` (外からの取り直しが要る。ここでは呼ばない)。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.keyword.normalizers.commercial_intent import (
    NORMALIZER_NAME,
    NORMALIZER_VERSION,
    calculate_commercial_intent,
)
from app.keyword.scoring import COMPONENT_NAMES
from app.models import Keyword, KeywordScore
from app.repositories.keyword_signal_repository import KeywordSignalRepository

COMPONENT = "commercial_intent"
PROVIDER = "google_ads"
VALUE_EPSILON = 1e-9

DECISION_REDERIVE = "rederive"
DECISION_CURRENT = "current"
DECISION_NO_METRICS = "no_stored_metrics"


class RederiveRefusedError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class KeywordRederive:
    keyword_id: int
    keyword: str
    decision: str
    current_value: float | None = None
    current_version: str | None = None
    new_value: float | None = None
    market_evidence_state: str | None = None
    quality_flags: tuple[str, ...] = ()
    scored_value: float | None = None
    rescore: bool = False
    rescore_blocked: tuple[str, ...] = ()

    @property
    def value_changed(self) -> bool:
        return (self.new_value is not None and self.current_value is not None
                and abs(self.new_value - self.current_value) > VALUE_EPSILON)

    def as_dict(self) -> dict:
        return {"keyword_id": self.keyword_id, "keyword": self.keyword,
                "decision": self.decision, "current_value": self.current_value,
                "current_version": self.current_version, "new_value": self.new_value,
                "value_changed": self.value_changed,
                "market_evidence_state": self.market_evidence_state,
                "quality_flags": list(self.quality_flags), "scored_value": self.scored_value,
                "rescore": self.rescore, "rescore_blocked": list(self.rescore_blocked)}


@dataclass
class RederivePlan:
    items: list[KeywordRederive] = field(default_factory=list)

    @property
    def rederive(self) -> list[KeywordRederive]:
        return [i for i in self.items if i.decision == DECISION_REDERIVE]

    @property
    def rescore(self) -> list[KeywordRederive]:
        return [i for i in self.items if i.rescore]

    def counts(self) -> dict:
        from collections import Counter

        return {"keywords": len(self.items),
                "by_decision": dict(sorted(Counter(i.decision for i in self.items).items())),
                "value_changes": sum(1 for i in self.rederive if i.value_changed),
                "expected_signal_inserts": len(self.rederive),
                "expected_rescores": len(self.rescore),
                "market_evidence": dict(sorted(Counter(
                    i.market_evidence_state for i in self.rederive).items()))}

    def as_dict(self) -> dict:
        return {"normalizer": {"name": NORMALIZER_NAME, "version": NORMALIZER_VERSION},
                "counts": self.counts(), "items": [i.as_dict() for i in self.items],
                "external_calls": 0}


class CommercialIntentRederiveService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._signals = KeywordSignalRepository(session)

    def plan(self) -> RederivePlan:
        plan = RederivePlan()
        for keyword in self._session.scalars(select(Keyword).order_by(Keyword.id)):
            plan.items.append(self._evaluate(keyword))
        self._session.rollback()
        return plan

    def _evaluate(self, keyword: Keyword) -> KeywordRederive:
        latest = self._signals.get_latest(keyword.id, COMPONENT)
        raw = dict(latest.raw_data or {}) if latest is not None else {}
        stored = "low_top_of_page_bid_micros" in raw
        if latest is None or latest.provider != PROVIDER or not stored:
            return KeywordRederive(keyword.id, keyword.keyword, DECISION_NO_METRICS,
                                   current_value=latest.normalized_value if latest else None)
        result = calculate_commercial_intent(
            keyword=keyword.keyword,
            low_top_of_page_bid_micros=raw.get("low_top_of_page_bid_micros"),
            competition_index=raw.get("competition_index"),
            competition=raw.get("competition"),
            high_top_of_page_bid_micros=raw.get("high_top_of_page_bid_micros"))
        version = (raw.get("normalizer") or {}).get("version") or raw.get("normalizer_version")
        current = version == NORMALIZER_VERSION and abs(
            result.score - latest.normalized_value) <= VALUE_EPSILON
        score = self._session.scalars(select(KeywordScore).where(
            KeywordScore.keyword_id == keyword.id).order_by(KeywordScore.id.desc())).first()
        scored = score.commercial_intent if score is not None else None
        blocked: tuple[str, ...] = ()
        rescore = False
        if (not current and score is not None and scored is not None
                and abs(result.score - scored) > VALUE_EPSILON):
            blocked = tuple(c for c in COMPONENT_NAMES if c != COMPONENT
                            and self._signals.get_latest(keyword.id, c) is None)
            rescore = not blocked
        return KeywordRederive(
            keyword.id, keyword.keyword, DECISION_CURRENT if current else DECISION_REDERIVE,
            current_value=latest.normalized_value, current_version=version,
            new_value=result.score, market_evidence_state=result.market_evidence_state,
            quality_flags=result.quality_flags, scored_value=scored, rescore=rescore,
            rescore_blocked=blocked)

    def execute(self, *, expect_rederive: int, expect_rescore: int) -> dict:
        from app.services.keyword_scoring_service import KeywordScoringService

        plan = self.plan()
        counts = plan.counts()
        if (counts["expected_signal_inserts"], counts["expected_rescores"]) != (
                expect_rederive, expect_rescore):
            raise RederiveRefusedError(
                f"plan has {counts['expected_signal_inserts']} rederive(s) and "
                f"{counts['expected_rescores']} rescore(s); expected {expect_rederive} / "
                f"{expect_rescore}; nothing was written")
        if not plan.rederive:
            return {"executed": False, "reason": "already_current", "counts": counts}
        created = []
        for item in plan.rederive:
            latest = self._signals.get_latest(item.keyword_id, COMPONENT)
            keyword = self._session.get(Keyword, item.keyword_id)
            raw = dict(latest.raw_data or {})
            result = calculate_commercial_intent(
                keyword=keyword.keyword,
                low_top_of_page_bid_micros=raw.get("low_top_of_page_bid_micros"),
                competition_index=raw.get("competition_index"),
                competition=raw.get("competition"),
                high_top_of_page_bid_micros=raw.get("high_top_of_page_bid_micros"))
            if abs(result.score - item.new_value) > VALUE_EPSILON:
                self._session.rollback()
                raise RederiveRefusedError(f"keyword {item.keyword_id} changed after the plan")
            new_raw = {**raw,
                       "cpc_score": result.cpc_score,
                       "ad_competition_score": result.ad_competition_score,
                       "available_weight": result.available_weight,
                       "evidence_coverage": result.evidence_coverage,
                       "market_evidence_available": result.market_evidence_available,
                       "market_evidence_state": result.market_evidence_state,
                       "quality_flags": list(result.quality_flags),
                       "normalizer_version": result.normalizer_version,
                       "normalizer": {"name": result.normalizer_name,
                                      "version": result.normalizer_version},
                       "rederived_from_signal_id": latest.id,
                       "rederived_without_external_call": True}  # fmt: skip
            entity = self._signals.create(
                keyword_id=item.keyword_id, component=COMPONENT, normalized_value=result.score,
                provider=latest.provider, observed_at=latest.observed_at, raw_data=new_raw,
                source_reference=latest.source_reference, period_start=latest.period_start,
                period_end=latest.period_end)
            created.append(entity.id)
        self._session.commit()
        rescored = []
        scoring = KeywordScoringService(self._session)
        for item in plan.rescore:
            rescored.append(scoring.score_keyword_from_latest_signals(item.keyword_id).id)
        self._session.commit()
        return {"executed": True, "signals_created": created, "scores_created": rescored,
                "counts": counts, "external_calls": 0}


__all__ = ["CommercialIntentRederiveService", "RederivePlan", "RederiveRefusedError"]
