"""MobileApprovalService -- 承認依頼の送信と、人の決定の取り込み (C8.8)。

この service がやることは 2 つだけ:

1. **明示的に指定された** C9 変更要求について、レビューセッションを 1 つ作り、
   承認依頼メールを 1 通送る (承認はしない)。
2. 人が携帯で下した決定を取りに行き、検証して、**既存の C9 service** で記録する。

やらないこと (境界):

- 候補から自動でセッションを作らない。要求は既に存在していなければならない。
- 決定を作り出さない。中継に人の決定が無ければ何も起きない。
- 承認を適用しない。WordPress には一切触れない。
- ``change_requests`` を直接書き換えない。必ず
  :class:`~app.services.change_request_service.ChangeRequestService` を通す。

したがって「承認と同じ意味」を持つのは 1 経路だけであり、弱い検査の第 2 経路は
存在しない。携帯からの承認は
``manage_change_requests.py approve <id> --proposal-hash <hash>`` と同じ検査を
通り、違うのは記録される決定者 (``human-mobile``) だけである。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.approval.capability import DEFAULT_TTL_HOURS, generate_capability, verify_capability
from app.approval.relay_client import ApprovalRelayClient, RelayError, build_review_url
from app.approval.review_snapshot import build_snapshot
from app.article.fact_freshness import ensure_aware, to_storage_utc
from app.models import (
    CR_OPEN_STATUSES,
    DECIDED_BY_MOBILE,
    DECISION_APPROVED,
    DECISION_REJECTED,
    MA_APPROVED_REMOTE,
    MA_DECIDED_STATES,
    MA_EXPIRED,
    MA_FAILED,
    MA_PENDING,
    MA_REJECTED_REMOTE,
    MA_REVOKED,
    MA_STALE,
    MA_SYNCHRONIZED,
    SUBJECT_CHANGE_REQUEST,
    SUBJECT_THREADS_POST,
    SUBJECT_TYPES_SUPPORTED_IN_V1,
    TP_APPROVED,
    TP_OPEN_STATES,
    TP_REJECTED,
    Article,
    ChangeRequest,
    MobileApprovalEvent,
    MobileApprovalSession,
    ThreadsPostProposal,
    mobile_approval_transition_allowed,
)
from app.models.notification_delivery import DELIVERY_SENT, NotificationDelivery
from app.services.change_request_service import ChangeRequestError, ChangeRequestService


class MobileApprovalError(Exception):
    """承認依頼/取り込みの不正な操作。**capability は含めない。**"""

    def __init__(self, reason: str) -> None:
        super().__init__(f"mobile approval error: {reason}")
        self.reason = reason


@dataclass
class PreparedApproval:
    """送信前に人へ見せる計画 (PLAN)。"""

    change_request_id: int
    subject_hash: str
    subject_version: int
    article_id: int
    expires_at: str
    ttl_hours: int
    snapshot: dict
    existing_session_id: int | None = None
    subject_type: str = SUBJECT_CHANGE_REQUEST
    blocked_reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocked_reasons

    def as_dict(self) -> dict:
        return {
            "change_request_id": self.change_request_id,
            "subject_type": self.subject_type,
            "subject_hash": self.subject_hash,
            "subject_version": self.subject_version,
            "article_id": self.article_id,
            "expires_at": self.expires_at,
            "ttl_hours": self.ttl_hours,
            "existing_session_id": self.existing_session_id,
            "blocked_reasons": list(self.blocked_reasons),
            "snapshot": self.snapshot,
        }


@dataclass
class IssuedRequest:
    """作ったレビューセッションと、メールに載せる URL (T4.2)。

    ``review_url`` には生の capability が入る。**メール本文にだけ載せ、保存しない。**
    """

    row: MobileApprovalSession
    review_url: str
    prepared: PreparedApproval


@dataclass
class SyncOutcome:
    """1 回の取り込みの結果。"""

    fetched: int = 0
    applied: int = 0
    skipped: int = 0
    failed: int = 0
    executed: bool = False
    details: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "fetched": self.fetched,
            "applied": self.applied,
            "skipped": self.skipped,
            "failed": self.failed,
            "executed": self.executed,
            "details": list(self.details),
        }


# -- subject adapters ----------------------------------------------------------
# 1 つの経路で 2 種類の subject を扱う。弱い検査の第 2 経路を作らないため、
# 期限・封筒一致・陳腐化の判定はすべて共通で、subject 固有の知識だけをここへ閉じ込める。


class _ChangeRequestSubject:
    """C9 の記事変更要求 (C8.8 から変わらない)。"""

    subject_type = SUBJECT_CHANGE_REQUEST

    def __init__(self, session: Session, settings) -> None:
        self._session = session
        self._service = ChangeRequestService(session)

    def load(self, subject_id: int):
        request = self._session.get(ChangeRequest, subject_id)
        if request is None:
            raise MobileApprovalError(f"change request {subject_id} not found")
        article = self._session.get(Article, request.article_id)
        target = (
            self._session.get(Article, request.target_article_id)
            if request.target_article_id
            else None
        )
        return request, article, target

    def snapshot(self, subject, article, target) -> dict:
        return build_snapshot(
            subject_type=SUBJECT_CHANGE_REQUEST,
            subject=subject,
            article=article,
            target_article=target,
        )

    def identity(self, subject) -> tuple[str, int]:
        return subject.proposal_hash, subject.proposal_version

    def blocked_reasons(self, subject) -> list[str]:
        reasons: list[str] = []
        if subject.status not in CR_OPEN_STATUSES:
            reasons.append(
                f"request status is {subject.status!r}; only a request awaiting approval "
                "can be sent for mobile review"
            )
        report = self._service.evaluate_staleness(subject)
        if report.stale:
            reasons.extend(report.reasons)
        return reasons

    def apply_decision(self, subject, decision: str, reason: str | None, now: datetime):
        """既存の C9 service を通す。ここだけが承認を記録できる経路である。"""

        if decision == DECISION_APPROVED:
            return self._service.approve(
                subject.id,
                proposal_hash=subject.proposal_hash,
                decided_by=DECIDED_BY_MOBILE,
                reason=reason,
                now=now,
            )
        return self._service.reject(
            subject.id,
            reason=reason or "モバイルから却下",
            decided_by=DECIDED_BY_MOBILE,
            now=now,
        )


class _ThreadsPostSubject:
    """Threads 投稿案 (T2)。

    **承認は公開ではない。** ここでも Threads API には一切触れない。承認された案は
    「公開してよい」状態になるだけで、実際の公開は T3 の別操作である。
    """

    subject_type = SUBJECT_THREADS_POST

    def __init__(self, session: Session, settings) -> None:
        self._session = session
        from app.services.threads_proposal_service import ThreadsProposalService

        self._service = ThreadsProposalService(session)

    def load(self, subject_id: int):
        row = self._session.get(ThreadsPostProposal, subject_id)
        if row is None:
            raise MobileApprovalError(f"threads post proposal {subject_id} not found")
        article = self._session.get(Article, row.source_article_id)
        return row, article, None

    def snapshot(self, subject, article, target) -> dict:
        return build_snapshot(subject_type=SUBJECT_THREADS_POST, subject=subject, article=article)

    def identity(self, subject) -> tuple[str, int]:
        # Threads の提案は版を持たない (作り直せば別レコードになる)。
        return subject.proposal_hash, 1

    def blocked_reasons(self, subject, now: datetime | None = None) -> list[str]:
        reasons: list[str] = []
        if subject.status not in TP_OPEN_STATES:
            reasons.append(
                f"proposal status is {subject.status!r}; only a proposal awaiting approval "
                "can be sent for mobile review"
            )
        stale, why = self._service.evaluate_staleness(subject)
        if stale:
            reasons.extend(why)
        # T4.2: 期限を過ぎた提案は、承認を求める意味も受け取る意味も無い。
        if subject.expires_at is not None and ensure_aware(subject.expires_at) <= (
            now or datetime.now(UTC)
        ):
            reasons.append("the proposal expired; it is no longer appropriate to publish")
        return reasons

    def apply_decision(self, subject, decision: str, reason: str | None, now: datetime):
        """人の判断を提案へ記録する。**公開しない。**

        承認されたら ``approved_at`` に **受理した時刻** を残す (T4.2)。以後の queue の
        順序はこの時刻を使い、``updated_at`` からは推測しない。承認は queue に入る
        だけで、公開は T3 の別操作である。
        """

        stale, why = self._service.evaluate_staleness(subject)
        if stale:
            raise ChangeRequestError("; ".join(why))
        if subject.status not in TP_OPEN_STATES:
            raise ChangeRequestError(f"the proposal is already {subject.status}")
        subject.status = TP_APPROVED if decision == DECISION_APPROVED else TP_REJECTED
        subject.status_reason = reason or f"decided on mobile ({decision})"
        if decision == DECISION_APPROVED:
            subject.approved_at = to_storage_utc(now)
        self._session.commit()
        return None


_SUBJECT_ADAPTERS = {
    SUBJECT_CHANGE_REQUEST: _ChangeRequestSubject,
    SUBJECT_THREADS_POST: _ThreadsPostSubject,
}


class MobileApprovalService:
    def __init__(self, session: Session, *, settings, relay_client=None, notifier=None) -> None:
        self._session = session
        self._settings = settings
        self._relay = relay_client or ApprovalRelayClient(settings)
        self._notifier = notifier

    @property
    def notifier(self):
        """承認依頼メールを送る notifier (まとめ送りでも同じものを使う)。"""

        return self._notifier

    # -- 1) 承認依頼 -----------------------------------------------------------
    def _adapter(self, subject_type: str):
        adapter = _SUBJECT_ADAPTERS.get(subject_type)
        if adapter is None or subject_type not in SUBJECT_TYPES_SUPPORTED_IN_V1:
            raise MobileApprovalError(f"subject type {subject_type!r} is not supported")
        return adapter(self._session, self._settings)

    def plan(
        self,
        *,
        change_request_id: int | None = None,
        subject_type: str = SUBJECT_CHANGE_REQUEST,
        subject_id: int | None = None,
        ttl_hours: int = DEFAULT_TTL_HOURS,
        now: datetime | None = None,
    ) -> PreparedApproval:
        """送らずに、いま依頼を出せるかだけを判定する。

        ``change_request_id`` は C9 のための従来どおりの呼び方で、
        ``subject_type`` / ``subject_id`` は Threads を含む一般形である。

        ``now`` は期限切れの判定に使う時刻。:meth:`issue` は自分の ``now`` をここへ渡す
        (TTL を数える時刻と、期限切れを判定する時刻を同じ時計にする)。省略時は現在時刻。
        """

        if change_request_id is not None:
            subject_type, subject_id = SUBJECT_CHANGE_REQUEST, change_request_id
        if subject_id is None:
            raise MobileApprovalError("a subject id is required")

        adapter = self._adapter(subject_type)
        subject, article, target = adapter.load(subject_id)
        subject_hash, subject_version = adapter.identity(subject)
        prepared = PreparedApproval(
            change_request_id=subject_id,
            subject_hash=subject_hash,
            subject_version=subject_version,
            article_id=getattr(article, "id", 0) or 0,
            expires_at="",
            ttl_hours=ttl_hours,
            snapshot=adapter.snapshot(subject, article, target),
        )
        prepared.subject_type = subject_type
        prepared.blocked_reasons.extend(_blocked(adapter, subject, now))

        existing = self._active_session(subject_type, subject_id)
        if existing is not None:
            prepared.existing_session_id = existing.id
            prepared.blocked_reasons.append(
                f"an active review session ({existing.id}) already exists; "
                "revoke it before sending another"
            )
        return prepared

    def send(
        self,
        *,
        change_request_id: int | None = None,
        subject_type: str = SUBJECT_CHANGE_REQUEST,
        subject_id: int | None = None,
        ttl_hours: int = DEFAULT_TTL_HOURS,
        now: datetime | None = None,
    ) -> MobileApprovalSession:
        """レビューセッションを 1 つ作り、承認依頼メールを 1 通送る。

        **承認はしない。** 作られるのは ``pending`` のセッションだけである。
        """

        now = now or datetime.now(UTC)
        if change_request_id is not None:
            subject_type, subject_id = SUBJECT_CHANGE_REQUEST, change_request_id
        if subject_id is None:
            raise MobileApprovalError("a subject id is required")
        issued = self.issue(
            subject_type=subject_type, subject_id=subject_id, ttl_hours=ttl_hours, now=now
        )
        delivery_id = self._send_email(issued.row, issued.prepared, issued.review_url)
        if delivery_id is not None:
            issued.row.notification_delivery_id = delivery_id
            self._session.commit()
        self.mark_request_sent([issued.row], delivery_id=delivery_id)
        return issued.row

    def issue(
        self,
        *,
        subject_type: str,
        subject_id: int,
        ttl_hours: int = DEFAULT_TTL_HOURS,
        now: datetime | None = None,
        approval_digest_id: int | None = None,
    ) -> IssuedRequest:
        """レビューセッションを 1 つ作る。**メールは送らない** (T4.2 で切り出し)。

        期限 (TTL) は ``now`` = **依頼を出す操作の時刻** から数える。提案を作った時刻
        からは数えない。夜のうちに作った提案が、朝に送られるまでに期限を使い切る
        ことはない。

        まとめ送りでは、1 通に入る提案ごとにこれを 1 回ずつ呼ぶ。セッションも
        capability も提案ごとに別々で、決定も別々である。
        """

        now = now or datetime.now(UTC)
        prepared = self.plan(
            subject_type=subject_type, subject_id=subject_id, ttl_hours=ttl_hours, now=now
        )
        if not prepared.ok:
            raise MobileApprovalError("; ".join(prepared.blocked_reasons))

        issued = generate_capability(
            subject_type=subject_type,
            subject_id=subject_id,
            subject_hash=prepared.subject_hash,
            subject_version=prepared.subject_version,
            issued_at=now,
            ttl_hours=ttl_hours,
        )
        relay_session_id = uuid.uuid4().hex

        # 中継には digest だけを渡す。生の capability は送らない。
        try:
            self._relay.create_session(
                relay_session_id=relay_session_id,
                subject_type=subject_type,
                subject_id=subject_id,
                subject_hash=prepared.subject_hash,
                subject_version=prepared.subject_version,
                capability_digest=issued.digest,
                expires_at=issued.expires_at.isoformat(),
                snapshot=prepared.snapshot,
            )
        except RelayError as exc:
            raise MobileApprovalError(f"relay refused the session: {exc.reason}") from None

        session_row = MobileApprovalSession(
            subject_type=subject_type,
            subject_id=subject_id,
            subject_hash=prepared.subject_hash,
            subject_version=prepared.subject_version,
            relay_session_id=relay_session_id,
            capability_digest=issued.digest,
            capability_binding=issued.binding,
            state=MA_PENDING,
            snapshot_json=prepared.snapshot,
            expires_at=to_storage_utc(issued.expires_at),
            approval_digest_id=approval_digest_id,
        )
        self._session.add(session_row)
        self._session.commit()
        self._session.refresh(session_row)
        self._event(session_row, "created", to_state=MA_PENDING)

        review_url = build_review_url(
            self._settings, relay_session_id=relay_session_id, capability=issued.secret
        )
        # 生の capability は review_url の中 (メール本文) にしか存在しない。DB には残さない。
        return IssuedRequest(row=session_row, review_url=review_url, prepared=prepared)

    def mark_request_sent(self, rows, *, delivery_id: int | None) -> datetime | None:
        """依頼が **実際に届いた** ときだけ、送信時刻を記録する (T4.2)。

        時刻は配送記録の ``finished_at`` (SMTP が受け付けた後に刻まれる) を使う。
        届かなかった (skipped / failed) なら何も書かず ``None`` を返す。
        """

        if delivery_id is None:
            return None
        delivery = self._session.get(NotificationDelivery, delivery_id)
        if delivery is None or delivery.outcome != DELIVERY_SENT:
            return None
        sent_at = delivery.finished_at or delivery.attempted_at
        for row in rows:
            row.request_sent_at = sent_at
            if row.subject_type == SUBJECT_THREADS_POST:
                proposal = self._session.get(ThreadsPostProposal, row.subject_id)
                if proposal is not None:
                    proposal.approval_request_sent_at = sent_at
        self._session.commit()
        return ensure_aware(sent_at)

    def revoke(self, *, session_id: int, reason: str) -> MobileApprovalSession:
        """セッションを失効させる (以後 capability は使えない)。"""

        row = self._session.get(MobileApprovalSession, session_id)
        if row is None:
            raise MobileApprovalError(f"mobile approval session {session_id} not found")
        if not mobile_approval_transition_allowed(row.state, MA_REVOKED):
            raise MobileApprovalError(f"'{row.state}' -> '{MA_REVOKED}' is not allowed")
        try:
            self._relay.revoke(relay_session_id=row.relay_session_id, reason=reason[:200])
        except RelayError as exc:
            # 中継に届かなくてもローカルは失効させる (こちらが権威)。
            self._event(row, "relay_revoke_failed", detail=exc.reason)
        self._transition(row, MA_REVOKED, reason=reason)
        return row

    def expire(self, *, session_id: int, reason: str) -> MobileApprovalSession:
        """期限を過ぎた pending のセッションを、ローカルで expired にする (T4.2)。

        中継側も期限切れの capability を受け付けないので、中継への呼び出しは不要。
        これで同じ提案を改めて依頼できるようになる。**他のセッションには触れない。**
        """

        row = self._session.get(MobileApprovalSession, session_id)
        if row is None:
            raise MobileApprovalError(f"mobile approval session {session_id} not found")
        self._transition(row, MA_EXPIRED, reason=reason)
        return row

    # -- 2) 決定の取り込み -----------------------------------------------------
    def sync(self, *, execute: bool = False, now: datetime | None = None) -> SyncOutcome:
        """人が下した決定を取りに行き、検証して C9 へ記録する。

        ``execute=False`` では **何も記録しない** (何が起きるかを示すだけ)。
        決定を作り出すことは、どちらのモードでも決してない。
        """

        now = now or datetime.now(UTC)
        outcome = SyncOutcome(executed=execute)
        try:
            decisions = self._relay.fetch_decisions()
        except RelayError as exc:
            outcome.failed += 1
            outcome.details.append(
                {"relay_session_id": None, "result": "relay_error", "reason": exc.reason}
            )
            return outcome

        outcome.fetched = len(decisions)
        for decision in decisions:
            detail = self._sync_one(decision, execute=execute, now=now)
            outcome.details.append(detail)
            result = detail["result"]
            if result == "applied":
                outcome.applied += 1
            elif result in ("failed", "relay_error"):
                outcome.failed += 1
            else:
                outcome.skipped += 1
        return outcome

    # -- internals ------------------------------------------------------------
    def _sync_one(self, decision, *, execute: bool, now: datetime) -> dict:
        base = {
            "relay_session_id": decision.relay_session_id,
            "subject_type": decision.subject_type,
            "subject_id": decision.subject_id,
            "decision": decision.decision,
        }
        row = self._session.scalars(
            select(MobileApprovalSession).where(
                MobileApprovalSession.relay_session_id == decision.relay_session_id
            )
        ).first()
        if row is None:
            return {**base, "result": "skipped", "reason": "no local session for this decision"}
        if row.state == MA_SYNCHRONIZED:
            # 中継の再送で二重に承認しない。
            return {**base, "result": "skipped", "reason": "already synchronized"}
        if row.state not in (MA_PENDING, *MA_DECIDED_STATES, MA_FAILED):
            return {**base, "result": "skipped", "reason": f"session is {row.state}"}
        if decision.subject_type not in SUBJECT_TYPES_SUPPORTED_IN_V1:
            return {**base, "result": "skipped", "reason": "subject type is not supported yet"}
        if decision.subject_type != row.subject_type:
            return {**base, "result": "skipped", "reason": "subject type mismatch"}
        if decision.decision not in (DECISION_APPROVED, DECISION_REJECTED):
            return {**base, "result": "skipped", "reason": "unknown decision"}

        # 中継が言う内容と、ローカルが凍結した封筒が一致すること。
        if (
            decision.subject_id != row.subject_id
            or decision.subject_hash != row.subject_hash
            or decision.subject_version != row.subject_version
        ):
            if execute:
                self._transition(row, MA_STALE, reason="relay envelope does not match the session")
            return {**base, "result": "skipped", "reason": "envelope mismatch"}

        expires = ensure_aware(row.expires_at)
        if now >= expires:
            if execute:
                self._transition(row, MA_EXPIRED, reason="the review session expired")
            return {**base, "result": "skipped", "reason": "session expired"}

        try:
            adapter = self._adapter(row.subject_type)
            subject, _article, _target = adapter.load(row.subject_id)
        except MobileApprovalError as exc:
            if execute:
                self._transition(row, MA_STALE, reason=exc.reason)
            return {**base, "result": "skipped", "reason": exc.reason}

        # 提案が作り直されていれば、古い決定は移らない。
        subject_hash, subject_version = adapter.identity(subject)
        if subject_hash != row.subject_hash:
            if execute:
                self._transition(row, MA_STALE, reason="the proposal hash changed after review")
            return {**base, "result": "skipped", "reason": "proposal hash changed"}
        if subject_version != row.subject_version:
            if execute:
                self._transition(row, MA_STALE, reason="the proposal version changed after review")
            return {**base, "result": "skipped", "reason": "proposal version changed"}

        blocked = _blocked(adapter, subject, now)
        if blocked:
            if execute:
                self._transition(row, MA_STALE, reason="; ".join(blocked))
            return {**base, "result": "skipped", "reason": "; ".join(blocked)}

        if not execute:
            return {**base, "result": "would_apply", "reason": None}

        # subject ごとの正規経路を通す。ここだけが人の判断を記録できる。
        try:
            approval = adapter.apply_decision(
                subject, decision.decision, decision.decision_reason, now
            )
        except ChangeRequestError as exc:
            self._transition(row, MA_FAILED, reason=exc.reason)
            return {**base, "result": "failed", "reason": exc.reason}

        row.decision = decision.decision
        row.decision_reason = decision.decision_reason
        row.decided_at = to_storage_utc(now)
        row.change_request_approval_id = approval.id if approval is not None else None
        row.synchronized_at = to_storage_utc(now)
        intermediate = (
            MA_APPROVED_REMOTE if decision.decision == DECISION_APPROVED else MA_REJECTED_REMOTE
        )
        self._transition(row, intermediate, reason="decision received from the review gateway")
        self._transition(row, MA_SYNCHRONIZED, reason="recorded through ChangeRequestService")

        try:
            self._relay.acknowledge(
                relay_session_id=row.relay_session_id, local_outcome=decision.decision
            )
        except RelayError as exc:
            # 反映は済んでいる。ack の失敗で承認を取り消さない。
            self._event(row, "relay_ack_failed", detail=exc.reason)
        return {
            **base,
            "result": "applied",
            "reason": None,
            "approval_id": approval.id if approval is not None else None,
        }

    def _send_email(self, row: MobileApprovalSession, prepared: PreparedApproval, url: str):
        from app.services.operations_notification_service import (
            OperationsNotificationService,
        )

        service = OperationsNotificationService(
            self._session, settings=self._settings, notifier=self._notifier
        )
        outcome = service.send_approval_request(
            session=row, snapshot=prepared.snapshot, review_url=url
        )
        self._event(
            row,
            "email_sent" if outcome.sent else "email_not_sent",
            detail=outcome.reason,
        )
        return outcome.delivery_id

    def _active_session(self, subject_type: str, subject_id: int) -> MobileApprovalSession | None:
        return self._session.scalars(
            select(MobileApprovalSession)
            .where(
                MobileApprovalSession.subject_type == subject_type,
                MobileApprovalSession.subject_id == subject_id,
                MobileApprovalSession.state == MA_PENDING,
            )
            .order_by(MobileApprovalSession.id.desc())
            .limit(1)
        ).first()

    def _require_request(self, change_request_id: int) -> ChangeRequest:
        request = self._session.get(ChangeRequest, change_request_id)
        if request is None:
            raise MobileApprovalError(f"change request {change_request_id} not found")
        return request

    def _transition(self, row: MobileApprovalSession, target: str, *, reason: str | None) -> None:
        if row.state == target:
            return
        if not mobile_approval_transition_allowed(row.state, target):
            raise MobileApprovalError(f"'{row.state}' -> '{target}' is not allowed")
        previous = row.state
        row.state = target
        row.state_reason = reason
        self._session.commit()
        self._event(row, "state_changed", from_state=previous, to_state=target, detail=reason)

    def _event(
        self,
        row: MobileApprovalSession,
        event_type: str,
        *,
        from_state: str | None = None,
        to_state: str | None = None,
        detail: str | None = None,
    ) -> None:
        self._session.add(
            MobileApprovalEvent(
                mobile_approval_session_id=row.id,
                event_type=event_type,
                from_state=from_state,
                to_state=to_state,
                detail=detail,
            )
        )
        self._session.commit()


def _blocked(adapter, subject, now: datetime | None) -> list[str]:
    """adapter ごとの blocked_reasons。時刻を見る adapter にだけ ``now`` を渡す。"""

    if isinstance(adapter, _ThreadsPostSubject):
        return adapter.blocked_reasons(subject, now)
    return adapter.blocked_reasons(subject)


def verify_session_capability(
    row: MobileApprovalSession, presented: str, *, now: datetime
) -> tuple[bool, str]:
    """ローカル側でも capability を検証できるようにする (中継の検証と同じ規則)。"""

    return verify_capability(
        presented=presented,
        stored_digest=row.capability_digest,
        stored_binding=row.capability_binding,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        subject_hash=row.subject_hash,
        subject_version=row.subject_version,
        expires_at=ensure_aware(row.expires_at),
        now=now,
    )
