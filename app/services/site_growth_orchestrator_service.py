"""Site Growth Orchestrator の今の状態 (C10-3 / C10-E)。**読むだけ・外に問い合わせない。**

生きている Growth Action ごとに、今の段 (レビュー待ち → 承認 → 引き渡し → 先の承認 → 実行 →
観測) と次の一歩を出す。既存のサービスを置き換えない (状態を読むだけ)。行動ごとの本番の状態は
``app/growth/orchestration.py`` の ``ACTION_MATRIX``。
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware
from app.growth import orchestration as orc


class SiteGrowthOrchestratorService:
    def __init__(self, session: Session, *, settings=None) -> None:
        self._session = session
        self._settings = settings

    def status(self, *, now: datetime | None = None) -> dict:
        from app.models.growth_action import (
            GrowthActionCandidate,
            GrowthActionConversion,
            GrowthActionReview,
        )
        from app.services.growth_action_service import history_tables_ready
        from app.services.growth_measurement_service import GrowthMeasurementService

        now = ensure_aware(now or datetime.now(UTC))
        matrix = {k: v.as_dict() for k, v in orc.ACTION_MATRIX.items()}
        if not history_tables_ready(self._session):
            return {"schema": orc.SCHEMA, "matrix": matrix, "actions": [], "stages": {}}
        reviews = {r.candidate_id: r for r in self._session.scalars(select(GrowthActionReview))}
        conversions = {c.candidate_id: c for c in self._session.scalars(
            select(GrowthActionConversion))}
        anchors = {}
        for item in GrowthMeasurementService(self._session, settings=self._settings).anchors():
            anchors.setdefault(item["anchor"]["growth_action_id"], []).append(item)
        rows = []
        for c in self._session.scalars(select(GrowthActionCandidate).where(
                GrowthActionCandidate.status.in_(("active", "pending_review", "approved",
                                                  "converted")))
                .order_by(GrowthActionCandidate.id)):
            review = reviews.get(c.id)
            items = anchors.get(c.id, [])
            effective = next((i["lifecycle"].effective_at for i in items
                              if i["lifecycle"].effective_at), None)
            stage = orc.stage_of(candidate_status=c.status,
                                 review_status=review.status if review else None,
                                 converted=c.id in conversions, effective_at=effective,
                                 downstream_open=bool(items))
            rows.append({"id": c.id, "action_type": c.action_type, "subject_id": c.subject_id,
                         "status": c.status, "availability": c.availability, "stage": stage,
                         "production": orc.ACTION_MATRIX[c.action_type].production
                         if c.action_type in orc.ACTION_MATRIX else None,
                         "next_step": orc.next_step(c.action_type, stage),
                         "waiting_for": [i["lifecycle"].waiting_for for i in items
                                         if i["lifecycle"].waiting_for]})
        self._session.rollback()
        return {"schema": orc.SCHEMA, "as_of": now.isoformat(), "matrix": matrix,
                "actions": rows,
                "stages": dict(sorted(Counter(r["stage"] for r in rows).items())),
                "by_action": dict(sorted(Counter(r["action_type"] for r in rows).items())),
                "notes": ["approvals stay in each workflow; a Growth approval never approves a "
                          "downstream request", "no external call"]}


__all__ = ["SiteGrowthOrchestratorService"]
