"""Growth Action の履歴・受け箱・人のレビュー (C9 Batch 2)。

- ``collect_coverage``: 既存の仕事 (開いている Threads の提案・最近の通常の投稿・在庫の保守の
  計画・Growth の枠・開いている変更の依頼・C9 の開いているレビュー) を **読むだけ** で集める。
- ``GrowthActionHistory.refresh``: 評価の結果を、候補の版の履歴に照らす。既定は **PLAN**
  (何も書かない)。``execute=True`` で書くのは C9 の 3 つの表 (``growth_action_*``) だけ。
- ``GrowthActionReviewService``: 人のレビュー (依頼・承認・却下・見送り)。**承認は次の段階へ進めて
  よいという許可だけ** で、WordPress・Threads・公開・アフィリエイトの設定・外の API に触れない。
  レビューに見せた内容 (版の指紋・証拠の指紋・根拠・止める理由) を固定し、承認の時に照合する
  (版が古い・観測されなくなった・指紋が違う → 承認しない)。
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware
from app.growth import inbox as gi
from app.growth.analysis import IDENTITY_SCHEMA
from app.growth.conversion import plan_conversion
from app.models import (
    CR_APPROVED,
    CR_OPEN_STATUSES,
    PUB_PUBLISHED,
    TP_APPROVED,
    TP_OPEN_STATES,
    ChangeRequest,
    ThreadsPostProposal,
    ThreadsPublication,
)
from app.models.growth_action import (
    GA_ACTIVE,
    GA_APPROVED,
    GA_CONVERTED,
    GA_DISMISSED,
    GA_OBSERVED,
    GA_PENDING_REVIEW,
    GA_REJECTED,
    GA_SUPERSEDED,
    GAE_APPROVED,
    GAE_AVAILABILITY_CHANGED,
    GAE_DISMISSED,
    GAE_NOT_OBSERVED,
    GAE_OBSERVED,
    GAE_REJECTED,
    GAE_REVIEW_REQUESTED,
    GAE_REVIEW_STALE,
    GAE_SUPERSEDED,
    GAR_APPROVED,
    GAR_PENDING,
    GAR_REJECTED,
    GAR_STALE,
    GrowthActionCandidate,
    GrowthActionEvent,
    GrowthActionReview,
    growth_action_transition_allowed,
)

REVIEW_SCHEMA = "growth-action-review/1"
HISTORY_TABLES = ("growth_action_candidates", "growth_action_reviews", "growth_action_events")
REQUIRED_REVISION = "74bfaf6c9c9f"
#: 観測されなくなった機会の availability。
NOT_OBSERVED = "not_observed"

# -- 照らし合わせの結果 --------------------------------------------------------------------------
NEW = "new"
NEW_REVISION = "new_revision"
REOBSERVED = "reobserved"  # 完全に同じ候補 (知らせない)
SUPPRESSED_REJECTED = "suppressed_rejected"
SUPPRESSED_COMPLETED = "suppressed_completed"
SUPPRESSED_DISMISSED = "suppressed_dismissed"
SUPPRESSED_SUPERSEDED = "suppressed_superseded"
REOBSERVED_PENDING = "reobserved_pending_review"


class GrowthActionError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _sha(payload) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=str)  # fmt: skip
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _iso(value) -> str | None:
    return ensure_aware(value).isoformat() if value is not None else None


def history_tables_ready(session: Session) -> bool:
    names = set(inspect(session.connection()).get_table_names())
    return all(t in names for t in HISTORY_TABLES)


# == 既存の仕事 (読むだけ) ==========================================


def collect_coverage(session: Session, *, now: datetime, report: dict, settings=None,
                     include_stock_plan: bool = True) -> gi.CoverageContext:  # fmt: skip
    now = ensure_aware(now)
    published = {pid for pid in session.scalars(select(ThreadsPublication.proposal_id)) if pid}
    open_props: dict[int, list[dict]] = defaultdict(list)
    for row in session.scalars(
        select(ThreadsPostProposal).where(
            ThreadsPostProposal.source_article_id.is_not(None),
            ThreadsPostProposal.status.in_((*TP_OPEN_STATES, TP_APPROVED)))
        .order_by(ThreadsPostProposal.id)
    ):  # fmt: skip
        if row.id in published:
            continue
        open_props[row.source_article_id].append(
            {"id": row.id, "status": row.status, "angle": row.angle})
    latest: dict[int, float] = {}
    for row in session.scalars(select(ThreadsPublication).where(
            ThreadsPublication.status == PUB_PUBLISHED,
            ThreadsPublication.source_article_id.is_not(None))):  # fmt: skip
        if row.published_at is None:
            continue
        hours = (now - ensure_aware(row.published_at)).total_seconds() / 3600
        aid = row.source_article_id
        latest[aid] = min(hours, latest.get(aid, hours))
    change_requests: dict[int, list[int]] = defaultdict(list)
    for row in session.scalars(select(ChangeRequest).where(
            ChangeRequest.status.in_((*CR_OPEN_STATUSES, CR_APPROVED)))):  # fmt: skip
        change_requests[row.article_id].append(row.id)
    open_reviews = {}
    if history_tables_ready(session):
        for review in session.scalars(select(GrowthActionReview).where(
                GrowthActionReview.status == GAR_PENDING)):  # fmt: skip
            open_reviews[review.opportunity_key] = review.id
    growth_plan = report.get("growth_plan") or {}
    return gi.CoverageContext(
        open_regular_proposals={k: tuple(v) for k, v in open_props.items()},
        latest_regular_post_hours=latest,
        stock_planned=_stock_planned(session, now, settings) if include_stock_plan else {},
        growth_lane_automatic=True,
        growth_proposal_today=growth_plan.get("active_proposal"),
        open_change_requests={k: tuple(sorted(v)) for k, v in change_requests.items()},
        open_reviews=open_reviews,
    )


def _stock_planned(session: Session, now: datetime, settings) -> dict[int, tuple[str, ...]]:
    """在庫の保守が次に依頼する記事と切り口 (読むだけ。provider は使わない)。"""

    try:
        from app.services.threads_generation_provider import DisabledAutomatedProvider
        from app.services.threads_proposal_stock_service import ThreadsProposalStockService
        from app.social.threads.policy import get_operations_policy
        from app.social.threads.stock import plan_stock

        service = ThreadsProposalStockService(
            session, settings=settings, provider=DisabledAutomatedProvider(None))
        policy = get_operations_policy()
        plan = plan_stock(service.facts(now=now), policy)
    except Exception:  # noqa: BLE001 - 計画が読めなくても受け箱は止めない (覆いが減るだけ)
        return {}
    out: dict[int, list[str]] = defaultdict(list)
    for request in plan.requests:
        out[request.article_id].append(request.angle)
    return {k: tuple(v) for k, v in out.items()}


# == 履歴 ==========================================


@dataclass
class RefreshItem:
    candidate: dict
    decision: str
    availability: str
    reasons: list[str]
    revision: int
    existing_id: int | None = None
    supersedes_id: int | None = None
    stale_review_id: int | None = None

    @property
    def surfaced(self) -> bool:
        """人に新しく知らせる価値があるか (新しい機会・新しい証拠で、いま動ける)。"""

        return self.decision in (NEW, NEW_REVISION) and self.availability == gi.ACTIONABLE_NOW

    def as_dict(self) -> dict:
        c = self.candidate
        return {"opportunity_key": c["opportunity_key"], "revision": self.revision,
                "decision": self.decision, "availability": self.availability,
                "reasons": self.reasons, "surfaced": self.surfaced,
                "existing_id": self.existing_id, "supersedes_id": self.supersedes_id,
                "stale_review_id": self.stale_review_id, "action_type": c["action_type"],
                "subject_id": c["subject_id"], "variant": c.get("variant"),
                "evidence_state": c["evidence_state"],
                "candidate_fingerprint": c["candidate_fingerprint"],
                "evidence_fingerprint": c["evidence_fingerprint"]}  # fmt: skip


@dataclass
class RefreshPlan:
    as_of: str
    tables_ready: bool
    items: list[RefreshItem] = field(default_factory=list)
    not_observed: list[dict] = field(default_factory=list)

    def counts(self) -> dict:
        decisions = defaultdict(int)
        availability = defaultdict(int)
        for item in self.items:
            decisions[item.decision] += 1
            availability[item.availability] += 1
        return {"raw_candidates": len(self.items), "decisions": dict(sorted(decisions.items())),
                "availability": dict(sorted(availability.items())),
                "surfaced": sum(1 for i in self.items if i.surfaced),
                "superseded": sum(1 for i in self.items if i.supersedes_id),
                "stale_reviews": sum(1 for i in self.items if i.stale_review_id),
                "not_observed": len(self.not_observed)}  # fmt: skip

    def as_dict(self) -> dict:
        return {"as_of": self.as_of, "tables_ready": self.tables_ready,
                "required_revision": REQUIRED_REVISION, "counts": self.counts(),
                "items": [i.as_dict() for i in self.items],
                "not_observed": self.not_observed}  # fmt: skip


class GrowthActionHistory:
    def __init__(self, session: Session) -> None:
        self._session = session

    def tables_ready(self) -> bool:
        return history_tables_ready(self._session)

    # -- refresh ---------------------------------------------------------------------------
    def plan_refresh(self, report: dict, coverage: gi.CoverageContext, *,
                     now: datetime) -> RefreshPlan:  # fmt: skip
        """評価の結果を履歴に照らす (書かない)。"""

        now = ensure_aware(now)
        ready = self.tables_ready()
        by_fp: dict[str, GrowthActionCandidate] = {}
        latest: dict[str, GrowthActionCandidate] = {}
        if ready:
            for row in self._session.scalars(select(GrowthActionCandidate)):
                by_fp[row.candidate_fingerprint] = row
                current = latest.get(row.opportunity_key)
                if current is None or row.revision > current.revision:
                    latest[row.opportunity_key] = row
        plan = RefreshPlan(as_of=now.isoformat(), tables_ready=ready)
        seen_keys = set()
        for candidate in report["candidates"]:
            key = candidate["opportunity_key"]
            seen_keys.add(key)
            availability, reasons = gi.assess(candidate, coverage)
            existing = by_fp.get(candidate["candidate_fingerprint"])
            if existing is not None:
                decision = {GA_REJECTED: SUPPRESSED_REJECTED, GA_APPROVED: SUPPRESSED_COMPLETED,
                            GA_CONVERTED: SUPPRESSED_COMPLETED,
                            GA_DISMISSED: SUPPRESSED_DISMISSED,
                            GA_SUPERSEDED: SUPPRESSED_SUPERSEDED,
                            GA_PENDING_REVIEW: REOBSERVED_PENDING}.get(existing.status,
                                                                       REOBSERVED)  # fmt: skip
                plan.items.append(RefreshItem(candidate, decision, availability, reasons,
                                              existing.revision, existing_id=existing.id))
                continue
            previous = latest.get(key)
            item = RefreshItem(candidate, NEW_REVISION if previous else NEW, availability,
                               reasons, (previous.revision + 1) if previous else 1)
            if previous is not None and previous.status != GA_SUPERSEDED:
                item.supersedes_id = previous.id
                if previous.status == GA_PENDING_REVIEW:
                    review = self._review_for(previous.id)
                    item.stale_review_id = review.id if review else None
            plan.items.append(item)
        for key, row in sorted(latest.items()):
            if key in seen_keys or row.status == GA_SUPERSEDED or row.availability == NOT_OBSERVED:
                continue
            plan.not_observed.append({"id": row.id, "opportunity_key": key,
                                      "status": row.status})  # fmt: skip
        return plan

    def apply_refresh(self, plan: RefreshPlan, *, now: datetime) -> dict:
        """PLAN を C9 の表に書く (ほかの表には書かない)。"""

        if not plan.tables_ready:
            raise GrowthActionError(
                f"growth action history tables are missing (alembic revision "
                f"{REQUIRED_REVISION} is not applied); nothing was written")
        now = ensure_aware(now)
        created, updated = [], []
        for item in plan.items:
            c = item.candidate
            if item.existing_id is not None:
                row = self._session.get(GrowthActionCandidate, item.existing_id)
                row.last_seen_at = now
                row.seen_count = (row.seen_count or 0) + 1
                if row.availability != item.availability:
                    self._event(row.id, None, GAE_AVAILABILITY_CHANGED, now,
                                {"from": row.availability, "to": item.availability,
                                 "reasons": item.reasons})  # fmt: skip
                    row.availability = item.availability
                    row.availability_reasons_json = item.reasons
                if row.status in (GA_OBSERVED, GA_ACTIVE):
                    target = _status_for(item.availability)
                    if target != row.status:
                        row.status, row.status_changed_at = target, now
                updated.append(row.id)
                continue
            row = GrowthActionCandidate(
                opportunity_key=c["opportunity_key"], revision=item.revision,
                candidate_fingerprint=c["candidate_fingerprint"],
                evidence_fingerprint=c["evidence_fingerprint"], identity_schema=IDENTITY_SCHEMA,
                action_type=c["action_type"], subject_type=c["subject_type"],
                subject_id=c["subject_id"], article_id=c.get("article_id"),
                keyword_id=c.get("keyword_id"), variant=c.get("variant"),
                status=_status_for(item.availability), availability=item.availability,
                availability_reasons_json=item.reasons, evidence_state=c["evidence_state"],
                snapshot_json=c, first_seen_at=now, last_seen_at=now, seen_count=1,
                status_changed_at=now,
            )  # fmt: skip
            self._session.add(row)
            self._session.flush()
            self._event(row.id, None, GAE_OBSERVED, now,
                        {"decision": item.decision, "availability": item.availability,
                         "reasons": item.reasons})  # fmt: skip
            if item.supersedes_id is not None:
                old = self._session.get(GrowthActionCandidate, item.supersedes_id)
                if old.status != GA_CONVERTED:
                    # 変換済みの版は converted のまま残す (変換の記録と結びついているため)。
                    # 新しい版とのつながりは superseded_by_id で分かる。
                    old.status, old.status_changed_at = GA_SUPERSEDED, now
                old.superseded_by_id = row.id
                self._event(old.id, None, GAE_SUPERSEDED, now,
                            {"superseded_by": row.id,
                             "new_evidence_fingerprint": c["evidence_fingerprint"]})
                if item.stale_review_id is not None:
                    review = self._session.get(GrowthActionReview, item.stale_review_id)
                    review.status = GAR_STALE
                    self._event(old.id, review.id, GAE_REVIEW_STALE, now,
                                {"reason": "a new evidence revision superseded the candidate"})
            created.append(row.id)
        for gone in plan.not_observed:
            row = self._session.get(GrowthActionCandidate, gone["id"])
            self._event(row.id, None, GAE_NOT_OBSERVED, now, {"previous": row.availability})
            row.availability = NOT_OBSERVED
            row.availability_reasons_json = ["no longer produced by the evaluation"]
            if row.status == GA_ACTIVE:
                row.status, row.status_changed_at = GA_OBSERVED, now
        self._session.commit()
        return {"created": created, "reobserved": updated,
                "not_observed": [g["id"] for g in plan.not_observed]}

    def refresh(self, report: dict, coverage: gi.CoverageContext, *, now: datetime,
                execute: bool = False) -> dict:  # fmt: skip
        plan = self.plan_refresh(report, coverage, now=now)
        out = {"plan": plan.as_dict(), "executed": False, "written": None}
        if execute:
            out["written"] = self.apply_refresh(plan, now=now)
            out["executed"] = True
        return out

    # -- reads -----------------------------------------------------------------------------
    def rows(self) -> list[GrowthActionCandidate]:
        if not self.tables_ready():
            return []
        return list(self._session.scalars(select(GrowthActionCandidate)
                                          .order_by(GrowthActionCandidate.id)))

    def get(self, candidate_id: int) -> GrowthActionCandidate:
        row = self._session.get(GrowthActionCandidate, candidate_id)
        if row is None:
            raise GrowthActionError(f"growth action {candidate_id} does not exist")
        return row

    def entry(self, row: GrowthActionCandidate) -> dict:
        review = self._review_for(row.id)
        snapshot = dict(row.snapshot_json or {})
        return {
            **snapshot, "id": row.id, "opportunity_key": row.opportunity_key,
            "revision": row.revision, "status": row.status, "availability": row.availability,
            "availability_reasons": list(row.availability_reasons_json or []),
            "first_seen_at": _iso(row.first_seen_at), "last_seen_at": _iso(row.last_seen_at),
            "seen_count": row.seen_count, "superseded_by_id": row.superseded_by_id,
            "review": ({"id": review.id, "status": review.status} if review else None),
            "candidate_fingerprint": row.candidate_fingerprint,
            "evidence_fingerprint": row.evidence_fingerprint,
        }  # fmt: skip

    def history(self, candidate_id: int) -> dict:
        row = self.get(candidate_id)
        revisions = list(self._session.scalars(
            select(GrowthActionCandidate)
            .where(GrowthActionCandidate.opportunity_key == row.opportunity_key)
            .order_by(GrowthActionCandidate.revision)))  # fmt: skip
        ids = [r.id for r in revisions]
        events = list(self._session.scalars(
            select(GrowthActionEvent).where(GrowthActionEvent.candidate_id.in_(ids))
            .order_by(GrowthActionEvent.id)))  # fmt: skip
        reviews = list(self._session.scalars(
            select(GrowthActionReview).where(GrowthActionReview.candidate_id.in_(ids))
            .order_by(GrowthActionReview.id)))  # fmt: skip
        return {
            "opportunity_key": row.opportunity_key,
            "revisions": [{"id": r.id, "revision": r.revision, "status": r.status,
                           "availability": r.availability,
                           "evidence_fingerprint": r.evidence_fingerprint,
                           "candidate_fingerprint": r.candidate_fingerprint,
                           "first_seen_at": _iso(r.first_seen_at),
                           "last_seen_at": _iso(r.last_seen_at), "seen_count": r.seen_count,
                           "superseded_by_id": r.superseded_by_id} for r in revisions],
            "reviews": [{"id": v.id, "candidate_id": v.candidate_id, "status": v.status,
                         "decided_by": v.decided_by, "decided_at": _iso(v.decided_at),
                         "decision_reason": v.decision_reason} for v in reviews],
            "events": [{"id": e.id, "candidate_id": e.candidate_id, "review_id": e.review_id,
                        "event_type": e.event_type, "occurred_at": _iso(e.occurred_at),
                        "detail": e.detail_json} for e in events],
        }  # fmt: skip

    # -- internals -------------------------------------------------------------------------
    def _review_for(self, candidate_id: int) -> GrowthActionReview | None:
        return self._session.scalars(select(GrowthActionReview).where(
            GrowthActionReview.candidate_id == candidate_id)).first()  # fmt: skip

    def _event(self, candidate_id, review_id, event_type, now, detail) -> None:
        self._session.add(GrowthActionEvent(candidate_id=candidate_id, review_id=review_id,
                                            event_type=event_type, detail_json=detail,
                                            occurred_at=now))  # fmt: skip


def _status_for(availability: str) -> str:
    return GA_ACTIVE if availability == gi.ACTIONABLE_NOW else GA_OBSERVED


def plan_entries(report: dict, plan: RefreshPlan,
                 rows: dict[int, GrowthActionCandidate] | None = None,
                 reviews: dict[int, GrowthActionReview] | None = None) -> list[dict]:  # fmt: skip
    """受け箱の行 (いまの評価と、履歴との照らし合わせから)。履歴の行があればその状態を使う。"""

    rows, reviews = rows or {}, reviews or {}
    out = []
    by_key = {i.candidate["candidate_fingerprint"]: i for i in plan.items}
    for c in report["candidates"]:
        item = by_key[c["candidate_fingerprint"]]
        row = rows.get(item.existing_id) if item.existing_id else None
        review = reviews.get(row.id) if row is not None else None
        status = row.status if row is not None else _status_for(item.availability)
        if row is not None and status in (GA_OBSERVED, GA_ACTIVE):
            status = _status_for(item.availability)
        out.append({**c, "id": item.existing_id, "revision": item.revision, "status": status,
                    "availability": item.availability, "availability_reasons": item.reasons,
                    "decision": item.decision, "surfaced": item.surfaced,
                    "first_seen_at": _iso(row.first_seen_at) if row else None,
                    "last_seen_at": _iso(row.last_seen_at) if row else None,
                    "seen_count": row.seen_count if row else None,
                    "review": ({"id": review.id, "status": review.status}
                               if review else None)})  # fmt: skip
    return out


#: 既定の受け箱に出す (いま動けて、人の判断を待つ) 版の状態。
DEFAULT_INBOX_STATUSES = frozenset({GA_ACTIVE, GA_PENDING_REVIEW})
SUPPRESSED_DECISIONS = frozenset({REOBSERVED, SUPPRESSED_REJECTED, SUPPRESSED_COMPLETED,
                                  SUPPRESSED_DISMISSED, SUPPRESSED_SUPERSEDED})  # fmt: skip


def build_inbox(session: Session, *, settings=None, now: datetime | None = None,
                days: int = 28, report: dict | None = None,
                include_stock_plan: bool = True) -> dict:  # fmt: skip
    """評価 → 既存の仕事 → 履歴との照らし合わせ → 受け箱の行。**書かない。**"""

    from app.services.growth_opportunity_service import GrowthOpportunityService

    now = ensure_aware(now or datetime.now(UTC))
    if report is None:
        report = GrowthOpportunityService(session, settings=settings
                                          ).evaluate_growth_opportunities(now, days=days)
    coverage = collect_coverage(session, now=now, report=report, settings=settings,
                                include_stock_plan=include_stock_plan)  # fmt: skip
    history = GrowthActionHistory(session)
    plan = history.plan_refresh(report, coverage, now=now)
    rows = {r.id: r for r in history.rows()}
    reviews = {}
    if plan.tables_ready:
        reviews = {v.candidate_id: v for v in session.scalars(select(GrowthActionReview))}
    entries = plan_entries(report, plan, rows, reviews)
    session.rollback()
    return {"as_of": now.isoformat(), "report": report, "coverage": coverage, "plan": plan,
            "entries": entries, "history_source": "history" if plan.tables_ready else "plan"}


def filter_entries(entries: list[dict], *, status: str | None = None,
                   action_type: str | None = None, article_id: int | None = None,
                   keyword_id: int | None = None, evidence_state: str | None = None,
                   actionable_only: bool = False, requires_review: bool = False,
                   include_all: bool = False) -> list[dict]:  # fmt: skip
    out = []
    for e in entries:
        if not include_all and not (status or action_type or article_id or keyword_id):
            # 既定: いま動けて人の判断を待つものだけ (重複・覆われたもの・情報は隠す)。
            if e["status"] not in DEFAULT_INBOX_STATUSES or e["decision"] in (
                    SUPPRESSED_DECISIONS - {REOBSERVED}):
                continue
            if e["availability"] != gi.ACTIONABLE_NOW and e["status"] != GA_PENDING_REVIEW:
                continue
        if status and e["status"] != status:
            continue
        if action_type and e["action_type"] != action_type:
            continue
        if article_id is not None and e.get("article_id") != article_id:
            continue
        if keyword_id is not None and e.get("keyword_id") != keyword_id:
            continue
        if evidence_state and e["evidence_state"] != evidence_state:
            continue
        if actionable_only and e["availability"] != gi.ACTIONABLE_NOW:
            continue
        if requires_review and (not e.get("requires_human_approval")
                                or e["action_type"] in gi.INFORMATIONAL_ACTIONS):
            continue
        out.append(e)
    return sorted(out, key=gi.entry_sort_key)


def inbox_counts(entries: list[dict]) -> dict:
    counts = defaultdict(int)
    for e in entries:
        counts[f"availability:{e['availability']}"] += 1
        counts[f"decision:{e['decision']}"] += 1
    return {
        "raw_candidates": len(entries),
        "actionable": sum(1 for e in entries if e["availability"] == gi.ACTIONABLE_NOW
                          and e["status"] in DEFAULT_INBOX_STATUSES),
        "covered_by_existing_work": counts[f"availability:{gi.COVERED}"],
        "blocked": counts[f"availability:{gi.BLOCKED}"],
        "informational": counts[f"availability:{gi.INFORMATIONAL}"],
        "suppressed": sum(1 for e in entries if e["decision"] in SUPPRESSED_DECISIONS),
        "superseded_by_this_refresh": sum(1 for e in entries if e["decision"] == NEW_REVISION),
        "pending_review": sum(1 for e in entries if e["status"] == GA_PENDING_REVIEW),
    }  # fmt: skip


# == 人のレビュー ==========================================


def review_snapshot(entry: dict) -> dict:
    """レビューに見せて固定する内容。"""

    return {
        "review_schema": REVIEW_SCHEMA,
        "opportunity_key": entry["opportunity_key"],
        "revision": entry["revision"],
        "candidate_fingerprint": entry["candidate_fingerprint"],
        "evidence_fingerprint": entry["evidence_fingerprint"],
        "action_type": entry["action_type"], "subject_type": entry["subject_type"],
        "subject_id": entry["subject_id"], "article_id": entry.get("article_id"),
        "keyword_id": entry.get("keyword_id"), "variant": entry.get("variant"),
        "patterns": list(entry.get("patterns") or ()),
        "rationale": entry.get("rationale"), "evidence": list(entry.get("evidence") or ()),
        "evidence_state": entry.get("evidence_state"),
        "blockers": list(entry.get("blockers") or ()),
        "prerequisites": list(entry.get("prerequisites") or ()),
        "priority": entry.get("priority"), "freshness": entry.get("freshness"),
        "effort": entry.get("effort"), "reversible": entry.get("reversible"),
        "external_write_required": entry.get("external_write_required"),
        "conversion": plan_conversion(entry).as_dict(),
        "approval_meaning": "permission to proceed to the next stage only; approval executes "
                            "nothing (no WordPress, Threads, publication or affiliate change)",
    }  # fmt: skip


class GrowthActionReviewService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._history = GrowthActionHistory(session)

    def request_review(self, candidate_id: int, *, now: datetime | None = None,
                       requested_by: str = "human") -> GrowthActionReview:  # fmt: skip
        now = ensure_aware(now or datetime.now(UTC))
        self._ready()
        row = self._history.get(candidate_id)
        existing = self._history._review_for(row.id)
        if existing is not None and existing.status == GAR_PENDING:
            return existing  # 同じ版に 2 つ目のレビューは作らない (冪等)
        if row.status != GA_ACTIVE or row.availability != gi.ACTIONABLE_NOW:
            raise GrowthActionError(
                f"growth action {row.id} is {row.status}/{row.availability}; only active, "
                "actionable candidates can be reviewed")
        if self._latest(row).id != row.id:
            raise GrowthActionError(f"growth action {row.id} is not the latest revision")
        other = self._session.scalars(select(GrowthActionReview).where(
            GrowthActionReview.opportunity_key == row.opportunity_key,
            GrowthActionReview.status == GAR_PENDING)).first()  # fmt: skip
        if other is not None:
            raise GrowthActionError(f"review #{other.id} is already open for this opportunity")
        snapshot = review_snapshot(self._history.entry(row))
        review = GrowthActionReview(
            candidate_id=row.id, review_schema=REVIEW_SCHEMA,
            opportunity_key=row.opportunity_key,
            candidate_fingerprint=row.candidate_fingerprint,
            evidence_fingerprint=row.evidence_fingerprint, action_type=row.action_type,
            subject_type=row.subject_type, subject_id=row.subject_id, article_id=row.article_id,
            keyword_id=row.keyword_id, snapshot_json=snapshot, snapshot_hash=_sha(snapshot),
            status=GAR_PENDING, requested_by=requested_by, requested_at=now)  # fmt: skip
        self._session.add(review)
        self._session.flush()
        row.status, row.status_changed_at = GA_PENDING_REVIEW, now
        self._history._event(row.id, review.id, GAE_REVIEW_REQUESTED, now,
                             {"snapshot_hash": review.snapshot_hash})
        self._session.commit()
        return review

    def approve(self, review_id: int, *, expected_candidate_fingerprint: str,
                decided_by: str = "human", reason: str | None = None,
                now: datetime | None = None) -> GrowthActionReview:  # fmt: skip
        return self._decide(review_id, GAR_APPROVED, expected_candidate_fingerprint,
                            decided_by, reason, now)

    def reject(self, review_id: int, *, expected_candidate_fingerprint: str, reason: str,
               decided_by: str = "human", now: datetime | None = None) -> GrowthActionReview:
        if not (reason or "").strip():
            raise GrowthActionError("a rejection needs a reason")
        return self._decide(review_id, GAR_REJECTED, expected_candidate_fingerprint,
                            decided_by, reason, now)

    def dismiss(self, candidate_id: int, *, reason: str, now: datetime | None = None,
                decided_by: str = "human") -> GrowthActionCandidate:  # fmt: skip
        now = ensure_aware(now or datetime.now(UTC))
        self._ready()
        if not (reason or "").strip():
            raise GrowthActionError("a dismissal needs a reason")
        row = self._history.get(candidate_id)
        if row.status == GA_DISMISSED:
            return row
        if not growth_action_transition_allowed(row.status, GA_DISMISSED):
            raise GrowthActionError(f"growth action {row.id} is {row.status}; cannot dismiss")
        row.status, row.status_changed_at = GA_DISMISSED, now
        self._history._event(row.id, None, GAE_DISMISSED, now,
                             {"reason": reason, "decided_by": decided_by})
        self._session.commit()
        return row

    # -- internals -------------------------------------------------------------------------
    def _decide(self, review_id, decision, expected, decided_by, reason, now):
        now = ensure_aware(now or datetime.now(UTC))
        self._ready()
        review = self._session.get(GrowthActionReview, review_id)
        if review is None:
            raise GrowthActionError(f"review {review_id} does not exist")
        if review.status == decision and review.candidate_fingerprint == expected:
            return review  # 同じ決定をもう一度 (冪等。出来事を増やさない)
        if review.status != GAR_PENDING:
            raise GrowthActionError(f"review {review.id} is already {review.status}")
        row = self._session.get(GrowthActionCandidate, review.candidate_id)
        problems = []
        if expected != review.candidate_fingerprint:
            problems.append("the presented candidate fingerprint does not match the review")
        if row.candidate_fingerprint != review.candidate_fingerprint:
            problems.append("the candidate changed after the review was requested")
        if row.status != GA_PENDING_REVIEW:
            problems.append(f"the candidate is {row.status}")
        if self._latest(row).id != row.id:
            problems.append("a newer evidence revision exists")
        if row.availability == NOT_OBSERVED:
            problems.append("the candidate is no longer produced by the evaluation")
        if _sha(review.snapshot_json) != review.snapshot_hash:
            problems.append("the frozen review snapshot does not match its hash")
        if problems:
            # fail closed: 古い承認を使わない。レビューは stale にして残す (理由つき)。
            review.status = GAR_STALE
            self._history._event(row.id, review.id, GAE_REVIEW_STALE, now,
                                 {"attempted": decision, "problems": problems})
            self._session.commit()
            raise GrowthActionError("review is stale: " + "; ".join(problems))
        review.status, review.decided_by = decision, decided_by
        review.decision_reason, review.decided_at = reason, now
        row.status = GA_APPROVED if decision == GAR_APPROVED else GA_REJECTED
        row.status_changed_at = now
        self._history._event(row.id, review.id,
                             GAE_APPROVED if decision == GAR_APPROVED else GAE_REJECTED, now,
                             {"decided_by": decided_by, "reason": reason,
                              "executes": "nothing (approval is permission only)"})
        self._session.commit()
        return review

    def _latest(self, row: GrowthActionCandidate) -> GrowthActionCandidate:
        return self._session.scalars(
            select(GrowthActionCandidate)
            .where(GrowthActionCandidate.opportunity_key == row.opportunity_key)
            .order_by(GrowthActionCandidate.revision.desc())).first()  # fmt: skip

    def _ready(self) -> None:
        if not history_tables_ready(self._session):
            raise GrowthActionError(
                f"growth action history tables are missing (alembic revision "
                f"{REQUIRED_REVISION} is not applied)")


__all__ = [
    "HISTORY_TABLES", "NEW", "NEW_REVISION", "NOT_OBSERVED", "REOBSERVED", "REOBSERVED_PENDING",
    "REQUIRED_REVISION", "REVIEW_SCHEMA", "SUPPRESSED_COMPLETED", "SUPPRESSED_DISMISSED",
    "SUPPRESSED_REJECTED", "SUPPRESSED_SUPERSEDED", "GrowthActionError", "GrowthActionHistory",
    "GrowthActionReviewService", "RefreshItem", "RefreshPlan", "collect_coverage",
    "history_tables_ready", "plan_entries", "review_snapshot", "DEFAULT_INBOX_STATUSES",
    "SUPPRESSED_DECISIONS", "build_inbox", "filter_entries", "inbox_counts",
]  # fmt: skip
