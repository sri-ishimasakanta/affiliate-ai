"""承認した Growth Action を既存の流れへ渡す (C9 Batch 3)。**外には書かない。**

- ``GrowthActionConversionService.plan``: 変換の計画 (``growth-action-conversion/1``) と、実行の
  直前に行う検査の結果を返す。**書かない。**
- ``GrowthActionConversionService.execute``: 検査が全部通ったときだけ、既存の流れの **手元の**
  依頼を作る。いま実行できるのは ``review_internal_links`` → ``ChangeRequest`` (awaiting_approval)
  だけ。ChangeRequest の承認・WordPress への適用は **しない** (その流れの人の判断)。Growth Action
  の承認を ChangeRequest の承認に流用しない。同じ変換を 2 回しても依頼は 1 つ
  (``growth_action_conversions.idempotency_key`` と ``change_requests.idempotency_key``)。
- ``GrowthActionOutcomeService``: 変換の先の状態を **読むだけ** で観測し、実際に変わった時刻
  (適用の成功) から、変わる前と後の窓を並べる (``ChangeEffectService`` を使う)。因果を言わない。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware
from app.growth import analysis as ga
from app.growth import inbox as gi
from app.growth import outcome as go
from app.growth.conversion import EXEC_LOCAL_HANDOFF, plan_conversion
from app.models import (
    CR_APPROVED,
    CR_OPEN_STATUSES,
    ChangeApplication,
    ChangeRequest,
    ChangeRequestApproval,
    SeoImprovementCandidate,
)
from app.models.growth_action import (
    GA_APPROVED,
    GA_CONVERTED,
    GAC_CREATED,
    GAC_LINKED_EXISTING,
    GAE_CONVERSION_EXECUTED,
    GAE_CONVERSION_FAILED,
    GAE_CONVERSION_REFUSED,
    GAR_APPROVED,
    GrowthActionCandidate,
    GrowthActionConversion,
    GrowthActionEvent,
    GrowthActionReview,
)
from app.services.growth_action_service import (
    GrowthActionError,
    build_inbox,
    history_tables_ready,
)

CONVERSION_SCHEMA = "growth-action-conversion/1"
#: 変換の意味の版 (変われば idempotency key も変わる)。
INTERNAL_LINK_SEMANTICS = "internal-link-change-request-v1"
CONVERSION_REVISION = "74dbecaa4bb2"
TARGET_CHANGE_REQUEST = "change_request"


def _sha(payload) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=str)  # fmt: skip
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _iso(value) -> str | None:
    return ensure_aware(value).isoformat() if value is not None else None


def conversions_ready(session: Session) -> bool:
    names = set(inspect(session.connection()).get_table_names())
    return history_tables_ready(session) and "growth_action_conversions" in names


def _review_for(session: Session, candidate_id: int) -> GrowthActionReview | None:
    return session.scalars(select(GrowthActionReview).where(
        GrowthActionReview.candidate_id == candidate_id)).first()  # fmt: skip


def _latest(session: Session, row: GrowthActionCandidate) -> GrowthActionCandidate:
    return session.scalars(select(GrowthActionCandidate)
                           .where(GrowthActionCandidate.opportunity_key == row.opportunity_key)
                           .order_by(GrowthActionCandidate.revision.desc())).first()


def internal_link_targets(review: GrowthActionReview | None) -> list[int]:
    """レビューに固定した内部リンクの先 (C6 の INTERNAL_LINK_OPPORTUNITY の証拠から)。"""

    if review is None:
        return []
    targets = set()
    for item in (review.snapshot_json or {}).get("evidence") or []:
        evidence = item.get("evidence") or {}
        if item.get("source_engine") == "seo" and "INTERNAL_LINK_OPPORTUNITY" in str(
                item.get("reason")) and evidence.get("target_article_id") is not None:
            targets.add(int(evidence["target_article_id"]))
    return sorted(targets)


class GrowthActionConversionService:
    def __init__(self, session: Session, *, settings=None, inbox_builder=None) -> None:
        self._session = session
        self._settings = settings
        self._inbox_builder = inbox_builder or build_inbox

    # -- plan ------------------------------------------------------------------------------
    def plan(self, candidate_id: int, *, now: datetime | None = None) -> dict:
        """変換の計画と検査の結果。**書かない。**"""

        now = ensure_aware(now or datetime.now(UTC))
        if not history_tables_ready(self._session):
            raise GrowthActionError("growth action history tables are missing")
        row = self._session.get(GrowthActionCandidate, candidate_id)
        if row is None:
            raise GrowthActionError(f"growth action {candidate_id} does not exist")
        review = _review_for(self._session, row.id)
        entry = dict(row.snapshot_json or {})
        conversion = plan_conversion(entry)
        targets = (internal_link_targets(review)
                   if row.action_type == ga.REVIEW_INTERNAL_LINKS else [])  # fmt: skip
        key = self._idempotency_key(row, review, conversion.target_workflow)
        existing = self._existing(key)
        checks = self._checks(row, review, conversion, targets, now, existing)
        plan = {
            "schema_version": CONVERSION_SCHEMA,
            "growth_action_id": row.id, "opportunity_key": row.opportunity_key,
            "revision": row.revision, "candidate_fingerprint": row.candidate_fingerprint,
            "evidence_fingerprint": row.evidence_fingerprint,
            "review_id": review.id if review else None,
            "review_status": review.status if review else None,
            "action_type": row.action_type,
            "target_workflow": conversion.target_workflow,
            "target_subject": {"article_id": row.article_id, "keyword_id": row.keyword_id,
                               "internal_link_targets": targets},
            "supported": conversion.execution_mode == EXEC_LOCAL_HANDOFF,
            "support": conversion.support, "execution_mode": conversion.execution_mode,
            "missing": conversion.missing, "entry_point": conversion.entry_point,
            "steps": list(conversion.steps),
            "prerequisites": list(entry.get("prerequisites") or ()),
            "blockers": list(entry.get("blockers") or ()),
            "expected_local_writes": self._expected_local_writes(conversion.execution_mode),
            "expected_external_writes": [],
            "idempotency_key": key,
            "note": conversion.note,
        }  # fmt: skip
        plan["plan_hash"] = _sha({k: v for k, v in plan.items() if k != "review_status"})
        failed = [c for c in checks if not c["ok"]]
        return {"plan": plan, "checks": checks, "executable": not failed and existing is None,
                "already_converted": _conversion_dict(existing) if existing else None,
                "downstream": self.downstream(existing) if existing else [],
                "side_effects": {"db_writes": 0, "external_writes": 0}}

    # -- execute ---------------------------------------------------------------------------
    def execute(self, candidate_id: int, *, now: datetime | None = None,
                executed_by: str = "human") -> dict:  # fmt: skip
        """検査が全部通ったときだけ、手元の依頼を作る。**外には書かない。**"""

        now = ensure_aware(now or datetime.now(UTC))
        if not conversions_ready(self._session):
            raise GrowthActionError(
                f"growth action conversion table is missing (alembic revision "
                f"{CONVERSION_REVISION} is not applied); nothing was written")
        result = self.plan(candidate_id, now=now)
        plan = result["plan"]
        if result["already_converted"] is not None:
            # 冪等: 同じ変換はもう済んでいる。依頼を作らずに、前の結果を返す。
            return {**result, "executed": False, "idempotent_replay": True,
                    "conversion": result["already_converted"]}
        failed = [c for c in result["checks"] if not c["ok"]]
        if failed:
            self._event(candidate_id, plan["review_id"], GAE_CONVERSION_REFUSED, now,
                        {"failed_checks": failed, "plan_hash": plan["plan_hash"]})
            self._session.commit()
            raise GrowthActionError("conversion refused: " + "; ".join(
                f"{c['name']}: {c['detail']}" for c in failed))
        try:
            downstream_ids, created, writes = self._handoff_internal_links(plan, now)
        except Exception as exc:  # noqa: BLE001 - 失敗は記録して、上へ理由を返す
            self._session.rollback()
            self._event(candidate_id, plan["review_id"], GAE_CONVERSION_FAILED, now,
                        {"error": f"{type(exc).__name__}: {exc}"[:500],
                         "plan_hash": plan["plan_hash"]})
            self._session.commit()
            raise GrowthActionError(f"conversion failed: {exc}") from None
        row = self._session.get(GrowthActionCandidate, candidate_id)
        conversion = GrowthActionConversion(
            candidate_id=row.id, review_id=plan["review_id"], schema_version=CONVERSION_SCHEMA,
            idempotency_key=plan["idempotency_key"], action_type=row.action_type,
            target_workflow=TARGET_CHANGE_REQUEST,
            candidate_fingerprint=row.candidate_fingerprint, plan_json=plan,
            plan_hash=plan["plan_hash"],
            status=GAC_CREATED if created else GAC_LINKED_EXISTING,
            downstream_type=TARGET_CHANGE_REQUEST, downstream_ids_json=downstream_ids,
            local_writes_json=writes + ["growth_action_conversions", "growth_action_events",
                                        "growth_action_candidates.status"],
            executed_by=executed_by, executed_at=now)  # fmt: skip
        self._session.add(conversion)
        self._session.flush()
        row.status, row.status_changed_at = GA_CONVERTED, now
        self._event(row.id, plan["review_id"], GAE_CONVERSION_EXECUTED, now,
                    {"conversion_id": conversion.id, "downstream_type": TARGET_CHANGE_REQUEST,
                     "downstream_ids": downstream_ids, "created": created,
                     "external_writes": 0,
                     "meaning": "handed off to the change request flow; not approved, "
                                "not applied"})  # fmt: skip
        self._session.commit()
        return {**result, "executed": True, "idempotent_replay": False,
                "conversion": _conversion_dict(conversion),
                "downstream": self.downstream(conversion),
                "side_effects": {"db_writes": conversion.local_writes_json,
                                 "external_writes": 0, "wordpress_writes": 0,
                                 "change_request_approvals": 0, "applications": 0}}

    # -- downstream (読むだけ) ---------------------------------------------------------------
    def downstream(self, conversion: GrowthActionConversion | None) -> list[dict]:
        if conversion is None or conversion.downstream_type != TARGET_CHANGE_REQUEST:
            return []
        out = []
        for cr_id in conversion.downstream_ids_json or []:
            request = self._session.get(ChangeRequest, cr_id)
            if request is None:
                out.append({"type": TARGET_CHANGE_REQUEST, "id": cr_id, "state": "missing"})
                continue
            approval = self._session.scalars(
                select(ChangeRequestApproval).where(
                    ChangeRequestApproval.change_request_id == cr_id)
                .order_by(ChangeRequestApproval.id.desc())).first()  # fmt: skip
            applications = [{"id": a.id, "outcome": a.outcome,
                             "finished_at": _iso(a.finished_at),
                             "attempted_at": _iso(a.attempted_at)}
                            for a in self._session.scalars(select(ChangeApplication).where(
                                ChangeApplication.change_request_id == cr_id)
                                .order_by(ChangeApplication.id))]  # fmt: skip
            effective_at, source = go.effective_at_for(TARGET_CHANGE_REQUEST,
                                                       applications=applications)
            out.append({"type": TARGET_CHANGE_REQUEST, "id": cr_id, "state": request.status,
                        "target_article_id": request.target_article_id,
                        "latest_decision": approval.decision if approval else None,
                        "applications": applications, "effective_at": effective_at,
                        "effective_source": source,
                        "note": "Growth Action converted ≠ change applied"})  # fmt: skip
        return out

    # -- internals -------------------------------------------------------------------------
    def _idempotency_key(self, row, review, target_workflow) -> str:
        digest = _sha({"schema": CONVERSION_SCHEMA, "semantics": INTERNAL_LINK_SEMANTICS,
                       "candidate_fingerprint": row.candidate_fingerprint,
                       "review_id": review.id if review else None,
                       "review_snapshot_hash": review.snapshot_hash if review else None,
                       "target_workflow": target_workflow})  # fmt: skip
        return f"gac1:{digest[:48]}"

    def _existing(self, key: str) -> GrowthActionConversion | None:
        if not conversions_ready(self._session):
            return None
        return self._session.scalars(select(GrowthActionConversion).where(
            GrowthActionConversion.idempotency_key == key)).first()  # fmt: skip

    @staticmethod
    def _expected_local_writes(mode: str) -> list[str]:
        if mode != EXEC_LOCAL_HANDOFF:
            return []
        return ["seo_improvement_runs / seo_improvement_candidates (only when no persisted C6 "
                "candidate matches the frozen target)",
                "change_requests (awaiting_approval; its own human approval and apply step)",
                "growth_action_conversions", "growth_action_events",
                "growth_action_candidates.status → converted"]

    def _checks(self, row, review, conversion, targets, now, existing) -> list[dict]:
        checks = []

        def check(name, ok, detail=""):
            checks.append({"name": name, "ok": bool(ok), "detail": detail})

        check("supported", conversion.execution_mode == EXEC_LOCAL_HANDOFF,
              f"execution mode {conversion.execution_mode}"
              + (f": {conversion.missing}" if conversion.missing else ""))
        check("approved_review", review is not None and review.status == GAR_APPROVED,
              f"review {review.status if review else 'missing'}")
        if review is not None:
            intact = _sha(review.snapshot_json) == review.snapshot_hash
            check("review_fingerprint_match",
                  review.candidate_fingerprint == row.candidate_fingerprint and intact,
                  "frozen review matches the candidate" if intact else "snapshot hash mismatch")
        latest = _latest(self._session, row)
        check("current_revision", latest.id == row.id,
              "latest revision" if latest.id == row.id else f"newer revision #{latest.id}")
        check("not_superseded", row.status in (GA_APPROVED, GA_CONVERTED),
              f"candidate status {row.status}")
        if conversion.execution_mode != EXEC_LOCAL_HANDOFF or existing is not None:
            return checks
        check("frozen_targets", bool(targets), f"targets {targets}")
        box = self._inbox_builder(self._session, settings=self._settings, now=now)
        current = next((e for e in box["entries"]
                        if e["candidate_fingerprint"] == row.candidate_fingerprint), None)
        check("still_observed", current is not None,
              "the current evaluation still produces this candidate" if current
              else "the current evaluation no longer produces this exact candidate")
        if current is not None:
            check("still_actionable", current["availability"] == gi.ACTIONABLE_NOW,
                  f"{current['availability']}: {'; '.join(current['availability_reasons'])}")
            frozen = list((review.snapshot_json or {}).get("blockers") or []) if review else []
            check("blockers_unchanged", list(current.get("blockers") or []) == frozen,
                  f"now {current.get('blockers')}, reviewed {frozen}")
        conflicts = list(self._session.scalars(select(ChangeRequest.id).where(
            ChangeRequest.article_id == row.article_id,
            ChangeRequest.status.in_((*CR_OPEN_STATUSES, CR_APPROVED)))))  # fmt: skip
        check("no_conflicting_downstream_work", not conflicts,
              f"open change request(s) for the article: {conflicts}" if conflicts else "none")
        return checks

    def _handoff_internal_links(self, plan: dict, now: datetime) -> tuple[list[int], bool, list]:
        """C6 の候補 (保存済み、無ければ保存) → ChangeRequest (awaiting_approval) を 1 つずつ。"""

        from app.services.change_request_service import ChangeRequestService

        article_id = plan["target_subject"]["article_id"]
        ids, created, writes = [], False, []
        for target in plan["target_subject"]["internal_link_targets"]:
            key = f"{plan['idempotency_key']}:t{target}"
            request = self._session.scalars(select(ChangeRequest).where(
                ChangeRequest.idempotency_key == key)).first()  # fmt: skip
            if request is None:
                candidate = self._seo_candidate(article_id, target)
                if candidate is None:
                    candidate = self._persist_seo(plan, now, article_id, target)
                    writes.append("seo_improvement_runs / seo_improvement_candidates")
                request = ChangeRequestService(self._session).propose_from_seo_candidate(
                    candidate_id=candidate.id, idempotency_key=key, now=now)
                if request.idempotency_key == key:
                    created = True
                    writes.append("change_requests")
            ids.append(request.id)
        return ids, created, sorted(set(writes))

    def _seo_candidate(self, article_id: int, target: int) -> SeoImprovementCandidate | None:
        for candidate in self._session.scalars(
            select(SeoImprovementCandidate).where(
                SeoImprovementCandidate.article_id == article_id,
                SeoImprovementCandidate.candidate_type == "INTERNAL_LINK_OPPORTUNITY")
            .order_by(SeoImprovementCandidate.id.desc())):  # fmt: skip
            if (candidate.evidence_json or {}).get("target_article_id") == target:
                return candidate
        return None

    def _persist_seo(self, plan, now, article_id, target) -> SeoImprovementCandidate:
        """保存済みの C6 の候補が無いとき、C6 の評価を既存の persist で残す (手元だけ)。"""

        from app.services.seo_improvement_candidate_service import SeoImprovementCandidateService

        service = SeoImprovementCandidateService(self._session, settings=self._settings)
        report = service.evaluate(now=now)
        produced = any(
            c.get("candidate_type") == "INTERNAL_LINK_OPPORTUNITY"
            and (c.get("evidence") or {}).get("target_article_id") == target
            for a in report.articles if a.article_id == article_id for c in a.candidates)
        if not produced:
            # C6 がいまこのリンクを提案していない: 何も保存せずに止める (失敗の記録だけ残る)。
            raise GrowthActionError(f"C6 no longer proposes an internal link from article "
                                    f"{article_id} to {target}")
        service.persist(report, idempotency_key=f"{plan['idempotency_key']}:seo")
        self._session.commit()
        candidate = self._seo_candidate(article_id, target)
        if candidate is None:
            raise GrowthActionError(f"C6 no longer proposes an internal link from article "
                                    f"{article_id} to {target}")
        return candidate

    def _event(self, candidate_id, review_id, event_type, now, detail) -> None:
        self._session.add(GrowthActionEvent(candidate_id=candidate_id, review_id=review_id,
                                            event_type=event_type, detail_json=detail,
                                            occurred_at=now))  # fmt: skip


def _conversion_dict(c: GrowthActionConversion) -> dict:
    return {"id": c.id, "growth_action_id": c.candidate_id, "review_id": c.review_id,
            "status": c.status, "target_workflow": c.target_workflow,
            "downstream_type": c.downstream_type, "downstream_ids": list(c.downstream_ids_json),
            "idempotency_key": c.idempotency_key, "plan_hash": c.plan_hash,
            "executed_at": _iso(c.executed_at), "executed_by": c.executed_by,
            "local_writes": list(c.local_writes_json or [])}  # fmt: skip


# == 効果の観測 (読むだけ) ==============================================


class GrowthActionOutcomeService:
    def __init__(self, session: Session, *, settings=None) -> None:
        self._session = session
        self._settings = settings

    def conversions(self) -> list[GrowthActionConversion]:
        if not conversions_ready(self._session):
            return []
        return list(self._session.scalars(select(GrowthActionConversion)
                                          .order_by(GrowthActionConversion.id)))

    def anchors(self) -> list[go.MeasurementAnchor]:
        service = GrowthActionConversionService(self._session, settings=self._settings)
        out = []
        for conversion in self.conversions():
            row = self._session.get(GrowthActionCandidate, conversion.candidate_id)
            for item in service.downstream(conversion):
                out.append(go.MeasurementAnchor(
                    growth_action_id=conversion.candidate_id, action_type=row.action_type,
                    subject_id=row.subject_id, article_id=row.article_id,
                    downstream_type=item["type"], downstream_id=item["id"],
                    downstream_state=item["state"], effective_at=item.get("effective_at"),
                    effective_source=item.get("effective_source")))  # fmt: skip
        return out

    def outcome(self, anchor: go.MeasurementAnchor, *, now: datetime | None = None,
                checkpoint: str | None = None) -> go.GrowthActionOutcome:  # fmt: skip
        now = ensure_aware(now or datetime.now(UTC))
        freshness = self._freshness(now)
        windows = [c for c in go.CHECKPOINTS if checkpoint in (None, c[0])]
        if anchor.effective_at is None:
            checkpoints = tuple(go.Checkpoint(name, days, go.WAITING,
                                              ("not applied yet: effective_at is None",))
                                for name, days in windows)  # fmt: skip
        elif anchor.downstream_type == TARGET_CHANGE_REQUEST:
            from app.services.change_effect_service import ChangeEffectService

            stale = freshness.get("search_console") == "stale"
            checkpoints = []
            for name, days in windows:
                report = ChangeEffectService(self._session, settings=self._settings).build(
                    window_days=days, request_id=anchor.downstream_id, now=now)
                effect = report.effects[0].as_dict() if report.effects else None
                checkpoints.append(go.checkpoint_from_effect(name, days, effect,
                                                             source_stale=stale))
            checkpoints = tuple(checkpoints)
        else:
            checkpoints = tuple(go.Checkpoint(name, days, go.NOT_APPLICABLE,
                                              (f"no measurement for {anchor.downstream_type}",))
                                for name, days in windows)  # fmt: skip
        self._session.rollback()
        return go.GrowthActionOutcome(anchor=anchor, checkpoints=checkpoints,
                                      source_freshness=freshness, notes=go.NOTES)

    def _freshness(self, now: datetime) -> dict:
        from app.operations.policy import get_policy as get_ops_policy
        from app.operations.source_health import evaluate_source_refresh
        from app.services.operations_source_health_service import collect_source_freshness

        policy = get_ops_policy()
        today = now.astimezone(policy.timezone).date()
        out = {}
        for name, f in collect_source_freshness(self._session).items():
            if not f.ever_imported:
                out[name] = "unavailable"
            elif evaluate_source_refresh(freshness=f, today=today, now=now, policy=policy):
                out[name] = "stale"
            else:
                out[name] = "fresh"
        return out


__all__ = ["CONVERSION_REVISION", "CONVERSION_SCHEMA", "GrowthActionConversionService",
           "GrowthActionOutcomeService", "INTERNAL_LINK_SEMANTICS", "conversions_ready",
           "internal_link_targets"]
