"""保存済みの確認から、記事ごとの索引の状態を読む (C10-A)。**読むだけ・外に問い合わせない。**

- 記事ごとに、**URL Inspection をした** 最新の ``check_indexability`` (週ごとの実行など) の結果を
  使う。日ごとの確認 (URL Inspection をしない) は ``GSC_UNKNOWN`` しか持たないので、索引の
  状態の代わりにしない (C9 までは最新の実行を読んでいたため、ほとんど ``unknown`` だった)。
- 調べた時刻から ``source_policy.json`` の ``url_inspection.stale_after_days`` を過ぎたら
  ``stale`` (古い状態を最新として使わない)。
- 一度も調べていない記事は ``unknown`` (``missing``)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis import sources as src
from app.article.fact_freshness import ensure_aware
from app.models import OperationsStepRun
from app.seo.index_state import IndexObservation, observation_from_row

STEP = "check_indexability"


class IndexStateService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def _steps(self) -> list[OperationsStepRun]:
        return list(self._session.scalars(
            select(OperationsStepRun)
            .where(OperationsStepRun.step_name == STEP, OperationsStepRun.status == "succeeded")
            .order_by(OperationsStepRun.id.desc())))  # fmt: skip

    def latest(self, *, now: datetime | None = None) -> tuple[dict[int, IndexObservation], dict]:
        """記事 → 観測、と実行の情報 (最新の調べた実行・最新の実行)。"""

        now = ensure_aware(now or datetime.now(UTC))
        definition = src.source_definition("url_inspection")
        out: dict[int, IndexObservation] = {}
        site: dict[int, dict] = {}
        latest_run = latest_inspected = None
        for step in self._steps():
            result = step.result_json if isinstance(step.result_json, dict) else {}
            finished = ensure_aware(step.finished_at) if step.finished_at else None
            if finished is not None and finished > now:
                continue  # 観測の時点より後の実行は使わない
            inspected = bool(result.get("inspect"))
            if latest_run is None:
                latest_run = step
            if inspected and latest_inspected is None:
                latest_inspected = step
            state = src.age_state(finished, now=now,
                                  stale_after_days=definition.get("stale_after_days"))
            source = f"operations_step_runs:{step.id} ({'inspect' if inspected else 'no inspect'})"
            for row in result.get("articles") or []:
                if not isinstance(row, dict) or row.get("article_id") is None:
                    continue
                aid = int(row["article_id"])
                site.setdefault(aid, {k: row.get(k) for k in ("live_state", "sitemap_state")})
                current = out.get(aid)
                if current is not None and (current.inspected or not inspected):
                    continue  # 新しい順: 調べた結果を先に取ったら、それを使う
                out[aid] = observation_from_row(
                    row, observed_at=finished.isoformat() if finished else None,
                    data_source=source, inspected=inspected, freshness_state=state)
        from dataclasses import replace

        out = {aid: replace(o, site_checks=site.get(aid, {})) for aid, o in out.items()}
        meta = {"latest_run_id": latest_run.id if latest_run else None,
                "latest_inspected_run_id": latest_inspected.id if latest_inspected else None,
                "latest_inspected_at": (ensure_aware(latest_inspected.finished_at).isoformat()
                                        if latest_inspected and latest_inspected.finished_at
                                        else None)}  # fmt: skip
        return out, meta


__all__ = ["IndexStateService"]
