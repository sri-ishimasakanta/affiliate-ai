"""ThreadsQueueControlService -- 人の queue 操作 (T4.2)。

操作は 5 つだけ:

- ``hold``: 選ばれないようにする。解除するまで有効。
- ``release``: 保留を解く。
- ``prefer_next``: 「次に優先」を指定する。**並び順の先頭に来るだけで、安全の条件は
  1 つも飛ばさない** (却下・stale・期限切れ・not_before・中身の不整合・T3 の不確定・
  公開窓・間隔・公開済み のどれも)。
- ``clear_preference``: 優先の指定を外す。
- ``set_timing``: ``not_before`` / ``expires_at`` を設定・変更・解除する。

どの操作も、誰が・いつ・なぜ行ったかを ``threads_queue_control_events`` に
append-only で残す。**提案の本文には触れない** (本文の権威は提案の行)。

自動では呼ばれない。承認と同じく、人が明示的に操作したときだけ動く。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware, to_storage_utc
from app.exceptions import ApplicationError
from app.models import (
    QC_CLEAR_PREFERENCE,
    QC_HOLD,
    QC_PREFER_NEXT,
    QC_RELEASE,
    QC_SET_TIMING,
    TP_APPROVED,
    TP_OPEN_STATES,
    ThreadsPostProposal,
    ThreadsPublication,
    ThreadsQueueControlEvent,
)

#: 人の操作だと分かる主体名。自動の操作には使わない。
ACTOR_HUMAN_CLI = "human-cli"

#: queue 操作の対象になりうる状態 (在庫・承認待ち・承認済み)。
_CONTROLLABLE = frozenset({TP_APPROVED, *TP_OPEN_STATES})


class ThreadsQueueControlError(ApplicationError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"threads queue control error: {reason}")
        self.reason = reason


class ThreadsQueueControlService:
    def __init__(self, session: Session) -> None:
        self._session = session

    # -- hold / release ----------------------------------------------------------
    def hold(
        self,
        proposal_id: int,
        *,
        reason: str,
        actor: str = ACTOR_HUMAN_CLI,
        now: datetime | None = None,
    ) -> ThreadsPostProposal:
        proposal = self._controllable(proposal_id)
        if not (reason or "").strip():
            raise ThreadsQueueControlError("a hold needs a human-readable reason")
        if proposal.held_at is not None:
            raise ThreadsQueueControlError(f"proposal {proposal_id} is already held")
        now = now or datetime.now(UTC)
        proposal.held_at = to_storage_utc(now)
        proposal.hold_reason = reason.strip()[:500]
        self._event(proposal, QC_HOLD, actor=actor, reason=reason, now=now)
        return proposal

    def release(
        self,
        proposal_id: int,
        *,
        reason: str | None = None,
        actor: str = ACTOR_HUMAN_CLI,
        now: datetime | None = None,
    ) -> ThreadsPostProposal:
        proposal = self._require(proposal_id)
        if proposal.held_at is None:
            raise ThreadsQueueControlError(f"proposal {proposal_id} is not held")
        detail = {
            "held_since": ensure_aware(proposal.held_at).isoformat(),
            "hold_reason": proposal.hold_reason,
        }
        proposal.held_at = None
        proposal.hold_reason = None
        self._event(proposal, QC_RELEASE, actor=actor, reason=reason, detail=detail, now=now)
        return proposal

    # -- preference --------------------------------------------------------------
    def prefer_next(
        self,
        proposal_id: int,
        *,
        reason: str | None = None,
        actor: str = ACTOR_HUMAN_CLI,
        now: datetime | None = None,
    ) -> ThreadsPostProposal:
        """並び順の希望を記録する。**資格の判定には一切影響しない。**"""

        proposal = self._controllable(proposal_id)
        now = now or datetime.now(UTC)
        proposal.preferred_at = to_storage_utc(now)
        self._event(proposal, QC_PREFER_NEXT, actor=actor, reason=reason, now=now)
        return proposal

    def clear_preference(
        self,
        proposal_id: int,
        *,
        reason: str | None = None,
        actor: str = ACTOR_HUMAN_CLI,
        now: datetime | None = None,
    ) -> ThreadsPostProposal:
        proposal = self._require(proposal_id)
        if proposal.preferred_at is None:
            raise ThreadsQueueControlError(f"proposal {proposal_id} has no preference to clear")
        proposal.preferred_at = None
        self._event(proposal, QC_CLEAR_PREFERENCE, actor=actor, reason=reason, now=now)
        return proposal

    # -- timing ------------------------------------------------------------------
    def set_timing(
        self,
        proposal_id: int,
        *,
        not_before: datetime | None,
        expires_at: datetime | None,
        reason: str | None = None,
        actor: str = ACTOR_HUMAN_CLI,
        now: datetime | None = None,
    ) -> ThreadsPostProposal:
        """時刻の制約を設定する。``None`` は「制約なし」(常緑)。

        期限は人が明示したときだけ付く。古いというだけで自動的に付けることはない。
        """

        proposal = self._controllable(proposal_id)
        now = now or datetime.now(UTC)
        if not_before is not None and not_before.tzinfo is None:
            raise ThreadsQueueControlError("not_before must be timezone-aware")
        if expires_at is not None and expires_at.tzinfo is None:
            raise ThreadsQueueControlError("expires_at must be timezone-aware")
        if not_before is not None and expires_at is not None and not not_before < expires_at:
            raise ThreadsQueueControlError("not_before must be earlier than expires_at")
        if expires_at is not None and expires_at <= now:
            raise ThreadsQueueControlError(
                "expires_at is already in the past; reject the proposal instead"
            )
        detail = {
            "before": {
                "not_before": _iso(proposal.not_before),
                "expires_at": _iso(proposal.expires_at),
            },
            "after": {
                "not_before": not_before.astimezone(UTC).isoformat() if not_before else None,
                "expires_at": expires_at.astimezone(UTC).isoformat() if expires_at else None,
            },
        }
        proposal.not_before = to_storage_utc(not_before) if not_before else None
        proposal.expires_at = to_storage_utc(expires_at) if expires_at else None
        self._event(proposal, QC_SET_TIMING, actor=actor, reason=reason, detail=detail, now=now)
        return proposal

    # -- history -----------------------------------------------------------------
    def history(self, proposal_id: int) -> list[ThreadsQueueControlEvent]:
        return list(
            self._session.scalars(
                select(ThreadsQueueControlEvent)
                .where(ThreadsQueueControlEvent.proposal_id == proposal_id)
                .order_by(ThreadsQueueControlEvent.id)
            ).all()
        )

    # -- internals ---------------------------------------------------------------
    def _require(self, proposal_id: int) -> ThreadsPostProposal:
        proposal = self._session.get(ThreadsPostProposal, proposal_id)
        if proposal is None:
            raise ThreadsQueueControlError(f"threads post proposal {proposal_id} not found")
        return proposal

    def _controllable(self, proposal_id: int) -> ThreadsPostProposal:
        proposal = self._require(proposal_id)
        if proposal.status not in _CONTROLLABLE:
            raise ThreadsQueueControlError(
                f"proposal {proposal_id} is {proposal.status!r}; only proposals in stock, "
                "awaiting approval or approved can be controlled"
            )
        published = self._session.scalars(
            select(ThreadsPublication.id).where(ThreadsPublication.proposal_id == proposal_id)
        ).first()
        if published is not None:
            raise ThreadsQueueControlError(
                f"proposal {proposal_id} already has a publication; it is no longer in the queue"
            )
        return proposal

    def _event(
        self,
        proposal: ThreadsPostProposal,
        action: str,
        *,
        actor: str,
        reason: str | None,
        now: datetime | None,
        detail: dict | None = None,
    ) -> None:
        self._session.add(
            ThreadsQueueControlEvent(
                proposal_id=proposal.id,
                action=action,
                actor=actor[:64],
                reason=(reason or None) and reason[:500],
                detail_json=detail,
                created_at=to_storage_utc(now or datetime.now(UTC)),
            )
        )
        self._session.commit()


def _iso(moment: datetime | None) -> str | None:
    return ensure_aware(moment).isoformat() if moment is not None else None


__all__ = ["ACTOR_HUMAN_CLI", "ThreadsQueueControlError", "ThreadsQueueControlService"]
