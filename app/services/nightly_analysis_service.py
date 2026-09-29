"""夜の分析 (C10-2 / C10-C)。1 日 1 回・約 80 候補 (上限ではなく予算)。

    発見の全体 → 安い・手元の前の絞り込み → 約 80 の分析の候補 → 手元・保存済みの証拠
    → 取り直しの要るものの分類 → Content Intelligence → 重なりの除外 → 次の記事の候補
    → (既存の Growth Action create_new_article と同じ識別) → C9-A の選び方・まとめ

- **PLAN** (既定) は何も書かない。**EXECUTE** は手元の 2 つの表 (``nightly_analysis_runs`` /
  ``content_discovery_candidates``) だけに書く。Keyword・記事・計画の依頼・Growth Action は
  作らない。外に問い合わせない。
- 候補が予算より少なければ少ないまま (埋めない)。多ければ、説明できる順
  (``nightly_analysis_policy.json`` の ``selection_order``) で前から選ぶ。1 つの点数は作らない。
  選んだ理由・後回しにした理由を 1 件ずつ残す。
- 外の取り直しは提供元ごとにまとめる (Google Ads は一括の呼び出し 1 回あたり
  ``max_keywords_per_call`` 件)。夜の分析はそれを呼ばない (人が承認して別に実行する)。
- 冪等: ``run_key`` = ``nightly:<日付 (Asia/Tokyo)>``。その日の実行が成功していれば 2 回目は
  前の結果を返す。失敗・途中で止まった実行はやり直せる (``attempt_count`` が増える)。発見の
  候補の ``seen_count`` は日が変わったときだけ増える。
"""

from __future__ import annotations

import json
import math
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware
from app.content import discovery as dc
from app.content import next_article as na
from app.models.content_discovery import (
    CDC_DISMISSED,
    CDC_PROMOTED,
    CDC_SUPPRESSED,
    CDC_TRACKED,
    NAR_FAILED,
    NAR_RUNNING,
    NAR_SUCCEEDED,
    ContentDiscoveryCandidate,
    NightlyAnalysisRun,
)

POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "nightly_analysis_policy.json"
NIGHTLY_REVISION = "c1d0e233e180"
#: この時間を過ぎた ``running`` の実行は、止まったとみなしてやり直してよい。
STALE_RUNNING_AFTER = timedelta(hours=6)

TIER_EXISTING_GAP = "existing_keyword_gap"
TIER_EXISTING = "existing_keyword"
TIER_DISCOVERY_GAP = "discovery_gap"
TIER_DISCOVERY_SEARCH = "discovery_search_evidence"
TIER_DISCOVERY_OTHER = "discovery_other"


class NightlyAnalysisError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def load_policy(path: Path | str | None = None) -> dict:
    return json.loads(Path(path or POLICY_PATH).read_text(encoding="utf-8"))


def tables_ready(session: Session) -> bool:
    names = set(inspect(session.connection()).get_table_names())
    return {"nightly_analysis_runs", "content_discovery_candidates"} <= names


def schedule_plan(policy: dict, *, project_root: Path | str) -> dict:
    """Windows のタスクの定義 (登録しない。人が確かめて実行する)。"""

    s = policy["schedule"]
    root = Path(project_root).resolve()
    launcher = str(root / s["launcher"]).replace("/", "\\")
    args = ["schtasks", "/Create", "/TN", s["task_name"], "/TR", launcher, "/SC", "WEEKLY",
            "/D", s["days"], "/ST", s["start_time"], "/RL", "LIMITED", "/F"]
    return {"task_name": s["task_name"], "start_time": s["start_time"], "days": s["days"],
            "timezone": s["timezone"], "launcher": launcher, "schtasks_arguments": args,
            "registered": bool(s.get("registered")), "rationale": s.get("rationale"),
            "note": "not registered by this code; registering the task is a human decision"}


