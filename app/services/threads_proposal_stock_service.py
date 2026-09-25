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
from app.social.threads.policy import ThreadsOperationsPolicy, get_operations_policy
from app.social.threads.prompt import parse_generated
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
        self._provider = provider or build_provider(self._policy)
        self._proposals = proposal_service or ThreadsProposalService(session)
        self._tz = timezone or get_c8_policy().timezone
        self._alert_notifiers = alert_notifiers

    @property
    def provider(self) -> ThreadsProposalGenerationProvider:
        return self._provider

    # -- facts -------------------------------------------------------------------
    def facts(self, *, now: datetime) -> StockFacts:
        now = ensure_aware(now)
        proposals = self._session.scalars(
            select(ThreadsPostProposal).order_by(ThreadsPostProposal.id)
        ).all()
        publications = self._session.scalars(
            select(ThreadsPublication).order_by(ThreadsPublication.id)
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
        )

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
            self._ingest(request, output, now, room, outcome, alerts)

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
                if output is not None:  # 同期の provider
                    room = per_cycle - len(outcome["created"])
                    if room > 0:
                        self._ingest(request, output, now, room, outcome, alerts)

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
        if outcome["created"] or outcome["requests_created"] or outcome["failures"]:
            status["last_generation_attempt_at"] = now.isoformat()
        previous = self._provider.read_status() or {}
        status.setdefault("last_generation_attempt_at", previous.get("last_generation_attempt_at"))
        self._provider.write_status(status)
        outcome["status"] = status
        return outcome

    # -- internals -------------------------------------------------------------------
    def _ingest(self, request, output, now, room, outcome, alerts) -> None:
        """1 件の出力を検査して保存する。失敗はこの依頼だけを止める。"""

        rid = request.request_id
        try:
            items = parse_generated(output)
        except (ValueError, json.JSONDecodeError) as exc:
            self._fail(request, outcome, f"malformed output: {exc}")
            alerts.append(_alert_invalid_output(rid, "malformed"))
            return

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
            self._fail(request, outcome, "no proposal matched the requested angle(s)")
            alerts.append(_alert_invalid_output(rid, "unmatched"))
            return

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
                        "reason": f"an identical proposal already exists (#{existing})",
                    }
                )
            kept = [item for item in kept if item["angle"] not in duplicates]
            usable = [c for c in prepared.candidates if c["angle"] not in duplicates]
            if not usable:
                self._fail(request, outcome, "no candidate passed validation or dedup")
                # 重複だけで止まったなら、運用上の問題ではない (通知しない)。
                duplicate_only = all(
                    any("already exists" in e or "duplicate" in e for e in r["errors"])
                    for r in prepared.rejected
                )
                if not duplicate_only:
                    alerts.append(_alert_invalid_output(rid, "invalid"))
                return
            trimmed = json.dumps({"proposals": kept}, ensure_ascii=False)
            rows = self._proposals.persist(
                article_id=request.article_id,
                generated_output=trimmed,
                now=now,
                learning_as_of=as_of,
                expected_guidance=request.guidance_fingerprint,
            )
        except ThreadsProposalError as exc:
            self._session.rollback()
            self._fail(request, outcome, exc.reason)
            alerts.append(_alert_invalid_output(rid, "refused"))
            return
        except SQLAlchemyError as exc:
            # 保存できなかった: 何も承認されない。依頼は残し、次の保守で再試行する。
            self._session.rollback()
            outcome["failures"].append(
                {"request_id": rid, "reason": f"save failed: {type(exc).__name__}"}
            )
            alerts.append(_alert_save_failed(type(exc).__name__))
            return
        created = [row.id for row in rows]
        outcome["created"].extend(created)
        self._provider.complete(
            request,
            ok=True,
            outcome={"result": "stored", "proposal_ids": created, "at": now.isoformat()},
        )

    def _live_identities(self) -> dict[str, int]:
        """生きている提案 (承認待ち・承認済み) と公開済みの本文の正規形 → 提案 ID。"""

        rows = self._session.execute(
            select(ThreadsPostProposal.id, ThreadsPostProposal.content_text).where(
                ThreadsPostProposal.status.in_((*TP_OPEN_STATES, TP_APPROVED))
            )
        ).all()
        identities = {normalized_identity(text_): pid for pid, text_ in rows}
        for pid, text_ in self._session.execute(
            select(ThreadsPublication.proposal_id, ThreadsPublication.exact_published_text)
        ).all():
            if text_:
                identities.setdefault(normalized_identity(text_), pid)
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
