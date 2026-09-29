"""承認した Growth Action を既存の流れへ渡す (C9 Batch 3 / C9-B)。**外には書かない。**

- ``GrowthActionConversionService.plan``: 変換の計画 (``growth-action-conversion/1``) と、実行の
  直前に行う検査の結果を返す。**書かない。**
- ``GrowthActionConversionService.execute``: 検査が全部通ったときだけ、既存の流れの **手元の**
  依頼を作る。いま実行できるのは ``review_internal_links`` → ``ChangeRequest`` (awaiting_approval)
  だけ。ChangeRequest の承認・WordPress への適用は **しない** (その流れの人の判断)。Growth Action
  の承認を ChangeRequest の承認に流用しない。同じ変換を 2 回しても依頼は 1 つ
  (``growth_action_conversions.idempotency_key`` と ``change_requests.idempotency_key``)。
- C9-B: Threads の提案 (通常・別の切り口) → 記事を指定した生成の依頼、新しい記事 → 記事の計画の
  依頼、本文・メタディスクリプション・アフィリエイトの配置 → 変更の準備の依頼
  (``growth_handoff_requests``、``GrowthHandoffService``)。どれも手元の依頼だけで、OpenAI・Threads・
  WordPress を呼ばない。先の承認・生成・公開・適用は、その流れの独自のまま。
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
from app.growth.conversion import (
    CHANGE_TYPE_FOR_ACTION,
    EXEC_LOCAL_HANDOFF,
    HANDOFF_TARGETS,
    TARGET_ARTICLE_PLANNING,
    TARGET_CHANGE_PREPARATION,
    TARGET_THREADS_GENERATION,
    plan_conversion,
)
from app.models import (
    CR_APPROVED,
    CR_OPEN_STATUSES,
    PUB_PUBLISHED,
    Article,
    ArticleAffiliateProgram,
    ChangeApplication,
    ChangeRequest,
    ChangeRequestApproval,
    Keyword,
    SeoImprovementCandidate,
    ThreadsPostProposal,
    ThreadsPublication,
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
from app.models.growth_handoff import (
    GH_ARTICLE_PLANNING,
    GH_CHANGE_PREPARATION,
    GH_LANE_ALTERNATIVE_ANGLE,
    GH_LANE_REGULAR,
    GH_THREADS_GENERATION,
)
from app.models.threads_post_proposal import TP_APPROVED, TP_OPEN_STATES
from app.services.growth_action_service import (
    GrowthActionError,
    build_inbox,
    history_tables_ready,
)
from app.services.growth_handoff_service import (
    HANDOFF_REVISION,
    GrowthHandoffService,
    handoff_ready,
    source_hashes,
)

CONVERSION_SCHEMA = "growth-action-conversion/1"
#: 変換の意味の版 (変われば idempotency key も変わる)。
INTERNAL_LINK_SEMANTICS = "internal-link-change-request-v1"
CONVERSION_REVISION = "74dbecaa4bb2"
TARGET_CHANGE_REQUEST = "change_request"
#: C9-B: 手元の依頼 (growth_handoff_requests) へ渡す変換の意味の版。
HANDOFF_SEMANTICS = "growth-handoff-request-v1"
#: 渡す先 → 依頼の流れ。
HANDOFF_WORKFLOW = {TARGET_THREADS_GENERATION: GH_THREADS_GENERATION,
                    TARGET_ARTICLE_PLANNING: GH_ARTICLE_PLANNING,
                    TARGET_CHANGE_PREPARATION: GH_CHANGE_PREPARATION}  # fmt: skip
#: 渡す先ごとの、変換の意味 (承認・生成・公開・適用とは別)。
HANDOFF_MEANING = {
    TARGET_THREADS_GENERATION: "a targeted generation request for the existing proposal stock "
                               "maintenance; not generated, not approved, not published",
    TARGET_ARTICLE_PLANNING: "a local article planning request; no article was created, not "
                             "approved, not published",
    TARGET_CHANGE_PREPARATION: "a local change preparation request with frozen source hashes; "
                               "no change content, not approved, not applied",
}  # fmt: skip


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
        handoff_target = HANDOFF_TARGETS.get(row.action_type)
        if handoff_target == TARGET_CHANGE_REQUEST:
            handoff_target = None  # Batch 3 の内部リンクの経路 (そのまま)
        key = self._idempotency_key(row, review, conversion.target_workflow,
                                    handoff=handoff_target is not None)
        existing = self._existing(key)
        current = None
        if conversion.execution_mode == EXEC_LOCAL_HANDOFF and existing is None:
            current = self._current(row, now)
        frozen = (self._freeze(row, review, handoff_target, current)
                  if handoff_target is not None else None)  # fmt: skip
        checks = self._checks(row, review, conversion, targets, now, existing, current=current,
                              handoff_target=handoff_target, frozen=frozen)
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
        if handoff_target is not None:
            plan["target_workflow"] = handoff_target
            plan["expected_local_writes"] = [
                f"growth_handoff_requests ({HANDOFF_WORKFLOW[handoff_target]}, pending)",
                "growth_action_conversions", "growth_action_events",
                "growth_action_candidates.status → converted"]
            plan["expected_external_calls"] = []
            plan["frozen_preview"] = frozen
            plan["source_hash"] = _sha((frozen or {}).get("source") or {})
            plan["meaning"] = HANDOFF_MEANING[handoff_target]
        plan["plan_hash"] = _sha({k: v for k, v in plan.items() if k != "review_status"})
        failed = [c for c in checks if not c["ok"]]
        return {"plan": plan, "checks": checks, "executable": not failed and existing is None,
                "already_converted": _conversion_dict(existing) if existing else None,
                "downstream": self.downstream(existing) if existing else [],
                "side_effects": {"db_writes": 0, "external_writes": 0}}

    # -- execute ---------------------------------------------------------------------------
    def execute(self, candidate_id: int, *, now: datetime | None = None,
                executed_by: str = "human",
                expected_source_hash: str | None = None) -> dict:  # fmt: skip
        """検査が全部通ったときだけ、手元の依頼を作る。**外には書かない。**

        変更の準備は ``expected_source_hash`` (計画で見た元の hash) が要る。記事が計画の後に
        変わっていれば断る (fail closed)。
        """

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
        target = plan["target_workflow"]
        handoff = target in HANDOFF_WORKFLOW
        if handoff and not handoff_ready(self._session):
            raise GrowthActionError(
                f"growth handoff request table is missing (alembic revision {HANDOFF_REVISION} "
                "is not applied); nothing was written")
        if handoff and (target == TARGET_CHANGE_PREPARATION or expected_source_hash):
            ok = expected_source_hash == plan["source_hash"]
            result["checks"].append({
                "name": "source_hash_confirmed", "ok": ok,
                "detail": "the source is the one shown in the plan" if ok else
                ("pass --expected-source-hash from the plan" if not expected_source_hash else
                 "the article changed after the plan (source hash differs); re-run the plan")})
        failed = [c for c in result["checks"] if not c["ok"]]
        if failed:
            self._event(candidate_id, plan["review_id"], GAE_CONVERSION_REFUSED, now,
                        {"failed_checks": failed, "plan_hash": plan["plan_hash"]})
            self._session.commit()
            raise GrowthActionError("conversion refused: " + "; ".join(
                f"{c['name']}: {c['detail']}" for c in failed))
        try:
            if handoff:
                downstream_ids, created, writes = self._handoff_request(plan, now)
            else:
                downstream_ids, created, writes = self._handoff_internal_links(plan, now)
        except Exception as exc:  # noqa: BLE001 - 失敗は記録して、上へ理由を返す
            self._session.rollback()
            self._event(candidate_id, plan["review_id"], GAE_CONVERSION_FAILED, now,
                        {"error": f"{type(exc).__name__}: {exc}"[:500],
                         "plan_hash": plan["plan_hash"]})
            self._session.commit()
            raise GrowthActionError(f"conversion failed: {exc}") from None
        row = self._session.get(GrowthActionCandidate, candidate_id)
        downstream_type = target if handoff else TARGET_CHANGE_REQUEST
        conversion = GrowthActionConversion(
            candidate_id=row.id, review_id=plan["review_id"], schema_version=CONVERSION_SCHEMA,
            idempotency_key=plan["idempotency_key"], action_type=row.action_type,
            target_workflow=downstream_type,
            candidate_fingerprint=row.candidate_fingerprint, plan_json=plan,
            plan_hash=plan["plan_hash"],
            status=GAC_CREATED if created else GAC_LINKED_EXISTING,
            downstream_type=downstream_type, downstream_ids_json=downstream_ids,
            local_writes_json=writes + ["growth_action_conversions", "growth_action_events",
                                        "growth_action_candidates.status"],
            executed_by=executed_by, executed_at=now)  # fmt: skip
        self._session.add(conversion)
        self._session.flush()
        row.status, row.status_changed_at = GA_CONVERTED, now
        meaning = (HANDOFF_MEANING[target] if handoff else
                   "handed off to the change request flow; not approved, not applied")
        self._event(row.id, plan["review_id"], GAE_CONVERSION_EXECUTED, now,
                    {"conversion_id": conversion.id, "downstream_type": downstream_type,
                     "downstream_ids": downstream_ids, "created": created,
                     "external_writes": 0, "meaning": meaning})  # fmt: skip
        self._session.commit()
        return {**result, "executed": True, "idempotent_replay": False,
                "conversion": _conversion_dict(conversion),
                "downstream": self.downstream(conversion),
                "side_effects": {"db_writes": conversion.local_writes_json,
                                 "external_writes": 0, "wordpress_writes": 0,
                                 "threads_writes": 0, "openai_calls": 0,
                                 "change_request_approvals": 0, "applications": 0}}

    # -- downstream (読むだけ) ---------------------------------------------------------------
    def downstream(self, conversion: GrowthActionConversion | None) -> list[dict]:
        if conversion is not None and conversion.downstream_type in HANDOFF_WORKFLOW:
            return self._handoff_downstream(conversion)
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

    def _handoff_downstream(self, conversion: GrowthActionConversion) -> list[dict]:
        """手元の依頼の状態と、その先 (提案・記事・変更) の状態 (別々に。読むだけ)。"""

        if not handoff_ready(self._session):
            return [{"type": conversion.downstream_type, "id": i, "state": "missing"}
                    for i in conversion.downstream_ids_json or []]
        service = GrowthHandoffService(self._session)
        out = []
        for request_id in conversion.downstream_ids_json or []:
            try:
                observed = service.observe(service.get(request_id))
            except Exception:  # noqa: BLE001 - 無い依頼は missing と見せる
                out.append({"type": conversion.downstream_type, "id": request_id,
                            "state": "missing"})
                continue
            source = {"threads_proposal": "threads_publication.published_at",
                      "article": "article.published_at",
                      "change_request": "change_applications.finished_at"}
            effective_at = observed.get("external_effect")
            first = (observed.get("downstream") or [{}])[0]
            out.append({"type": conversion.downstream_type, "id": request_id,
                        "state": observed["status"], "handoff": observed,
                        "effective_at": effective_at,
                        "effective_source": source.get(first.get("type"))
                        if effective_at else None,
                        "note": "Growth Action converted ≠ downstream approved ≠ "
                                "published / applied"})  # fmt: skip
        return out

    # -- internals -------------------------------------------------------------------------
    def _idempotency_key(self, row, review, target_workflow, *, handoff: bool = False) -> str:
        semantics = HANDOFF_SEMANTICS if handoff else INTERNAL_LINK_SEMANTICS
        digest = _sha({"schema": CONVERSION_SCHEMA, "semantics": semantics,
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

    def _current(self, row, now) -> dict | None:
        """いまの評価の、この候補 (同じ指紋) の行 (無ければ None)。"""

        box = self._inbox_builder(self._session, settings=self._settings, now=now)
        return next((e for e in box["entries"]
                     if e["candidate_fingerprint"] == row.candidate_fingerprint), None)

    def _checks(self, row, review, conversion, targets, now, existing, *, current=None,
                handoff_target=None, frozen=None) -> list[dict]:  # fmt: skip
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
        if handoff_target is None:
            check("frozen_targets", bool(targets), f"targets {targets}")
        check("still_observed", current is not None,
              "the current evaluation still produces this candidate" if current
              else "the current evaluation no longer produces this exact candidate")
        if current is not None:
            check("still_actionable", current["availability"] == gi.ACTIONABLE_NOW,
                  f"{current['availability']}: {'; '.join(current['availability_reasons'])}")
            reviewed = (list((review.snapshot_json or {}).get("blockers") or [])
                        if review else [])  # fmt: skip
            check("blockers_unchanged", list(current.get("blockers") or []) == reviewed,
                  f"now {current.get('blockers')}, reviewed {reviewed}")
        if handoff_target is not None:
            checks += self._handoff_checks(row, handoff_target, frozen or {})
            return checks
        conflicts = list(self._session.scalars(select(ChangeRequest.id).where(
            ChangeRequest.article_id == row.article_id,
            ChangeRequest.status.in_((*CR_OPEN_STATUSES, CR_APPROVED)))))  # fmt: skip
        check("no_conflicting_downstream_work", not conflicts,
              f"open change request(s) for the article: {conflicts}" if conflicts else "none")
        return checks

    # -- C9-B: 手元の依頼 ---------------------------------------------------------------------
    def _handoff_checks(self, row, target, frozen) -> list[dict]:
        """渡す先ごとの、実行の直前の重なり・前提の検査 (どれも fail closed)。"""

        checks = []

        def check(name, ok, detail=""):
            checks.append({"name": name, "ok": bool(ok), "detail": detail})

        ready = handoff_ready(self._session)
        check("handoff_table_ready", ready,
              "growth_handoff_requests exists" if ready else
              f"alembic revision {HANDOFF_REVISION} is not applied")
        handoffs = GrowthHandoffService(self._session)
        article = self._session.get(Article, row.article_id) if row.article_id else None
        if target in (TARGET_THREADS_GENERATION, TARGET_CHANGE_PREPARATION):
            published = (article is not None and str(article.status) == "published"
                         and bool(article.published_url))  # fmt: skip
            check("article_published", published,
                  f"article {row.article_id} is "
                  f"{article.status if article else 'missing'}"
                  + ("" if article is None or article.published_url else " (no published URL)"))
        if target == TARGET_THREADS_GENERATION:
            open_ = self._open_threads_proposals(row.article_id)
            check("no_open_threads_proposal", not open_,
                  f"open / approved-unpublished proposal(s) {open_}" if open_ else "none")
            pending = [r.id for r in handoffs.open_requests(GH_THREADS_GENERATION,
                                                            article_id=row.article_id)]
            check("no_open_targeted_request", not pending,
                  f"open targeted request(s) {pending}" if pending else "none")
            if frozen.get("lane") == GH_LANE_ALTERNATIVE_ANGLE:
                angle = frozen.get("requested_angle")
                used = set(frozen.get("angles_tried") or ()) | set(
                    self._published_angles(row.article_id))
                check("frozen_angle_untried", bool(angle) and angle not in used,
                      f"angle {angle}; already used {sorted(used)}" if angle else
                      "no recommended angle in the current evaluation")
        elif target == TARGET_ARTICLE_PLANNING:
            keyword = self._session.get(Keyword, row.keyword_id) if row.keyword_id else None
            check("keyword_present", keyword is not None,
                  f"keyword {row.keyword_id}" if keyword else
                  "the action has no scored keyword (cannot verify the planned article later)")
            if keyword is not None:
                existing = self._articles_for_keyword(keyword)
                check("no_existing_article", not existing,
                      f"non-archived article(s) for the keyword {existing}" if existing
                      else "none")
                pending = [r.id for r in handoffs.open_requests(GH_ARTICLE_PLANNING,
                                                                keyword_id=keyword.id)]
                check("no_open_planning_request", not pending,
                      f"open planning request(s) {pending}" if pending else "none")
                canni = frozen.get("cannibalization") or {}
                check("no_cannibalization", canni.get("ok") is True,
                      canni.get("detail") or "cannibalization could not be rechecked")
        elif target == TARGET_CHANGE_PREPARATION:
            conflicts = list(self._session.scalars(select(ChangeRequest.id).where(
                ChangeRequest.article_id == row.article_id,
                ChangeRequest.status.in_((*CR_OPEN_STATUSES, CR_APPROVED)))))  # fmt: skip
            check("no_conflicting_downstream_work", not conflicts,
                  f"open change request(s) for the article: {conflicts}" if conflicts
                  else "none")
            change_type = CHANGE_TYPE_FOR_ACTION[row.action_type]
            pending = [r.id for r in handoffs.open_requests(
                GH_CHANGE_PREPARATION, article_id=row.article_id, change_type=change_type)]
            check("no_open_preparation_request", not pending,
                  f"open {change_type} preparation request(s) {pending}" if pending else "none")
        return checks

    def _open_threads_proposals(self, article_id) -> list[int]:
        rows = self._session.scalars(select(ThreadsPostProposal).where(
            ThreadsPostProposal.source_article_id == article_id,
            ThreadsPostProposal.status.in_((*TP_OPEN_STATES, TP_APPROVED))))  # fmt: skip
        out = []
        for proposal in rows:
            if proposal.status == TP_APPROVED and self._published(proposal.id):
                continue
            out.append(proposal.id)
        return sorted(out)

    def _published(self, proposal_id: int) -> bool:
        return self._session.scalars(select(ThreadsPublication.id).where(
            ThreadsPublication.proposal_id == proposal_id,
            ThreadsPublication.status == PUB_PUBLISHED)).first() is not None  # fmt: skip

    def _published_angles(self, article_id) -> list[str]:
        rows = self._session.scalars(select(ThreadsPostProposal).where(
            ThreadsPostProposal.source_article_id == article_id))  # fmt: skip
        return sorted({p.angle for p in rows if self._published(p.id)})

    def _articles_for_keyword(self, keyword) -> list[int]:
        """同じキーワード (ID か、正規化した文字列が同じ) の、archived でない記事。"""

        norm = " ".join(str(keyword.keyword or "").lower().split())
        same = [k.id for k in self._session.scalars(select(Keyword))
                if " ".join(str(k.keyword or "").lower().split()) == norm]
        return sorted(a.id for a in self._session.scalars(select(Article).where(
            Article.keyword_id.in_(same))) if str(a.status) != "archived")  # fmt: skip

    def _freeze(self, row, review, target, current) -> dict:
        """依頼の時点の文脈 (後から変わっても、依頼が何を根拠にしたかが残る)。URL は入れない。"""

        base = {"schema": "growth-handoff-frozen/1", "growth_action_id": row.id,
                "opportunity_key": row.opportunity_key, "revision": row.revision,
                "candidate_fingerprint": row.candidate_fingerprint,
                "evidence_fingerprint": row.evidence_fingerprint,
                "review_id": review.id if review else None,
                "review_snapshot_hash": review.snapshot_hash if review else None,
                "blockers": list((current or {}).get("blockers") or []),
                "material": list((current or row.snapshot_json or {}).get("material") or [])}
        article = self._session.get(Article, row.article_id) if row.article_id else None
        if article is not None:
            base["article"] = {"id": article.id, "title": article.title,
                               "status": str(article.status),
                               "has_published_url": bool(article.published_url)}
        if target == TARGET_THREADS_GENERATION:
            alternative = row.action_type == ga.CREATE_THREADS_ALTERNATIVE_ANGLE
            recommendation = dict((current or {}).get("recommendation") or {})
            tried = sorted({a for m in base["material"] for a in (m.get("angles_tried") or ())})
            return {**base, "lane": GH_LANE_ALTERNATIVE_ANGLE if alternative else GH_LANE_REGULAR,
                    "requested_angle": recommendation.get("angle") if alternative else None,
                    "recommendation": recommendation, "angles_tried": tried}
        if target == TARGET_ARTICLE_PLANNING:
            keyword = self._session.get(Keyword, row.keyword_id) if row.keyword_id else None
            if keyword is not None:
                base["keyword"] = {"id": keyword.id, "keyword": keyword.keyword,
                                   "status": str(keyword.status)}
            base["cannibalization"] = self._cannibalization(keyword)
            return base
        source = source_hashes(article) if article is not None else {}
        change_type = CHANGE_TYPE_FOR_ACTION[row.action_type]
        if change_type == "affiliate_placement" and article is not None:
            source.update(self._placement_state(article.id))
        return {**base, "change_type": change_type, "source": source}

    def _cannibalization(self, keyword) -> dict:
        """既存の記事の計画 (読むだけ) で、重なりをもう一度確かめる。"""

        if keyword is None:
            return {"ok": False, "detail": "no keyword"}
        from app.services.article_plan_service import ArticlePlanService

        try:
            plan = ArticlePlanService(self._session).plan_for_keyword(keyword.id)
        except Exception as exc:  # noqa: BLE001 - 確かめられなければ止める
            return {"ok": False, "detail": f"article plan unavailable: {type(exc).__name__}"}
        info = plan.cannibalization
        live = [b for b in plan.production_blockers if b.startswith("live_article_exists")]
        needs_ack = bool(info.acknowledgment_required) or (
            "cannibalization" in plan.acknowledgements_required)
        detail = (f"live article: {live}" if live else
                  f"similar to keyword {info.most_similar_keyword_id} "
                  f"(similarity {info.max_similarity})" if needs_ack else
                  "no overlap requiring acknowledgement")
        return {"ok": not live and not needs_ack, "detail": detail,
                "originality": info.originality, "max_similarity": info.max_similarity,
                "most_similar_keyword_id": info.most_similar_keyword_id,
                "production_blockers": list(plan.production_blockers)}

    def _placement_state(self, article_id: int) -> dict:
        """アフィリエイトの配置の今の状態 (ID だけ。URL は入れない・推測しない)。"""

        from app.models import AffiliateLinkTarget, ArticleLinkSubstitutionMapping

        programs = list(self._session.scalars(select(ArticleAffiliateProgram).where(
            ArticleAffiliateProgram.article_id == article_id)))  # fmt: skip
        targets = self._session.scalars(select(AffiliateLinkTarget.id).where(
            AffiliateLinkTarget.article_id == article_id,
            AffiliateLinkTarget.status == "active"))  # fmt: skip
        mappings = self._session.scalars(select(ArticleLinkSubstitutionMapping.id).where(
            ArticleLinkSubstitutionMapping.article_id == article_id,
            ArticleLinkSubstitutionMapping.status == "active"))  # fmt: skip
        return {"program_ids": sorted(p.affiliate_program_id for p in programs),
                "primary_program_ids": sorted(p.affiliate_program_id for p in programs
                                              if p.is_primary),
                "active_target_ids": sorted(targets), "active_mapping_ids": sorted(mappings)}

    def _handoff_request(self, plan: dict, now: datetime) -> tuple[list[int], bool, list]:
        """手元の依頼を 1 つ作る (同じ変換なら同じ依頼)。**commit は呼ぶ側。**"""

        target = plan["target_workflow"]
        frozen = plan["frozen_preview"] or {}
        subject = plan["target_subject"]
        row, created = GrowthHandoffService(self._session).create(
            workflow=HANDOFF_WORKFLOW[target], idempotency_key=f"{plan['idempotency_key']}:h",
            growth_action_id=plan["growth_action_id"], review_id=plan["review_id"],
            candidate_fingerprint=plan["candidate_fingerprint"], frozen=frozen, now=now,
            article_id=subject.get("article_id"), keyword_id=subject.get("keyword_id"),
            lane=frozen.get("lane"), change_type=frozen.get("change_type"),
            requested_angle=frozen.get("requested_angle"))  # fmt: skip
        return [row.id], created, ["growth_handoff_requests"] if created else []

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
           "GrowthActionOutcomeService", "HANDOFF_SEMANTICS", "INTERNAL_LINK_SEMANTICS",
           "conversions_ready", "internal_link_targets"]
