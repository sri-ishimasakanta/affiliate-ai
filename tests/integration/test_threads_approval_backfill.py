"""T4.2 migration の backfill 規則 (承認の時刻を推測で作らない)。

pin する規則:

- ``request_sent_at`` は、配送記録が ``sent`` のときだけ、その ``finished_at``。
- ``approval_request_sent_at`` は、その提案のセッションで実際に届いた最新の時刻。
- ``approved_at`` は、携帯承認が approved かつ synchronized のセッションの ``decided_at``。
- どれにも当てはまらなければ **NULL のまま**。``updated_at`` からは推測しない。
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import (
    TP_APPROVED,
    TP_AWAITING_APPROVAL,
    Article,
    MobileApprovalSession,
    NotificationDelivery,
    ThreadsPostProposal,
)

_NOW = datetime(2026, 9, 23, 18, 0, tzinfo=UTC)
_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "949edb90023f_add_threads_approval_digest_workflow.py"
)


def _statements() -> tuple[str, ...]:
    spec = importlib.util.spec_from_file_location("t42_migration", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.BACKFILL_STATEMENTS


def _article(session: Session) -> Article:
    row = Article(
        id=21,
        title="記事",
        slug="a",
        body="本文",
        status="published",
        published_url="https://bizfluxlab.com/a/",
        published_at=_NOW - timedelta(days=5),
        article_type="informational",
        monetization_mode="supporting",
        wordpress_post_id="84",
    )
    session.add(row)
    session.commit()
    return row


def _proposal(session: Session, *, seed: str, status: str) -> ThreadsPostProposal:
    row = ThreadsPostProposal(
        source_article_id=21,
        source_article_body_hash="0" * 64,
        angle="insight",
        link_mode="none",
        content_text=f"投稿 {seed}",
        character_count=len(f"投稿 {seed}"),
        content_seed=(seed * 64)[:64],
        proposal_hash=(seed.upper() * 64)[:64],
        policy_version="t2.1",
        generator_version="g",
        status=status,
        updated_at=_NOW + timedelta(hours=9),  # 無関係な更新。推測に使ってはいけない。
    )
    session.add(row)
    session.commit()
    return row


def _delivery(session: Session, *, outcome: str, finished: datetime) -> NotificationDelivery:
    row = NotificationDelivery(
        channel="email",
        notification_type="approval_request",
        recipient_fingerprint="f" * 64,
        subject="Approval required",
        outcome=outcome,
        attempted_at=finished - timedelta(seconds=4),
        finished_at=finished,
    )
    session.add(row)
    session.commit()
    return row


def _session_row(session: Session, proposal, *, delivery=None, **kw) -> MobileApprovalSession:
    row = MobileApprovalSession(
        subject_type="threads_post",
        subject_id=proposal.id,
        subject_hash=proposal.proposal_hash,
        subject_version=1,
        relay_session_id=kw.pop("relay", "r" * 32),
        capability_digest=kw.pop("digest", "d" * 64),
        capability_binding="b" * 64,
        snapshot_json={},
        expires_at=_NOW + timedelta(hours=24),
        notification_delivery_id=delivery.id if delivery else None,
        **kw,
    )
    session.add(row)
    session.commit()
    return row


def _run(session: Session) -> None:
    for statement in _statements():
        session.execute(text(statement))
    session.commit()
    session.expire_all()


def test_an_approved_mobile_decision_backfills_both_timestamps(session: Session) -> None:
    _article(session)
    proposal = _proposal(session, seed="a", status=TP_APPROVED)
    delivered = _NOW + timedelta(seconds=5)
    decided = _NOW + timedelta(minutes=1)
    _session_row(
        session,
        proposal,
        delivery=_delivery(session, outcome="sent", finished=delivered),
        state="synchronized",
        decision="approved",
        decided_at=decided,
    )
    _run(session)

    assert proposal.approved_at.replace(tzinfo=UTC) == decided
    assert proposal.approval_request_sent_at.replace(tzinfo=UTC) == delivered


def test_an_undelivered_request_leaves_the_send_time_null(session: Session) -> None:
    _article(session)
    proposal = _proposal(session, seed="a", status=TP_AWAITING_APPROVAL)
    row = _session_row(
        session,
        proposal,
        delivery=_delivery(session, outcome="skipped", finished=_NOW),
        state="pending",
    )
    _run(session)

    assert row.request_sent_at is None
    assert proposal.approval_request_sent_at is None


def test_no_authoritative_record_means_null_not_updated_at(session: Session) -> None:
    """承認済みでも、携帯の決定記録が無ければ approved_at は NULL (推測しない)。"""

    _article(session)
    proposal = _proposal(session, seed="a", status=TP_APPROVED)
    _run(session)

    assert proposal.approved_at is None
    assert proposal.approval_request_sent_at is None


def test_a_rejected_or_unsynchronized_decision_is_not_an_approval(session: Session) -> None:
    _article(session)
    approved_but_unsynced = _proposal(session, seed="a", status=TP_APPROVED)
    _session_row(
        session,
        approved_but_unsynced,
        state="approved_remote",
        decision="approved",
        decided_at=_NOW,
    )
    _run(session)
    assert approved_but_unsynced.approved_at is None


def test_the_latest_delivered_request_wins(session: Session) -> None:
    _article(session)
    proposal = _proposal(session, seed="a", status=TP_AWAITING_APPROVAL)
    first = _NOW
    second = _NOW + timedelta(hours=30)
    _session_row(
        session,
        proposal,
        delivery=_delivery(session, outcome="sent", finished=first),
        state="expired",
        relay="1" * 32,
        digest="1" * 64,
    )
    _session_row(
        session,
        proposal,
        delivery=_delivery(session, outcome="sent", finished=second),
        state="pending",
        relay="2" * 32,
        digest="2" * 64,
    )
    _run(session)
    assert proposal.approval_request_sent_at.replace(tzinfo=UTC) == second


def test_the_backfill_is_idempotent(session: Session) -> None:
    _article(session)
    proposal = _proposal(session, seed="a", status=TP_APPROVED)
    _session_row(
        session,
        proposal,
        delivery=_delivery(session, outcome="sent", finished=_NOW),
        state="synchronized",
        decision="approved",
        decided_at=_NOW + timedelta(minutes=1),
    )
    _run(session)
    first = (proposal.approved_at, proposal.approval_request_sent_at)
    _run(session)
    assert (proposal.approved_at, proposal.approval_request_sent_at) == first
