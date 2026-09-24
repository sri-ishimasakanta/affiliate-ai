"""ThreadsApprovalDigestService -- 承認依頼のまとめ送り (T4.2)。

在庫にある提案から「いま人に承認を求める価値があるもの」を選び、**1 通の
メール** にまとめて届ける。

- 提案づくりとは別。提案の文面は読むだけで、書き換えない。
- 1 通に入る提案は通常 5 件まで。残りは在庫に残し、次の機会に回す。
- 提案ごとにレビューセッション (capability) を 1 つずつ作る。**決定は提案ごとに独立**
  しており、1 件を承認・却下しても他には影響しない。一括承認は無い。
- 期限 (TTL) は **送信の操作の時刻** から数える。提案を作った時刻からは数えない。
- メールが届かなければ、作ったセッションをすべて失効させ、提案は在庫に戻す
  (``approval_request_sent_at`` は書かない)。
- 期限切れや stale になった提案の依頼は、その 1 件だけを失効させる。
- **承認は公開ではない。** ここから T3 の公開は決して呼ばれない。

既定は PLAN で、何も書かず、何も送らない。送るのは ``send(execute=True)`` を
人が明示したとき (または worker を ``--send-approval-digests`` で起動したとき) だけ。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware, to_storage_utc
from app.models import (
    DIGEST_FAILED,
    DIGEST_SENT,
    MA_PENDING,
    SUBJECT_THREADS_POST,
    TP_APPROVED,
    TP_OPEN_STATES,
    Article,
    MobileApprovalSession,
    ThreadsApprovalDigest,
    ThreadsPostProposal,
    ThreadsPublication,
)
from app.operations.local_time import to_local
from app.operations.policy import get_policy as get_c8_policy
from app.services.mobile_approval_service import MobileApprovalError, MobileApprovalService
from app.services.threads_proposal_service import ThreadsProposalService
from app.services.threads_publication_service import ThreadsPublicationService
from app.social.threads.digest import DigestCandidate, DigestPlan, plan_digest
from app.social.threads.policy import ThreadsOperationsPolicy
from app.social.threads.policy import get_operations_policy as get_threads_operations_policy
from app.social.threads.proposal import canonical_identity


@dataclass
class DigestOutcome:
    executed: bool
    sent: bool
    digest_id: int | None = None
    proposal_ids: list[int] = field(default_factory=list)
    session_ids: list[int] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    revoked_session_ids: list[int] = field(default_factory=list)
    expired_session_ids: list[int] = field(default_factory=list)
    sent_at: datetime | None = None
    reason: str | None = None
    threads_writes: int = 0

    def as_dict(self) -> dict:
        return {
            "executed": self.executed,
            "sent": self.sent,
            "digest_id": self.digest_id,
            "proposal_ids": list(self.proposal_ids),
            "session_ids": list(self.session_ids),
            "skipped": list(self.skipped),
            "revoked_session_ids": list(self.revoked_session_ids),
            "expired_session_ids": list(self.expired_session_ids),
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
            "reason": self.reason,
            "threads_writes": self.threads_writes,
        }


class ThreadsApprovalDigestService:
    def __init__(
        self,
        session: Session,
        *,
        settings,
        mobile_approval: MobileApprovalService | None = None,
        notifier=None,
        relay_client=None,
        policy: ThreadsOperationsPolicy | None = None,
        timezone: ZoneInfo | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._mobile = mobile_approval or MobileApprovalService(
            session, settings=settings, relay_client=relay_client, notifier=notifier
        )
        self._policy = policy or get_threads_operations_policy()
        self._tz = timezone or get_c8_policy().timezone
        self._publications = ThreadsPublicationService(session, settings=settings)
        self._proposals = ThreadsProposalService(session)

    @property
    def timezone(self) -> ZoneInfo:
        return self._tz

    # -- plan ------------------------------------------------------------------
    def plan(self, *, now: datetime | None = None) -> DigestPlan:
        """いま送るなら何を送るか。**何も書かず、何も送らない。**"""

        now = ensure_aware(now or datetime.now(UTC))
        return plan_digest(
            self._candidates(now),
            now=now,
            last_sent_at=self._last_sent_at(),
            queued_identities=self._queued_identities(),
            approved_unpublished=self._approved_unpublished(),
            policy=self._policy,
            tz=self._tz,
        )

    def housekeeping(self, *, now: datetime | None = None, execute: bool = False) -> dict:
        """期限を過ぎた依頼と、無効になった提案の依頼を片付ける。

        - 期限を過ぎた pending のセッション → expired (提案は再依頼できる)。
        - 提案が期限切れ・stale になった pending のセッション → **その 1 件だけ** 失効。
          同じ digest の他の提案のセッションには触れない。
        """

        now = ensure_aware(now or datetime.now(UTC))
        lapsed: list[int] = []
        invalid: list[dict] = []
        for row in self._pending_sessions():
            if ensure_aware(row.expires_at) <= now:
                lapsed.append(row.id)
                continue
            proposal = self._session.get(ThreadsPostProposal, row.subject_id)
            reasons = self._invalid_reasons(proposal, now)
            if reasons:
                invalid.append(
                    {"session_id": row.id, "proposal_id": row.subject_id, "reasons": reasons}
                )
        if execute:
            for session_id in lapsed:
                self._mobile.expire(session_id=session_id, reason="the review session expired")
            for item in invalid:
                self._mobile.revoke(
                    session_id=item["session_id"], reason="; ".join(item["reasons"])[:200]
                )
        return {"executed": execute, "expired_session_ids": lapsed, "revoke": invalid}

    # -- send ------------------------------------------------------------------
    def send(self, *, now: datetime | None = None, execute: bool = False) -> DigestOutcome:
        """期限が来ていれば、1 通だけ送る。``execute=False`` では何もしない。"""

        now = ensure_aware(now or datetime.now(UTC))
        outcome = DigestOutcome(executed=execute, sent=False)
        if not execute:
            plan = self.plan(now=now)
            outcome.reason = "plan only" if plan.would_send else ",".join(plan.waiting_for)
            outcome.proposal_ids = [i.candidate.proposal_id for i in plan.selected]
            return outcome

        cleanup = self.housekeeping(now=now, execute=True)
        outcome.expired_session_ids = list(cleanup["expired_session_ids"])
        outcome.revoked_session_ids = [item["session_id"] for item in cleanup["revoke"]]

        plan = self.plan(now=now)
        if not plan.would_send:
            outcome.reason = ",".join(plan.waiting_for) or "nothing is due"
            return outcome

        digest = ThreadsApprovalDigest(
            outcome=DIGEST_FAILED,
            failure_reason="sending",
            policy_version=self._policy.policy_version,
            issued_at=to_storage_utc(now),
            selected_json=[
                {"proposal_id": i.candidate.proposal_id, "reason": i.reason} for i in plan.selected
            ],
            suppressed_json=[
                {"proposal_id": i.candidate.proposal_id, "reason": i.reason}
                for i in (*plan.suppressed, *plan.deferred)
            ]
            or None,
        )
        self._session.add(digest)
        self._session.commit()
        self._session.refresh(digest)
        outcome.digest_id = digest.id

        items: list[dict] = []
        for item in plan.selected:
            candidate = item.candidate
            try:
                issued = self._mobile.issue(
                    subject_type=SUBJECT_THREADS_POST,
                    subject_id=candidate.proposal_id,
                    ttl_hours=self._policy.approval_ttl_hours,
                    now=now,
                    approval_digest_id=digest.id,
                )
            except MobileApprovalError as exc:
                # 1 件を作れなくても、他の提案の依頼は続ける。
                outcome.skipped.append({"proposal_id": candidate.proposal_id, "reason": exc.reason})
                continue
            items.append(
                {
                    "session": issued.row,
                    "proposal_id": candidate.proposal_id,
                    "article_title": candidate.article_title,
                    "angle": candidate.angle,
                    "preview": candidate.preview,
                    "timing": self._timing_text(candidate),
                    "review_url": issued.review_url,
                }
            )

        if not items:
            digest.failure_reason = "no review session could be issued"
            self._session.commit()
            outcome.reason = digest.failure_reason
            return outcome

        rows = [item["session"] for item in items]
        expires_at = min(ensure_aware(row.expires_at) for row in rows)
        from app.services.operations_notification_service import OperationsNotificationService

        delivery = OperationsNotificationService(
            self._session, settings=self._settings, notifier=self._mobile.notifier
        ).send_approval_digest(digest_id=digest.id, items=items, expires_at=expires_at)
        for row in rows:
            row.notification_delivery_id = delivery.delivery_id
        self._session.commit()

        sent_at = self._mobile.mark_request_sent(rows, delivery_id=delivery.delivery_id)
        if sent_at is None:
            # 届かなかった。誰も知らないセッションを生かしておかない。提案は在庫に戻る。
            for row in rows:
                self._mobile.revoke(session_id=row.id, reason="the digest email was not delivered")
                outcome.revoked_session_ids.append(row.id)
            digest.failure_reason = delivery.reason or "the digest email was not delivered"
            digest.notification_delivery_id = delivery.delivery_id
            self._session.commit()
            outcome.reason = digest.failure_reason
            return outcome

        digest.outcome = DIGEST_SENT
        digest.failure_reason = None
        digest.sent_at = to_storage_utc(sent_at)
        digest.notification_delivery_id = delivery.delivery_id
        self._session.commit()
        outcome.sent = True
        outcome.sent_at = sent_at
        outcome.proposal_ids = [item["proposal_id"] for item in items]
        outcome.session_ids = [row.id for row in rows]
        return outcome

    # -- facts -----------------------------------------------------------------
    def _candidates(self, now: datetime) -> list[DigestCandidate]:
        active = {
            row.subject_id for row in self._pending_sessions() if ensure_aware(row.expires_at) > now
        }
        rows = self._session.scalars(
            select(ThreadsPostProposal)
            .where(ThreadsPostProposal.status.in_(tuple(TP_OPEN_STATES)))
            .order_by(ThreadsPostProposal.id)
        ).all()
        out: list[DigestCandidate] = []
        for proposal in rows:
            article = self._session.get(Article, proposal.source_article_id)
            _stale, stale_reasons = self._proposals.evaluate_staleness(proposal)
            assessment = self._publications.assess(proposal, article=article)
            out.append(
                DigestCandidate(
                    proposal_id=proposal.id,
                    source_article_id=proposal.source_article_id,
                    article_title=getattr(article, "title", "") or "",
                    angle=proposal.angle,
                    ready_at=ensure_aware(proposal.created_at),
                    identity=canonical_identity(proposal.content_text),
                    preview=_preview(proposal.content_text, self._policy.digest_preview_characters),
                    not_before=_aware(proposal.not_before),
                    expires_at=_aware(proposal.expires_at),
                    held=proposal.held_at is not None,
                    stale_reasons=tuple(stale_reasons),
                    integrity_reasons=assessment.integrity_reasons,
                    has_active_request=proposal.id in active,
                )
            )
        return out

    def _pending_sessions(self) -> list[MobileApprovalSession]:
        return list(
            self._session.scalars(
                select(MobileApprovalSession)
                .where(
                    MobileApprovalSession.subject_type == SUBJECT_THREADS_POST,
                    MobileApprovalSession.state == MA_PENDING,
                )
                .order_by(MobileApprovalSession.id)
            ).all()
        )

    def _invalid_reasons(self, proposal: ThreadsPostProposal | None, now: datetime) -> list[str]:
        if proposal is None:
            return ["the proposal no longer exists"]
        reasons: list[str] = []
        if proposal.expires_at is not None and ensure_aware(proposal.expires_at) <= now:
            reasons.append("the proposal expired")
        _stale, why = self._proposals.evaluate_staleness(proposal)
        reasons.extend(why)
        return reasons

    def _last_sent_at(self) -> datetime | None:
        """直前に **届いた** digest の送信操作の時刻 (cooldown の起点)。

        起点は ``issued_at`` (送信の操作を始めた時刻 = 期限の起点と同じ瞬間)。
        ``sent_at`` は SMTP が受け付けた壁時計の時刻で、計画が使う ``now`` とは
        別の時計なので、間隔の計算には混ぜない。
        """

        moment = self._session.scalars(
            select(ThreadsApprovalDigest.issued_at)
            .where(ThreadsApprovalDigest.outcome == DIGEST_SENT)
            .order_by(ThreadsApprovalDigest.issued_at.desc())
            .limit(1)
        ).first()
        return ensure_aware(moment) if moment else None

    def _queued_identities(self) -> set[str]:
        """既に承認済み・公開済みの本文。同じ文面を二度依頼しない。"""

        rows = self._session.scalars(
            select(ThreadsPostProposal.content_text).where(
                ThreadsPostProposal.status == TP_APPROVED
            )
        ).all()
        published = self._session.scalars(select(ThreadsPublication.exact_published_text)).all()
        return {canonical_identity(text) for text in (*rows, *published) if text}

    def _approved_unpublished(self) -> int:
        published = set(self._session.scalars(select(ThreadsPublication.proposal_id)).all())
        rows = self._session.scalars(
            select(ThreadsPostProposal.id).where(ThreadsPostProposal.status == TP_APPROVED)
        ).all()
        return sum(1 for pid in rows if pid not in published)

    def _timing_text(self, candidate: DigestCandidate) -> str | None:
        parts = []
        if candidate.not_before is not None:
            parts.append(f"{to_local(candidate.not_before, self._tz):%m/%d %H:%M} 以降")
        if candidate.expires_at is not None:
            parts.append(f"{to_local(candidate.expires_at, self._tz):%m/%d %H:%M} まで")
        return " / ".join(parts) or None


def _aware(moment: datetime | None) -> datetime | None:
    return ensure_aware(moment) if moment is not None else None


def _preview(text: str, limit: int) -> str:
    """メールに載せる短い抜粋。本文はそのまま (整形し直さない)。"""

    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


__all__ = ["DigestOutcome", "ThreadsApprovalDigestService"]
