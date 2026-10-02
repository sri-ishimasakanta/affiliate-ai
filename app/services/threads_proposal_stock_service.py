"""ThreadsProposalStockService -- Threads の投稿案の在庫を保守する (T6)。

流れ::

    在庫を数える → 作る必要があるか (理由つき) → 記事と切り口を選ぶ
    → T5.5 の参考つき prompt → provider へ依頼 → 出力を検査
    → awaiting_approval として保存 → (既存の T4.2 digest が人へ依頼)

**人の承認が防火壁である。** ここは提案を用意するだけで、承認・却下・公開・既存の
提案の編集・承認の取り消しは一切しない。保存は必ず ``ThreadsProposalService.persist``
を通り、状態は ``awaiting_approval`` になる。

安全の上限:

- 1 回の保守で保存する提案は最大 ``max_new_proposals_per_cycle`` (3 以下に固定)。
- 答えを待つ依頼があれば、新しい依頼は出さない。依頼の ID は決定的で、同じ依頼を
  二重に出さない。
- 来歴の列が無い DB (migration 前) では、依頼も保存もしない (PLAN は動く)。
- 失敗 (出力が壊れている・検査を通らない・保存できない) は、その依頼だけを止める。
  queue にも公開にも触れない。アラートは既存の cooldown つきの仕組みで、意味のある
  ときだけ (同じ問題を何度も通知しない)。

PLAN (``execute=False``) は DB にもファイルにも書かない。

**collect-only** (``collect_only=True``、T6.1): 既に出した依頼の答えを取り込むだけで、
その呼び出しでは新しい生成の依頼を **一切出さない** (provider の submit を呼ばない)。
在庫が下限より少なくても同じ。取り込みの検査・重複・保存・1 回 3 本の上限は通常と同じ。
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware
from app.models import (
    MA_PENDING,
    PUB_PUBLISHED,
    SUBJECT_THREADS_POST,
    TP_APPROVED,
    TP_AWAITING_APPROVAL,
    TP_OPEN_STATES,
    TP_REJECTED,
    TP_STALE,
    TP_SUPERSEDED,
    Article,
    MobileApprovalSession,
    ThreadsPostProposal,
    ThreadsPublication,
)
from app.operations.local_time import to_local
from app.operations.monitoring import AUTOMATION_HEALTH, AlertDraft
from app.operations.policy import get_policy as get_c8_policy
from app.services.threads_generation_provider import (
    GenerationRequest,
    ThreadsProposalGenerationProvider,
    build_provider,
)
from app.services.threads_proposal_service import ThreadsProposalError, ThreadsProposalService
from app.services.threads_queue_service import ThreadsQueueService
from app.social.threads.conversation import (
    BRIEF_VERSION,
    LEGACY,
    conversation_errors,
    hook_from_provenance,
)
from app.social.threads.fact_guard import fact_boundary_errors
from app.social.threads.performance_analysis import freeze_for_request
from app.social.threads.policy import ThreadsOperationsPolicy, get_operations_policy
from app.social.threads.prompt import parse_generated
from app.social.threads.quality import (
    QUALITY_VERSION,
    RECENT_WINDOW,
    hook_forms,
    money_figures,
    overlap,
    prose_length,
    quality_findings,
    recent_overlap,
    recent_topic_lines,
    topic_signature,
)
from app.social.threads.stock import (
    STATE_APPROVED_UNPUBLISHED,
    STATE_EXPIRED,
    STATE_HELD,
    STATE_PREPARED,
    STATE_PUBLISHED,
    STATE_REJECTED,
    STATE_REQUESTED,
    STATE_SCHEDULED,
    STATE_STALE,
    STATE_SUPERSEDED,
    ArticleFact,
    ProposalFact,
    StockFacts,
    plan_stock,
    request_id,
)
from app.social.threads.validators import normalized_identity

SCHEMA = "threads-proposal-stock/1"


def _overlap_audit(body: str, recent: list[dict]) -> dict:
    """最近の話題との比較の記録 (窓の大きさ・近い上位 3 件・止めたか・止めた相手)。"""

    compared = []
    for item in recent:
        result = overlap(body, item.get("text") or "")
        compared.append(
            {
                "ref": item.get("ref"),
                "signature": topic_signature(
                    article_id=item.get("article_id"),
                    angle=item.get("angle"),
                    link_mode=item.get("link_mode"),
                    body=item.get("text") or "",
                )["fingerprint"],
                **result,
            }
        )
    compared.sort(
        key=lambda c: (
            not c["high"], -len(c["shared_numbers"]), -len(c["shared_entities"]),
            -c["containment"], str(c["ref"]),
        )
    )  # fmt: skip
    blocking = next((c for c in compared if c["high"]), None)
    return {
        "recent_window": len(recent),
        "max_containment": max((c["containment"] for c in compared), default=0.0),
        "top": compared[:3],
        "blocked": blocking is not None,
        "blocked_by": blocking["ref"] if blocking else None,
        "reason": "recent_topic_overlap" if blocking else None,
    }


def _saved_hook(row) -> str | None:
    hook = hook_from_provenance(getattr(row, "learning_guidance_json", None))
    return None if hook == LEGACY else hook


ALERT_SOURCE = "threads_proposal_stock"


class ThreadsProposalStockService:
    def __init__(
        self,
        session: Session,
        *,
        settings,
        threads_service=None,
        policy: ThreadsOperationsPolicy | None = None,
        provider: ThreadsProposalGenerationProvider | None = None,
        proposal_service: ThreadsProposalService | None = None,
        timezone: ZoneInfo | None = None,
        alert_notifiers=None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._threads = threads_service
        self._policy = policy or get_operations_policy()
        self._provider = provider or build_provider(self._policy, settings)
        self._proposals = proposal_service or ThreadsProposalService(session)
        self._tz = timezone or get_c8_policy().timezone
        self._alert_notifiers = alert_notifiers
        self._audit: dict | None = None

    @property
    def provider(self) -> ThreadsProposalGenerationProvider:
        return self._provider

    # -- facts -------------------------------------------------------------------
    def facts(self, *, now: datetime) -> StockFacts:
        now = ensure_aware(now)
        # T6.3.3: 記事から作る提案・公開だけ。Growth Post (記事なし) は足し分なので、記事の在庫・
        # 計画 (切り口・リンクの割合・記事の間隔) に数えない。
        proposals = self._session.scalars(
            select(ThreadsPostProposal)
            .where(ThreadsPostProposal.source_article_id.is_not(None))
            .order_by(ThreadsPostProposal.id)
        ).all()
        publications = self._session.scalars(
            select(ThreadsPublication)
            .where(ThreadsPublication.source_article_id.is_not(None))
            .order_by(ThreadsPublication.id)
        ).all()
        published_ids = {p.proposal_id for p in publications if p.status == PUB_PUBLISHED}
        queue = ThreadsQueueService(
            self._session,
            settings=self._settings,
            threads_service=self._threads,
            policy=self._policy,
            timezone=self._tz,
        )
        candidates = {c.proposal_id: c for c in queue.facts(now=now).candidates}
        active = set(
            self._session.scalars(
                select(MobileApprovalSession.subject_id).where(
                    MobileApprovalSession.subject_type == SUBJECT_THREADS_POST,
                    MobileApprovalSession.state == MA_PENDING,
                    MobileApprovalSession.expires_at > now,
                )
            ).all()
        )
        articles = {
            a.id: a
            for a in self._session.scalars(
                select(Article).where(Article.status == "published").order_by(Article.id)
            ).all()
        }

        def topic_of(article_id):
            article = articles.get(article_id) or self._session.get(Article, article_id)
            keyword = article.keyword if article is not None else None
            return keyword.keyword if keyword is not None else None

        facts = []
        for row in proposals:
            facts.append(
                ProposalFact(
                    proposal_id=row.id,
                    article_id=row.source_article_id,
                    angle=row.angle,
                    link_mode=row.link_mode,
                    state=_state(row, candidates.get(row.id), published_ids, active, now),
                    created_at=ensure_aware(row.created_at),
                    topic=topic_of(row.source_article_id),
                    conversation_hook=_saved_hook(row),
                )
            )

        suppression = timedelta(
            days=float(self._policy.proposal_stock("source_angle_suppression_days", 30))
        )
        last_used: dict[int, datetime] = {}
        recent_angles: dict[int, set[str]] = defaultdict(set)
        for row in proposals:
            created = ensure_aware(row.created_at)
            last_used[row.source_article_id] = max(
                last_used.get(row.source_article_id, created), created
            )
            if row.status != TP_SUPERSEDED and now - created < suppression:
                recent_angles[row.source_article_id].add(row.angle)
        for row in publications:
            if row.published_at is not None:
                moment = ensure_aware(row.published_at)
                current = last_used.get(row.source_article_id)
                last_used[row.source_article_id] = max(current, moment) if current else moment

        article_facts = tuple(
            ArticleFact(
                article_id=a.id,
                title=a.title or "",
                topic=topic_of(a.id),
                has_url=bool(a.published_url),
                last_used_at=last_used.get(a.id),
                recent_angles=frozenset(recent_angles.get(a.id, ())),
            )
            for a in articles.values()
            if (a.body or "").strip()
        )
        by_proposal = {row.id: row for row in proposals}
        recent = tuple(
            (p.angle, getattr(by_proposal.get(p.proposal_id), "link_mode", "none"))
            for p in sorted(
                (p for p in publications if p.status == PUB_PUBLISHED and p.published_at),
                key=lambda p: ensure_aware(p.published_at),
                reverse=True,
            )
        )
        pending = [r for r in self._provider.pending() if not self._request_stale(r, now)]
        return StockFacts(
            now=now,
            proposals=tuple(facts),
            articles=article_facts,
            recent_publications=recent,
            pending_generation_requests=len(pending),
            targeted=self._targeted(),
        )

    # -- C9-B: 指定の依頼 ---------------------------------------------------------------
    def _targeted(self) -> tuple:
        """使ってよい指定の依頼 (方針のスイッチが無効・表が無ければ空: 今までと同じ)。"""

        from app.services.growth_handoff_service import (
            GrowthHandoffService,
            consume_targeted_enabled,
            handoff_ready,
        )
        from app.social.threads.stock import TargetedFact

        try:
            if not consume_targeted_enabled() or not handoff_ready(self._session):
                return ()
            return tuple(TargetedFact(request_id=r.id, article_id=r.article_id,
                                      angle=r.requested_angle, lane=r.lane or "regular")
                         for r in GrowthHandoffService(self._session).pending_targeted())
        except Exception:  # noqa: BLE001 - 指定の依頼が読めなくても在庫の保守は止めない
            return ()

    def _handoff(self, action: str, **kwargs) -> None:
        """指定の依頼の状態を進める (表が無い・指定の依頼で無ければ何もしない)。

        依頼の記録に失敗しても、在庫の保守 (提案の保存) は止めない (依頼はそのまま残る)。
        """

        from app.models.growth_handoff import GrowthHandoffRequest
        from app.services.growth_handoff_service import (
            GrowthHandoffError,
            GrowthHandoffService,
            handoff_ready,
        )

        if not handoff_ready(self._session):
            return
        try:
            getattr(GrowthHandoffService(self._session), action)(**kwargs)
        except GrowthHandoffError:
            self._session.rollback()
            return
        if any(isinstance(o, GrowthHandoffRequest) for o in self._session.dirty):
            self._session.commit()

    # -- plan ----------------------------------------------------------------------
    def plan(self, *, now: datetime | None = None, collect_only: bool = False) -> dict:
        """在庫の保守の計画。**何も書かない** (DB にもファイルにも)。

        ``collect_only`` のときは、依頼を出さないこと (``would_request = 0``) を明示する。
        """

        now = ensure_aware(now or datetime.now(UTC))
        guidance = self._proposals.learning_guidance(as_of=now)
        stock = plan_stock(self.facts(now=now), self._policy, guidance=guidance)
        availability = self._provider.availability()
        can_save = self._proposals.can_store_learning_provenance()
        blocked = list(stock.blocked_by)
        if stock.needs_generation and not availability.available:
            blocked.append(f"provider {availability.name} is unavailable: {availability.reason}")
        if (stock.needs_generation or self._provider.pending()) and not can_save:
            blocked.append(
                "saving is blocked until the production migration (alembic upgrade head)"
            )
        if collect_only:
            blocked.append(
                "collect-only: submitting new generation requests is suppressed for this invocation"
            )
        would_request = (
            stock.requested_count
            if availability.available and can_save and not stock.blocked_by
            else 0
        )
        if collect_only:
            would_request = 0
        pending = self._provider.pending()
        return {
            "schema": SCHEMA,
            "generated_at": now.isoformat(),
            "generated_at_local": to_local(now, self._tz).isoformat(timespec="minutes"),
            "policy_version": self._policy.policy_version,
            "stock": stock.as_dict(),
            "mode": "collect_only" if collect_only else "maintain",
            "submission_suppressed": collect_only,
            "blocked_by": blocked,
            "would_request": would_request,
            "would_collect": sum(1 for r in pending if self._provider.collect(r) is not None),
            "max_new_proposals_per_cycle": self._policy.max_new_proposals_per_cycle,
            "learning": {
                "mode": guidance.mode,
                "evidence_status": guidance.evidence_status,
                "fingerprint": guidance.fingerprint,
                "as_of": guidance.as_of,
                "learning_policy_version": guidance.source_policy_version,
            },
            "provider": availability.as_dict(),
            "pending_requests": [self._describe_request(r, now) for r in pending],
            "migration": {
                "learning_provenance_column": can_save,
                "can_save": can_save,
                "required_revision": "afc2f36bb3ca",
            },
            "last_status": self._provider.read_status(),
            "side_effects": _no_side_effects(),
        }

    # -- maintain ------------------------------------------------------------------
    def maintain(
        self,
        *,
        now: datetime | None = None,
        execute: bool = False,
        collect_only: bool = False,
    ) -> dict:
        """在庫を 1 回保守する。``execute=False`` は :meth:`plan` と同じ (何も書かない)。

        ``collect_only=True`` は、届いた答えの取り込みだけを行い、新しい依頼は出さない。
        """

        now = ensure_aware(now or datetime.now(UTC))
        if not execute:
            return {**self.plan(now=now, collect_only=collect_only), "executed": False}

        per_cycle = self._policy.max_new_proposals_per_cycle
        outcome = {
            "executed": True,
            "mode": "collect_only" if collect_only else "maintain",
            "submission_suppressed": collect_only,
            "created": [],
            "skipped": [],
            "failures": [],
            "requests_created": [],
            "stale_requests": [],
        }
        alerts: list[AlertDraft] = []
        can_save = self._proposals.can_store_learning_provenance()

        # 1) 答えの届いた依頼を取り込む。
        for request in self._provider.pending():
            output = self._provider.collect(request)
            if output is None:
                if self._request_stale(request, now):
                    self._provider.complete(
                        request, ok=False, outcome={"result": "stale", "at": now.isoformat()}
                    )
                    outcome["stale_requests"].append(request.request_id)
                    self._handoff("release", generation_request_id=request.request_id, now=now,
                                  note="the generation request went stale without an answer")
                    alerts.append(_alert_unanswered(request))
                continue
            if not can_save:
                outcome["skipped"].append(
                    {"request_id": request.request_id, "reason": "migration required"}
                )
                alerts.append(_alert_migration_required())
                continue
            room = per_cycle - len(outcome["created"])
            if room <= 0:
                outcome["skipped"].append(
                    {"request_id": request.request_id, "reason": "cycle bound reached"}
                )
                continue
            before = len(outcome["created"])
            self._ingest(request, output, now, room, outcome, alerts)
            self._handoff("proposal_created", generation_request_id=request.request_id,
                          proposal_ids=list(outcome["created"][before:]), now=now)

        # 2) まだ足りなければ、新しい依頼を出す (上限・待ち・migration を守る)。
        #    collect-only では、このブロックに入らない (submit を呼ぶ経路が無い)。
        plan = self.plan(now=now, collect_only=collect_only)
        outcome["plan"] = plan
        room = per_cycle - len(outcome["created"])
        if not collect_only and plan["would_request"] and room > 0:
            guidance_fp = plan["learning"]["fingerprint"]
            for planned in plan["stock"]["requests"][:room]:
                request = self._build_request(planned, now, guidance_fp)
                try:
                    output = self._provider.submit(request)
                except Exception as exc:  # provider の故障で保守全体を止めない
                    outcome["failures"].append(
                        {"request_id": request.request_id, "reason": f"provider failed: {exc}"}
                    )
                    alerts.append(_alert_provider_failed(str(exc)))
                    break
                outcome["requests_created"].append(request.request_id)
                if planned.get("targeted_request_id") is not None:
                    self._handoff("claim", request_id=planned["targeted_request_id"],
                                  generation_request_id=request.request_id, now=now)
                if output is not None:  # 同期の provider
                    room = per_cycle - len(outcome["created"])
                    if room > 0:
                        before = len(outcome["created"])
                        self._ingest_with_repair(request, output, now, room, outcome, alerts)
                        self._handoff("proposal_created", generation_request_id=request.request_id,
                                      proposal_ids=list(outcome["created"][before:]), now=now)

        if alerts:
            self._alert(alerts, now)
        status = {
            "schema": SCHEMA,
            "last_maintenance_at": now.isoformat(),
            "mode": outcome["mode"],
            "needs_generation": plan["stock"]["needs_generation"],
            "usable": plan["stock"]["usable"],
            "created": list(outcome["created"]),
            "requests_created": list(outcome["requests_created"]),
            "skipped": list(outcome["skipped"]),
            "failures": list(outcome["failures"]),
            "provider": plan["provider"],
            "learning": plan["learning"],
            "can_save": plan["migration"]["can_save"],
        }
        summary = getattr(self._provider, "generation_summary", None)
        if summary is not None:
            status["generation"] = summary()
        if outcome["created"] or outcome["requests_created"] or outcome["failures"]:
            status["last_generation_attempt_at"] = now.isoformat()
        previous = self._provider.read_status() or {}
        status.setdefault("last_generation_attempt_at", previous.get("last_generation_attempt_at"))
        self._provider.write_status(status)
        outcome["status"] = status
        return outcome

    def supersede_pending(
        self,
        request_id: str,
        *,
        reason: str,
        now: datetime | None = None,
        execute: bool = False,
    ) -> dict:
        """答え待ちの依頼を「置き換え済み (superseded)」として閉じる (監査つき・冪等)。

        prompt / schema の版が変わって古くなった依頼のため。**LLM を呼ばない・提案を作らない・
        承認も公開もしない。** 依頼と prompt のファイルは消さずに ``failed/`` へ移し、
        ``outcome.json`` に ``result: superseded``・理由・時刻を残す (取り込みに成功したとは
        記録しない)。``execute=False`` (既定) は何も変えずに、何が起きるかだけを返す。

        断るとき: 依頼が無い・答え (``response.json``) が取り込みを待っている・もう取り込み済み
        (``done/``)。もう置き換え済みなら何もしない (``already_superseded``)。
        """

        now = ensure_aware(now or datetime.now(UTC))
        reason = (reason or "").strip()
        base = {"request_id": request_id, "executed": False, "reason": reason}
        if not reason:
            return {**base, "result": "refused", "why": "a reason is required"}
        directory = getattr(self._provider, "directory", None)
        outcome_of = (
            (lambda folder: directory / folder / f"{request_id}.outcome.json")
            if directory is not None
            else (lambda folder: None)
        )
        done = outcome_of("done")
        failed = outcome_of("failed")
        if done is not None and done.exists():
            return {**base, "result": "refused", "why": "the request was already imported"}
        if failed is not None and failed.exists():
            previous = json.loads(failed.read_text(encoding="utf-8"))
            if previous.get("result") == "superseded":
                return {**base, "result": "already_superseded", "previous": previous}
            closed = previous.get("result")
            return {**base, "result": "refused", "why": f"already closed ({closed})"}
        request = next((r for r in self._provider.pending() if r.request_id == request_id), None)
        if request is None:
            return {**base, "result": "refused", "why": "no such pending request"}
        if self._provider.collect(request) is not None:
            return {
                **base,
                "result": "refused",
                "why": "a response is waiting to be imported; import or review it first",
            }
        record = {
            "result": "superseded",
            "reason": reason,
            "at": now.isoformat(),
            "had_conversation_hook": bool(getattr(request, "conversation_hook", None)),
        }
        if not execute:
            return {**base, "result": "would_supersede", "outcome": record}
        self._provider.complete(request, ok=False, outcome=record)
        return {**base, "executed": True, "result": "superseded", "outcome": record}

    def generate_pending(self, request_id: str, *, now: datetime | None = None) -> dict:
        """既にある依頼を、自動生成の provider で明示に 1 回だけ生成して取り込む。

        人の明示の指示のときだけ使う (本番の確認点)。provider の記録で冪等 (2 度送らない)。
        保存は必ず ``awaiting_approval``。承認・公開・digest の送信はしない。
        """

        now = ensure_aware(now or datetime.now(UTC))
        generate = getattr(self._provider, "generate_pending", None)
        outcome = {"executed": True, "mode": "generate_pending", "created": [], "skipped": [],
                   "failures": [], "requests_created": [], "stale_requests": []}  # fmt: skip
        request = next((r for r in self._provider.pending() if r.request_id == request_id), None)
        if generate is None or request is None:
            reason = "the provider cannot generate" if generate is None else "no such request"
            outcome["failures"].append({"request_id": request_id, "reason": reason})
            return outcome
        alerts: list[AlertDraft] = []
        output = self._provider.collect(request) or generate(request)
        if output is None:
            outcome["failures"].append(
                {"request_id": request_id, "reason": "no output (left as a manual request)"}
            )
        elif not self._proposals.can_store_learning_provenance():
            outcome["skipped"].append({"request_id": request_id, "reason": "migration required"})
        else:
            room = self._policy.max_new_proposals_per_cycle
            self._ingest_with_repair(request, output, now, room, outcome, alerts)
        if alerts:
            self._alert(alerts, now)
        return outcome

    # -- internals -------------------------------------------------------------------
    def _ingest_with_repair(self, request, output, now, room, outcome, alerts) -> None:
        """検査落ちなら、書き直せる provider に 1 回だけ書き直させる (上限は provider が守る)。"""

        repair = getattr(self._provider, "repair", None)
        note = getattr(self._provider, "record_validation", None)
        if repair is None:
            self._ingest(request, output, now, room, outcome, alerts)
            return
        # 1 回目は質の検査 (長さ・密度・きっかけの形・最近の話題) も書き直しの対象にする。
        self._audit = None
        problem = self._ingest(
            request, output, now, room, outcome, alerts, final=False, strict_quality=True
        )
        if note is not None:  # T6.3.1a: 呼び出しごとの検査の結果を残す
            note(request, ok=problem is None, reasons=problem, audit=self._audit)
        if problem is None:
            return
        repaired = repair(request, output, problem)
        if repaired is None:
            self._fail(request, outcome, problem)
            alerts.append(_alert_invalid_output(request.request_id, "invalid"))
            return
        # 書き直しの後: 好みの問題は警告として残す。最近の話題の重なりは保存しない。
        before = len(outcome["created"])
        self._audit = None
        self._ingest(request, repaired, now, room, outcome, alerts)
        if note is not None:
            ok = len(outcome["created"]) > before
            reason = next(
                (
                    f["reason"]
                    for f in reversed(outcome["failures"])
                    if f.get("request_id") == request.request_id
                ),
                None,
            )
            note(request, ok=ok, reasons=None if ok else reason, audit=self._audit)

    def _reject(self, request, outcome, alerts, reason: str, kind: str | None, final: bool):
        if not final:
            return reason
        self._fail(request, outcome, reason)
        if kind:
            alerts.append(_alert_invalid_output(request.request_id, kind))
        return None

    def _recent_items(self) -> list[dict]:
        """最近の提案 (承認待ち・承認済み = 公開済みを含む)。最大 ``RECENT_WINDOW`` 件。"""

        rows = self._session.scalars(
            select(ThreadsPostProposal)
            .where(
                ThreadsPostProposal.status.in_((TP_AWAITING_APPROVAL, TP_APPROVED)),
                # T6.3.3: 記事の投稿どうしだけで比べる (Growth Post は別に比べる)。
                ThreadsPostProposal.source_article_id.is_not(None),
            )
            .order_by(ThreadsPostProposal.created_at.desc(), ThreadsPostProposal.id.desc())
            .limit(RECENT_WINDOW)
        ).all()
        items = [
            {
                "ref": f"proposal #{row.id}",
                "text": row.content_text or "",
                "article_id": row.source_article_id,
                "angle": row.angle,
                "link_mode": row.link_mode,
            }
            for row in rows
        ]
        # manual-post coexistence: 人が Threads から出した投稿の話題も避ける (記事・切り口は不明)。
        items += [{"ref": m["ref"], "text": m["text"], "article_id": None, "angle": "unknown",
                   "link_mode": None} for m in manual_recent_items(self._session,
                                                                    limit=RECENT_WINDOW)]
        return items

    def _ingest(
        self, request, output, now, room, outcome, alerts, *, final=True, strict_quality=False
    ) -> str | None:
        """1 件の出力を検査して保存する。失敗はこの依頼だけを止める。

        ``final=False`` のときは、検査落ちを記録せずに理由を返す (書き直しの前)。
        """

        rid = request.request_id
        try:
            items = parse_generated(output)
        except (ValueError, json.JSONDecodeError) as exc:
            return self._reject(
                request, outcome, alerts, f"malformed output: {exc}", "malformed", final
            )
        brief = None
        wanted_hook = getattr(request, "conversation_hook", None)
        if wanted_hook is not None:
            problems, style_warnings = [], []
            for item in items:
                got = item.get("conversation_hook")
                if got != wanted_hook:
                    problems.append(
                        f"conversation_hook {got!r} does not match the requested {wanted_hook!r}"
                    )
                errors, warnings = conversation_errors(item["body"], wanted_hook)
                problems += errors
                style_warnings += warnings
                # T6.3.1a: 計画の link_mode は拘束 (モデルに変えさせない)。
                wanted_link = request.link_mode
                got_link = item.get("link_mode") or "none"
                if got_link != wanted_link:
                    problems.append(
                        f"link_mode {got_link!r} does not match the requested {wanted_link!r}; "
                        f"keep link_mode={wanted_link}"
                    )
                if wanted_link == "none" and "{link}" in item["body"]:
                    problems.append("link_mode none must not contain {link}; remove the link")
                if wanted_link == "article" and item["body"].count("{link}") > 1:
                    problems.append(
                        "link_mode article must contain {link} at most once; keep one {link}"
                    )
            if problems:
                return self._reject(
                    request, outcome, alerts,
                    "conversation check failed: " + "; ".join(sorted(set(problems))),
                    "invalid", final,
                )  # fmt: skip
            # 設定の欠けた自動 provider (misconfigured) の答えは人が書いたもの: 重なりは警告。
            automatic = getattr(self._provider, "automatic", False) and (
                getattr(self._provider, "mode", "automatic") == "automatic"
            )
            recent = self._recent_items()
            quality_problems = []
            overlap_audit = []
            for item in items:
                overlap_audit.append(_overlap_audit(item["body"], recent))
                soft, warns = quality_findings(item["body"], wanted_hook)
                style_warnings += warns
                if strict_quality:
                    quality_problems += soft
                else:
                    style_warnings += [f"quality: {s}" for s in soft]
                hit = recent_overlap(item["body"], recent)
                if hit:
                    message = (
                        f"recent topic overlap with {hit['ref']} (products "
                        f"{', '.join(hit['shared_entities']) or '-'}, facts "
                        f"{', '.join(hit['shared_numbers']) or '-'}, axes "
                        f"{', '.join(hit['shared_axes']) or '-'}); choose a different main point "
                        "from the same article, keeping the same conversation_hook and link_mode"
                    )
                    if strict_quality or automatic:
                        quality_problems.append(message)
                    else:
                        style_warnings.append(f"quality: {message}")
            self._audit = {"overlap": overlap_audit[0] if overlap_audit else None}
            if quality_problems:
                return self._reject(
                    request, outcome, alerts,
                    "quality check failed: " + "; ".join(sorted(set(quality_problems))),
                    "invalid", final,
                )  # fmt: skip
            first = items[0]["body"]
            brief = {
                "brief_version": BRIEF_VERSION,
                "conversation_hook": wanted_hook,
                "warnings": sorted(set(style_warnings)),
                "quality": {
                    "version": QUALITY_VERSION,
                    "prose_length": prose_length(first),
                    "money_figures": money_figures(first),
                    "hook_forms": sorted(hook_forms(first)),
                },
                "topic_signature": topic_signature(
                    article_id=request.article_id,
                    angle=items[0]["angle"],
                    link_mode=items[0].get("link_mode"),
                    body=first,
                ),
                "overlap": overlap_audit[0] if overlap_audit else None,
            }
        if getattr(self._provider, "automatic", False):
            article = self._session.get(Article, request.article_id)
            article_text = f"{getattr(article, 'title', '')}\n{getattr(article, 'body', '')}"
            problems = sorted(
                {e for item in items for e in fact_boundary_errors(item["body"], article_text)}
            )
            if problems:
                return self._reject(
                    request, outcome, alerts,
                    "generated output failed the fact boundary: " + "; ".join(problems),
                    "invalid", final,
                )  # fmt: skip

        kept, seen = [], set()
        for item in items:
            if item["angle"] in request.angles and item["angle"] not in seen and len(kept) < room:
                kept.append(item)
                seen.add(item["angle"])
            else:
                outcome["skipped"].append(
                    {"request_id": rid, "reason": "outside the request (angle or cycle bound)"}
                )
        if not kept:
            return self._reject(
                request, outcome, alerts, "no proposal matched the requested angle(s)",
                "unmatched", final,
            )  # fmt: skip

        trimmed = json.dumps({"proposals": kept}, ensure_ascii=False)
        as_of = datetime.fromisoformat(request.learning_as_of)
        try:
            prepared = self._proposals.plan(
                article_id=request.article_id,
                generated_output=trimmed,
                learning_as_of=as_of,
                expected_guidance=request.guidance_fingerprint,
            )
            for rejected in prepared.rejected:
                outcome["skipped"].append(
                    {"request_id": rid, "reason": "; ".join(rejected["errors"])}
                )
            # T2 の重複判定は同じ記事の中だけを見る。在庫の保守では、別の記事でも同じ
            # 本文の案 (生きている提案・公開済み) を重ねて作らない (決定的な正規形で比べる)。
            live = self._live_identities()
            duplicates = {
                c["angle"]: live[normalized_identity(c["publish_text"])]
                for c in prepared.candidates
                if normalized_identity(c["publish_text"]) in live
            }
            for existing in duplicates.values():
                outcome["skipped"].append(
                    {
                        "request_id": rid,
                        "reason": f"an identical proposal already exists ({existing})",
                    }
                )
            kept = [item for item in kept if item["angle"] not in duplicates]
            usable = [c for c in prepared.candidates if c["angle"] not in duplicates]
            if not usable:
                # 重複だけで止まったなら、運用上の問題ではない (通知しない)。
                duplicate_only = all(
                    any("already exists" in e or "duplicate" in e for e in r["errors"])
                    for r in prepared.rejected
                )
                reasons = [e for r in prepared.rejected for e in r["errors"]] + [
                    s["reason"] for s in outcome["skipped"] if s.get("request_id") == rid
                ]
                return self._reject(
                    request, outcome, alerts,
                    "no candidate passed validation or dedup: " + "; ".join(reasons)[:600],
                    None if duplicate_only else "invalid", final,
                )  # fmt: skip
            trimmed = json.dumps({"proposals": kept}, ensure_ascii=False)
            rows = self._proposals.persist(
                article_id=request.article_id,
                generated_output=trimmed,
                now=now,
                learning_as_of=as_of,
                expected_guidance=request.guidance_fingerprint,
                generation_brief=brief,
                frozen_feedback=request.performance_feedback,
                request_prompt=request.prompt or None,
            )
        except ThreadsProposalError as exc:
            self._session.rollback()
            return self._reject(request, outcome, alerts, exc.reason, "refused", final)
        except SQLAlchemyError as exc:
            # 保存できなかった: 何も承認されない。依頼は残し、次の保守で再試行する。
            self._session.rollback()
            outcome["failures"].append(
                {"request_id": rid, "reason": f"save failed: {type(exc).__name__}"}
            )
            alerts.append(_alert_save_failed(type(exc).__name__))
            return None
        created = [row.id for row in rows]
        outcome["created"].extend(created)
        self._provider.complete(
            request,
            ok=True,
            outcome={"result": "stored", "proposal_ids": created, "at": now.isoformat()},
        )
        return None

    def _live_identities(self) -> dict[str, str]:
        """生きている提案 (承認待ち・承認済み)・公開済み・manual の自分の投稿の本文の正規形 →
        その参照 (``#12`` / ``manual Threads post <id>``)。"""

        rows = self._session.execute(
            select(ThreadsPostProposal.id, ThreadsPostProposal.content_text).where(
                ThreadsPostProposal.status.in_((*TP_OPEN_STATES, TP_APPROVED))
            )
        ).all()
        identities = {normalized_identity(text_): f"#{pid}" for pid, text_ in rows}
        for pid, text_ in self._session.execute(
            select(ThreadsPublication.proposal_id, ThreadsPublication.exact_published_text)
        ).all():
            if text_:
                identities.setdefault(normalized_identity(text_), f"#{pid}")
        for item in manual_recent_items(self._session, limit=RECENT_WINDOW):
            identities.setdefault(normalized_identity(item["text"]), item["ref"])
        return identities

    def _fail(self, request, outcome, reason: str) -> None:
        outcome["failures"].append({"request_id": request.request_id, "reason": reason})
        self._provider.complete(request, ok=False, outcome={"result": "failed", "reason": reason})

    def _build_request(self, planned: dict, now: datetime, guidance_fp: str) -> GenerationRequest:
        package = self._proposals.build_prompt(
            article_id=planned["article_id"],
            angles=[planned["angle"]],
            learning_as_of=now,
            requested_link_mode=planned["link_mode"],
            conversation_hook=planned.get("conversation_hook"),
            recent_topics=(
                recent_topic_lines(self._recent_items())
                if planned.get("conversation_hook")
                else None
            ),
        )
        return GenerationRequest(
            request_id=request_id(
                article_id=planned["article_id"],
                angle=planned["angle"],
                link_mode=planned["link_mode"],
                fingerprint=package.guidance.fingerprint,
                as_of=now,
            ),
            article_id=planned["article_id"],
            article_title=planned["article_title"],
            angles=(planned["angle"],),
            link_mode=planned["link_mode"],
            learning_as_of=now.isoformat(),
            guidance_fingerprint=package.guidance.fingerprint,
            guidance_mode=package.guidance.mode,
            prompt_hash=package.prompt_hash,
            created_at=now.isoformat(),
            reasons=tuple(planned["reasons"]),
            prompt=package.rendered_prompt,
            conversation_hook=planned.get("conversation_hook"),
            # T6.5: prompt に入れた参考をこの時点で固定する (保存の時に照合する)。
            performance_feedback=freeze_for_request(package.performance_feedback),
        )

    def _request_stale(self, request: GenerationRequest, now: datetime) -> bool:
        hours = float(self._policy.proposal_stock("request_stale_after_hours", 72))
        created = ensure_aware(datetime.fromisoformat(request.created_at))
        return now - created > timedelta(hours=hours)

    def _describe_request(self, request: GenerationRequest, now: datetime) -> dict:
        return {
            "request_id": request.request_id,
            "article_id": request.article_id,
            "angles": list(request.angles),
            "link_mode": request.link_mode,
            "created_at": request.created_at,
            "has_response": self._provider.collect(request) is not None,
            "stale": self._request_stale(request, now),
        }

    def _alert(self, drafts: list[AlertDraft], now: datetime) -> None:
        from app.operations.notifications import build_notifiers
        from app.services.operations_alert_service import OperationsAlertService

        unique = list({d.fingerprint: d for d in drafts}.values())
        notifiers = (
            self._alert_notifiers
            if self._alert_notifiers is not None
            else build_notifiers(self._settings)
        )
        OperationsAlertService(
            self._session, policy=get_c8_policy(), notifiers=notifiers
        ).record_and_notify(unique, now=now)


def _state(row, candidate, published_ids, active, now) -> str:
    """1 件の提案を在庫の区分 1 つに決める (上から順)。"""

    if row.id in published_ids or (
        candidate and (candidate.already_published or candidate.in_flight)
    ):
        return STATE_PUBLISHED
    if row.status == TP_REJECTED:
        return STATE_REJECTED
    if row.status == TP_SUPERSEDED:
        return STATE_SUPERSEDED
    if row.status == TP_STALE or (
        candidate and (candidate.stale_reasons or candidate.integrity_reasons)
    ):
        return STATE_STALE
    expires = candidate.expires_at if candidate else None
    if expires is not None and expires <= now:
        return STATE_EXPIRED
    if candidate and candidate.held:
        return STATE_HELD
    if row.status == TP_APPROVED:
        not_before = candidate.not_before if candidate else None
        return STATE_SCHEDULED if not_before and not_before > now else STATE_APPROVED_UNPUBLISHED
    if row.id in active:
        return STATE_REQUESTED
    if row.status in TP_OPEN_STATES:
        return STATE_PREPARED
    return STATE_STALE


def _no_side_effects() -> dict:
    return {
        "database_writes": 0,
        "files_written": 0,
        "threads_calls": 0,
        "emails": 0,
        "approvals": 0,
        "publications": 0,
    }


def _draft(kind: str, severity: str, title: str, summary: str, evidence: dict) -> AlertDraft:
    return AlertDraft(
        alert_type=AUTOMATION_HEALTH,
        severity=severity,
        source=ALERT_SOURCE,
        title=title,
        summary=summary,
        fingerprint=f"{ALERT_SOURCE}:{kind}",
        evidence=evidence,
    )


def _alert_invalid_output(request_id: str, why: str) -> AlertDraft:
    return _draft(
        "invalid_output",
        "warning",
        "Threads 投稿案の生成結果を取り込めなかった",
        "生成結果が壊れているか、検査を通らなかった。依頼は failed/ に移した "
        "(公開・承認への影響なし)。",
        {"request_id": request_id, "why": why},
    )


def _alert_save_failed(error: str) -> AlertDraft:
    return _draft(
        "save_failed",
        "error",
        "Threads 投稿案を保存できなかった",
        "DB への保存に失敗した。何も承認されていない。依頼は次の保守で再試行する。",
        {"error": error},
    )


def _alert_migration_required() -> AlertDraft:
    return _draft(
        "migration_required",
        "warning",
        "Threads 投稿案を保存するには migration が必要",
        "生成結果が届いているが、learning_guidance_json の列が無い。"
        "`uv run alembic upgrade head` の後に取り込む。",
        {"required_revision": "afc2f36bb3ca"},
    )


def _alert_unanswered(request: GenerationRequest) -> AlertDraft:
    return _draft(
        "request_unanswered",
        "warning",
        "Threads 投稿案の生成依頼に答えが無いまま期限を過ぎた",
        "依頼を failed/ に移した。次の保守で必要なら新しく依頼する。",
        {"request_id": request.request_id, "article_id": request.article_id},
    )


def _alert_provider_failed(reason: str) -> AlertDraft:
    return _draft(
        "provider_failed",
        "warning",
        "Threads 投稿案の生成 provider が失敗した",
        "依頼を出せなかった。在庫・承認・公開への影響はない。",
        {"reason": reason[:300]},
    )


__all__ = ["SCHEMA", "ThreadsProposalStockService"]


def manual_recent_items(session, *, limit: int) -> list[dict]:
    """manual / unknown の自分の投稿の本文 (新しい順)。migration の前の DB では空。

    manual-post coexistence: 人が Threads から出した投稿も、重複・話題・言い回しの判定に入れる
    (本数・承認・公開の成功には入れない)。
    """

    from app.services.threads_account_post_service import (
        ThreadsAccountPostService,
        account_posts_ready,
    )

    if not account_posts_ready(session):
        return []
    return ThreadsAccountPostService(session).recent_texts(limit=limit)