class NightlyAnalysisService:
    def __init__(self, session: Session, *, settings=None, policy: dict | None = None,
                 intelligence=None) -> None:  # fmt: skip
        self._session = session
        self._settings = settings
        self._policy = policy or load_policy()
        self._intelligence = intelligence

    def _tz(self):
        from app.operations.policy import get_policy as get_ops_policy

        return get_ops_policy().timezone

    # -- PLAN ---------------------------------------------------------------------------------
    def plan(self, *, now: datetime | None = None, budget: int | None = None) -> dict:
        from app.services.content_intelligence_service import ContentIntelligenceService

        now = ensure_aware(now or datetime.now(UTC))
        budget = int(budget if budget is not None else self._policy.get("budget", 80))
        service = self._intelligence or ContentIntelligenceService(self._session,
                                                                   settings=self._settings)
        ci = service.build(now=now)
        by_identity = {n.identity: n for n in ci.next_articles}
        universe = len(ci.candidates) + sum(1 for d in ci.discovery if not d.is_new)
        skipped = [{"subject": f"discovery:{d.phrase_key}", "phrase": d.phrase,
                    "reason": f"prefilter: {d.duplicate_state}", "duplicate_of": d.duplicate_of}
                   for d in ci.discovery if not d.is_new]  # fmt: skip
        eligible = []
        for row in ci.candidates:
            item = by_identity[row["subject"]]
            tier = self._tier(row, item)
            eligible.append((tier, row, item))
        order = {t: i for i, t in enumerate(self._policy.get("selection_order") or [])}
        priority = {c.cluster_id: (c.priority if c.priority is not None else 99)
                    for c in ci.clusters}
        eligible.sort(key=lambda e: (order.get(e[0], 99),
                                     priority.get(e[2].cluster_id, 100), e[1]["subject"]))
        analyzed, deferred = eligible[:budget], eligible[budget:]
        next_articles = []
        for tier, _row, item in analyzed:
            next_articles.append({**item.as_dict(), "selection": {
                "tier": tier, "reason": self._tier_reason(tier, item)}})
        refresh = self._refresh_plan(ci, [e[2] for e in analyzed])
        counts = {
            "universe": universe, "prefilter_skipped": len(skipped), "eligible": len(eligible),
            "analyzed": len(analyzed), "deferred_by_budget": len(deferred), "budget": budget,
            "by_tier": dict(Counter(t for t, _r, _i in analyzed)),
            "handoff": dict(Counter(i.handoff["readiness"] for _t, _r, i in analyzed)),
            "refresh_required": sum(1 for _t, _r, i in analyzed if i.external_refresh
                                    or i.keyword_id is None),
            "clusters": len(ci.clusters), "gaps": len(ci.gaps),
            "discovery_new": sum(1 for d in ci.discovery if d.is_new),
        }
        return {
            "schema": "nightly-analysis/1", "as_of": now.isoformat(), "counts": counts,
            "budget_semantics": "analysis budget (about 80), not a quota: fewer is fine, "
                                "nothing is padded",
            "next_articles": next_articles,
            "deferred": [{"subject": r["subject"], "tier": t,
                          "reason": "beyond the analysis budget for this run"}
                         for t, r, _i in deferred],
            "skipped": skipped, "refresh_plan": refresh,
            "clusters": [c.as_dict() for c in ci.clusters],
            "gaps": [g.as_dict() for g in ci.gaps],
            "discovery": [d.as_dict() for d in ci.discovery],
            "facts": [f.as_dict() for f in ci.facts],
            "fact_research_plan": ci.as_dict()["fact_research_plan"],
            "sources": ci.sources,
            "external_calls": 0, "creates_articles": False, "creates_keywords": False,
            "composite_score": None,
        }

    @staticmethod
    def _tier(row: dict, item: na.NextArticleCandidate) -> str:
        if item.keyword_id is not None:
            return TIER_EXISTING_GAP if item.gaps_filled else TIER_EXISTING
        if item.gaps_filled:
            return TIER_DISCOVERY_GAP
        if ((row.get("discovery") or {}).get("evidence") or {}).get("gsc_impressions"):
            return TIER_DISCOVERY_SEARCH
        return TIER_DISCOVERY_OTHER

    @staticmethod
    def _tier_reason(tier: str, item: na.NextArticleCandidate) -> str:
        return {TIER_EXISTING_GAP: "existing keyword without an article that fills "
                                   + ", ".join(item.gaps_filled),
                TIER_EXISTING: "existing keyword without an article",
                TIER_DISCOVERY_GAP: "discovered term that fills " + ", ".join(item.gaps_filled),
                TIER_DISCOVERY_SEARCH: "discovered term with Search Console impressions",
                TIER_DISCOVERY_OTHER: "discovered term (config / seed)"}[tier]

    def _refresh_plan(self, ci, analyzed: list[na.NextArticleCandidate]) -> dict:
        """提供元ごとの取り直しの計画 (呼ばない)。"""

        ads = (self._policy.get("refresh") or {}).get("google_ads") or {}
        per_call = int(ads.get("max_keywords_per_call") or 1000)
        existing = list(ci.refresh.get("google_ads", {}).get("keywords_without_stored_metrics")
                        or [])
        discovered = sorted({i.topic for i in analyzed if i.keyword_id is None})
        total = len(existing) + len(discovered)
        return {"google_ads": {
            "existing_keywords_without_stored_metrics": existing,
            "discovered_terms": discovered, "terms": total, "batched": True,
            "max_keywords_per_call": per_call,
            "calls_if_run": math.ceil(total / per_call) if total else 0,
            "how": ads.get("how"), "status": "PENDING HUMAN (not called by the batch)"}}

    # -- EXECUTE ------------------------------------------------------------------------------
    def execute(self, *, now: datetime | None = None, trigger: str = "manual",
                budget: int | None = None) -> dict:
        now = ensure_aware(now or datetime.now(UTC))
        if not tables_ready(self._session):
            raise NightlyAnalysisError(
                f"nightly analysis tables are missing (alembic revision {NIGHTLY_REVISION} "
                "is not applied); nothing was written")
        local = now.astimezone(self._tz()).date()
        key = f"nightly:{local.isoformat()}"
        run = self._session.scalars(select(NightlyAnalysisRun).where(
            NightlyAnalysisRun.run_key == key)).first()  # fmt: skip
        if run is not None and run.status == NAR_SUCCEEDED:
            return {"executed": False, "idempotent_replay": True, "run": _run_dict(run)}
        if run is not None and run.status == NAR_RUNNING and (
                now - ensure_aware(run.started_at) < STALE_RUNNING_AFTER):
            raise NightlyAnalysisError(f"run {key} is still running (started {run.started_at})")
        budget = int(budget if budget is not None else self._policy.get("budget", 80))
        if run is None:
            run = NightlyAnalysisRun(run_key=key, local_date=local, status=NAR_RUNNING,
                                     trigger=trigger, attempt_count=1, budget=budget,
                                     started_at=now)
            self._session.add(run)
        else:
            run.status, run.attempt_count, run.started_at = NAR_RUNNING, run.attempt_count + 1, now
            run.failure_reason, run.finished_at, run.trigger = None, None, trigger
        self._session.commit()
        try:
            plan = self.plan(now=now, budget=budget)
            written = self._upsert_discovery(plan["discovery"], run, now)
            c = plan["counts"]
            run.universe_count, run.eligible_count = c["universe"], c["eligible"]
            run.analyzed_count, run.skipped_count = c["analyzed"], c["prefilter_skipped"]
            run.deferred_count, run.refresh_required_count = (c["deferred_by_budget"],
                                                              c["refresh_required"])
            run.next_article_count = len(plan["next_articles"])
            run.counts_json = {**c, "discovery_written": written}
            run.refresh_plan_json = plan["refresh_plan"]
            run.summary_json = {"next_articles": [
                {"identity": n["identity"], "topic": n["topic"],
                 "readiness": n["handoff"]["readiness"], "tier": n["selection"]["tier"]}
                for n in plan["next_articles"]]}
            run.status, run.finished_at = NAR_SUCCEEDED, datetime.now(UTC)
            self._session.commit()
        except Exception as exc:  # noqa: BLE001 - 失敗は記録して上へ返す (やり直せる)
            self._session.rollback()
            run = self._session.scalars(select(NightlyAnalysisRun).where(
                NightlyAnalysisRun.run_key == key)).first()  # fmt: skip
            run.status, run.finished_at = NAR_FAILED, datetime.now(UTC)
            run.failure_reason = f"{type(exc).__name__}: {exc}"[:1000]
            self._session.commit()
            raise NightlyAnalysisError(f"nightly analysis failed: {exc}") from None
        return {"executed": True, "idempotent_replay": False, "run": _run_dict(run),
                "plan_counts": plan["counts"]}

    def _upsert_discovery(self, discovery: list[dict], run: NightlyAnalysisRun,
                          now: datetime) -> dict:
        written = Counter()
        existing = {r.phrase_key: r for r in self._session.scalars(
            select(ContentDiscoveryCandidate))}
        local = now.astimezone(self._tz()).date()
        for d in discovery:
            status = CDC_TRACKED if d["duplicate_state"] == dc.STATE_NEW else CDC_SUPPRESSED
            keyword_id = (d.get("duplicate_of") or {}).get("keyword_id") if (
                d["duplicate_state"] == dc.STATE_EXISTING_KEYWORD) else None
            row = existing.get(d["phrase_key"])
            if row is None:
                self._session.add(ContentDiscoveryCandidate(
                    phrase_key=d["phrase_key"], phrase=d["phrase"], status=status,
                    duplicate_state=d["duplicate_state"], duplicate_ref_json=d["duplicate_of"],
                    cluster_key=d["cluster_key"], cluster_basis=d["cluster_basis"],
                    sources_json=d["sources"], evidence_json=d["evidence"],
                    refresh_needs_json=list(d["refresh_needs"]), first_seen_at=now,
                    last_seen_at=now, seen_count=1, first_run_id=run.id, last_run_id=run.id,
                    keyword_id=keyword_id, updated_at=now))
                written["created"] += 1
                continue
            if row.status == CDC_DISMISSED:
                written["dismissed_kept"] += 1
                continue  # 人が外したものは戻さない
            if ensure_aware(row.last_seen_at).astimezone(self._tz()).date() < local:
                row.seen_count += 1
                written["seen_again"] += 1
            if row.status == CDC_TRACKED and keyword_id is not None:
                status = CDC_PROMOTED  # 後で人が Keyword にした
            row.status, row.duplicate_state = status, d["duplicate_state"]
            row.duplicate_ref_json, row.cluster_key = d["duplicate_of"], d["cluster_key"]
            row.cluster_basis, row.sources_json = d["cluster_basis"], d["sources"]
            row.evidence_json, row.refresh_needs_json = d["evidence"], list(d["refresh_needs"])
            row.last_seen_at, row.last_run_id, row.updated_at = now, run.id, now
            row.keyword_id = keyword_id or row.keyword_id
            written["updated"] += 1
        return dict(written)

    def history(self, *, limit: int = 14) -> list[dict]:
        if not tables_ready(self._session):
            return []
        return [_run_dict(r) for r in self._session.scalars(
            select(NightlyAnalysisRun).order_by(NightlyAnalysisRun.id.desc()).limit(limit))]


def _run_dict(run: NightlyAnalysisRun) -> dict:
    return {"id": run.id, "run_key": run.run_key, "status": run.status, "trigger": run.trigger,
            "attempt_count": run.attempt_count, "budget": run.budget,
            "universe": run.universe_count, "eligible": run.eligible_count,
            "analyzed": run.analyzed_count, "skipped": run.skipped_count,
            "deferred": run.deferred_count, "refresh_required": run.refresh_required_count,
            "next_articles": run.next_article_count, "failure_reason": run.failure_reason,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None}


__all__ = ["NIGHTLY_REVISION", "NightlyAnalysisError", "NightlyAnalysisService",
           "load_policy", "schedule_plan", "tables_ready"]
