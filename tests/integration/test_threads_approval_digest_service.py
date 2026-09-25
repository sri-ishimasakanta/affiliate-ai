"""承認依頼のまとめ送り (T4.2、DB 連携。中継もメールも偽物で、外には出ない)。

pin する契約:

- 1 通のメールに複数の提案。提案ごとに別々のレビューセッション・別々の capability。
- **一括承認は無い。** 決定は提案ごとに独立し、A の却下は B/C に影響しない。
- 期限は送信時刻から 24 時間。提案が何時間前に作られていても関係ない。
- ``approval_request_sent_at`` は **メールが届いたときだけ** 記録される。
- メールが届かなければ、作ったセッションはすべて失効し、提案は在庫に戻る。
- stale / 期限切れになった提案の依頼は、その 1 件だけが失効する。
- 承認されたら ``approved_at`` が記録される。**承認しても投稿はされない。**
- 生の capability は中継にも DB にも配送記録にも残らない (メール本文にだけ載る)。
- PLAN は何も書かず、何も送らない。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.approval.relay_client import RelayDecision
from app.models import (
    DIGEST_FAILED,
    DIGEST_SENT,
    MA_EXPIRED,
    MA_PENDING,
    MA_REVOKED,
    MA_SYNCHRONIZED,
    NOTIFICATION_APPROVAL_DIGEST,
    TP_APPROVED,
    TP_AWAITING_APPROVAL,
    TP_REJECTED,
    Article,
    MobileApprovalSession,
    NotificationDelivery,
    ThreadsApprovalDigest,
    ThreadsPostProposal,
    ThreadsPublication,
    ThreadsPublicationAttempt,
)
from app.operations.notifications import NotificationResult
from app.services.mobile_approval_service import MobileApprovalService
from app.services.threads_approval_digest_service import ThreadsApprovalDigestService

_BASE = "https://bizfluxlab.com"
# 2026-09-25 10:00 JST。通知窓の中。
_NOW = datetime(2026, 9, 25, 1, 0, tzinfo=UTC)
_TOKEN = "THAAAsecret-token-must-never-appear"


class _Settings:
    wordpress_base_url = _BASE
    approval_relay_shared_secret = "approval-relay-secret"
    affiliate_runtime_shared_secret = "affiliate-runtime-secret"
    operations_email_enabled = True
    operations_email_smtp_host = "smtp.example.com"
    operations_email_smtp_port = 587
    operations_email_username = "ops@example.com"
    operations_email_password = "smtp-password-never-stored"
    operations_email_from = "ops@example.com"
    operations_email_use_starttls = True
    operations_email_subject_prefix = "BizFluxLab"
    operations_webhook_url = None
    threads_enabled = True
    threads_user_id = "9876543210"
    threads_access_token = _TOKEN
    threads_api_base_url = "https://graph.threads.net"
    threads_api_version = "v1.0"

    @property
    def operations_email_recipients(self):
        return ["owner@example.com"]


class _FakeRelay:
    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.decisions: list[RelayDecision] = []
        self.revoked: list[str] = []
        self.fail_for: set[int] = set()

    def create_session(self, **kwargs):
        from app.approval.relay_client import RelayError

        if kwargs["subject_id"] in self.fail_for:
            raise RelayError("invalid_payload")
        self.sessions[kwargs["relay_session_id"]] = dict(kwargs)
        return {"relay_session_id": kwargs["relay_session_id"], "state": "pending"}

    def fetch_decisions(self):
        return list(self.decisions)

    def acknowledge(self, *, relay_session_id, local_outcome):
        return {"state": "consumed"}

    def revoke(self, *, relay_session_id, reason):
        self.revoked.append(relay_session_id)
        return {"state": "revoked"}


class _Notifier:
    name = "email"

    def __init__(self, *, delivered: bool = True) -> None:
        self.sent: list[dict] = []
        self._delivered = delivered

    def send_report(self, *, subject, body, html_body=None):
        self.sent.append({"subject": subject, "body": body, "html": html_body})
        return NotificationResult(self.name, self._delivered, None if self._delivered else "smtp")


@pytest.fixture(autouse=True)
def _no_threads_http(monkeypatch):
    """Threads の HTTP 層に触れたら失敗させる (承認もまとめ送りも投稿しない)。"""

    from app.social.threads.client import ThreadsClient

    def _refuse(self, *_args, **_kwargs):
        raise AssertionError("the approval workflow must never call Threads")

    for name in (
        "create_text_container",
        "publish_container",
        "fetch_media",
        "fetch_media_insights",
        "fetch_profile",
    ):
        monkeypatch.setattr(ThreadsClient, name, _refuse, raising=False)


@pytest.fixture
def articles(session: Session) -> list[Article]:
    rows = []
    for article_id in (21, 22, 23, 24, 25, 26, 27):
        row = Article(
            id=article_id,
            title=f"記事{article_id}",
            slug=f"article-{article_id}",
            body=f"記事{article_id}の本文。",
            status="published",
            published_url=f"{_BASE}/article-{article_id}/",
            published_at=_NOW - timedelta(days=30),
            article_type="informational",
            monetization_mode="supporting",
            wordpress_post_id=str(1000 + article_id),
        )
        session.add(row)
        rows.append(row)
    session.commit()
    return rows


def _proposal(
    session: Session, article: Article, *, seed: str, angle="insight", created_at=None, **kw
) -> ThreadsPostProposal:
    from app.article.draft_promotion_canonical import compute_text_hash

    text = kw.pop("text", f"{article.title}について {seed} の観点で書いた投稿案。")
    row = ThreadsPostProposal(
        source_article_id=article.id,
        source_article_body_hash=compute_text_hash(article.body),
        angle=angle,
        link_mode="none",
        content_text=text,
        character_count=len(text),
        destination_url=None,
        content_seed=(seed * 64)[:64],
        proposal_hash=(seed.upper() * 64)[:64],
        policy_version="t2.1",
        generator_version="threads-proposal-1",
        status=kw.pop("status", TP_AWAITING_APPROVAL),
        created_at=created_at or _NOW - timedelta(hours=2),
        **kw,
    )
    session.add(row)
    session.commit()
    return row


def _service(session, relay, notifier) -> ThreadsApprovalDigestService:
    mobile = MobileApprovalService(
        session, settings=_Settings(), relay_client=relay, notifier=notifier
    )
    return ThreadsApprovalDigestService(session, settings=_Settings(), mobile_approval=mobile)


def _decision(row: MobileApprovalSession, decision: str) -> RelayDecision:
    return RelayDecision.from_payload(
        {
            "relay_session_id": row.relay_session_id,
            "subject_type": row.subject_type,
            "subject_id": row.subject_id,
            "subject_hash": row.subject_hash,
            "subject_version": row.subject_version,
            "decision": decision,
            "decision_reason": None,
            "decided_at": _NOW.isoformat(),
        }
    )


def _count(session: Session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


# == PLAN =====================================================================
def test_plan_writes_nothing_and_sends_nothing(session: Session, articles) -> None:
    for i, article in enumerate(articles[:3]):
        _proposal(session, article, seed=f"p{i}")
    relay, notifier = _FakeRelay(), _Notifier()
    service = _service(session, relay, notifier)

    plan = service.plan(now=_NOW)
    outcome = service.send(now=_NOW, execute=False)

    assert plan.would_send and len(plan.selected) == 3
    assert outcome.sent is False
    assert notifier.sent == [] and relay.sessions == {}
    assert _count(session, MobileApprovalSession) == 0
    assert _count(session, ThreadsApprovalDigest) == 0
    assert _count(session, NotificationDelivery) == 0


# == one email, many proposals ================================================
def test_one_email_carries_several_independent_review_links(session: Session, articles) -> None:
    proposals = [
        _proposal(session, a, seed=f"p{i}", angle=angle)
        for i, (a, angle) in enumerate(
            zip(articles[:3], ("insight", "question", "comparison"), strict=True)
        )
    ]
    relay, notifier = _FakeRelay(), _Notifier()
    outcome = _service(session, relay, notifier).send(now=_NOW, execute=True)

    assert outcome.sent is True
    assert len(notifier.sent) == 1
    sessions = session.scalars(select(MobileApprovalSession)).all()
    assert len(sessions) == 3
    assert {s.subject_id for s in sessions} == {p.id for p in proposals}
    # 提案ごとに別々の capability。
    assert len({s.capability_digest for s in sessions}) == 3
    assert len({s.relay_session_id for s in sessions}) == 3
    digest = session.scalars(select(ThreadsApprovalDigest)).one()
    assert digest.outcome == DIGEST_SENT
    assert {s.approval_digest_id for s in sessions} == {digest.id}

    body = notifier.sent[0]["body"]
    for s in sessions:
        assert s.relay_session_id in body
    assert body.count("確認   :") == 3


def test_the_email_offers_no_approve_all(session: Session, articles) -> None:
    for i, article in enumerate(articles[:3]):
        _proposal(session, article, seed=f"p{i}")
    notifier = _Notifier()
    _service(session, _FakeRelay(), notifier).send(now=_NOW, execute=True)
    message = notifier.sent[0]
    combined = (message["body"] + message["html"]).lower()
    assert "approve all" not in combined
    assert "一括承認はありません" in message["body"]
    assert "approve" not in message["subject"].lower()


def test_more_than_five_prepared_proposals_still_send_one_email_of_five(
    session: Session, articles
) -> None:
    for i, article in enumerate(articles):
        _proposal(session, article, seed=f"p{i}", angle=f"angle{i}")
    notifier = _Notifier()
    outcome = _service(session, _FakeRelay(), notifier).send(now=_NOW, execute=True)

    assert len(notifier.sent) == 1
    assert len(outcome.proposal_ids) == 5
    remaining = session.scalars(
        select(ThreadsPostProposal).where(ThreadsPostProposal.approval_request_sent_at.is_(None))
    ).all()
    assert len(remaining) == 2  # 消えずに在庫に残る
    assert all(p.status == TP_AWAITING_APPROVAL for p in remaining)


# == TTL ======================================================================
def test_ttl_starts_when_the_request_is_sent_not_when_the_proposal_was_made(
    session: Session, articles
) -> None:
    """夜中 (10 時間前) に作った提案でも、期限は送った時刻から 24 時間。"""

    proposal = _proposal(session, articles[0], seed="night", created_at=_NOW - timedelta(hours=10))
    _service(session, _FakeRelay(), _Notifier()).send(now=_NOW, execute=True)

    row = session.scalars(select(MobileApprovalSession)).one()
    expires = row.expires_at.replace(tzinfo=UTC)
    assert expires == _NOW + timedelta(hours=24)
    session.refresh(proposal)
    assert proposal.approval_request_sent_at is not None
    assert row.request_sent_at is not None


def test_approval_request_sent_at_is_the_delivery_time(session: Session, articles) -> None:
    proposal = _proposal(session, articles[0], seed="a")
    _service(session, _FakeRelay(), _Notifier()).send(now=_NOW, execute=True)

    delivery = session.scalars(
        select(NotificationDelivery).where(
            NotificationDelivery.notification_type == NOTIFICATION_APPROVAL_DIGEST
        )
    ).one()
    session.refresh(proposal)
    assert proposal.approval_request_sent_at == delivery.finished_at


# == delivery failure ==========================================================
def test_an_undelivered_digest_revokes_every_session_and_returns_to_stock(
    session: Session, articles
) -> None:
    proposals = [_proposal(session, a, seed=f"p{i}") for i, a in enumerate(articles[:3])]
    relay = _FakeRelay()
    outcome = _service(session, relay, _Notifier(delivered=False)).send(now=_NOW, execute=True)

    assert outcome.sent is False
    assert {s.state for s in session.scalars(select(MobileApprovalSession))} == {MA_REVOKED}
    assert len(relay.revoked) == 3
    for proposal in proposals:
        session.refresh(proposal)
        assert proposal.approval_request_sent_at is None
        assert proposal.status == TP_AWAITING_APPROVAL
    assert session.scalars(select(ThreadsApprovalDigest)).one().outcome == DIGEST_FAILED


def test_one_relay_failure_does_not_stop_the_other_requests(session: Session, articles) -> None:
    a, b, c = (_proposal(session, x, seed=f"p{i}") for i, x in enumerate(articles[:3]))
    relay = _FakeRelay()
    relay.fail_for = {b.id}
    outcome = _service(session, relay, _Notifier()).send(now=_NOW, execute=True)

    assert outcome.sent is True
    assert set(outcome.proposal_ids) == {a.id, c.id}
    assert outcome.skipped[0]["proposal_id"] == b.id


# == individual decisions =====================================================
def test_each_proposal_is_decided_independently(session: Session, articles) -> None:
    a, b, c = (_proposal(session, x, seed=f"p{i}") for i, x in enumerate(articles[:3]))
    relay, notifier = _FakeRelay(), _Notifier()
    mobile = MobileApprovalService(
        session, settings=_Settings(), relay_client=relay, notifier=notifier
    )
    ThreadsApprovalDigestService(session, settings=_Settings(), mobile_approval=mobile).send(
        now=_NOW, execute=True
    )
    rows = {s.subject_id: s for s in session.scalars(select(MobileApprovalSession))}

    # A は承認、B は却下。C には何もしない。
    relay.decisions = [_decision(rows[a.id], "approved"), _decision(rows[b.id], "rejected")]
    mobile.sync(execute=True, now=_NOW + timedelta(minutes=5))

    for p in (a, b, c):
        session.refresh(p)
    assert a.status == TP_APPROVED
    assert b.status == TP_REJECTED
    assert c.status == TP_AWAITING_APPROVAL
    session.refresh(rows[c.id])
    assert rows[c.id].state == MA_PENDING


def test_approval_records_approved_at_and_publishes_nothing(session: Session, articles) -> None:
    proposal = _proposal(session, articles[0], seed="a")
    relay = _FakeRelay()
    mobile = MobileApprovalService(
        session, settings=_Settings(), relay_client=relay, notifier=_Notifier()
    )
    ThreadsApprovalDigestService(session, settings=_Settings(), mobile_approval=mobile).send(
        now=_NOW, execute=True
    )
    row = session.scalars(select(MobileApprovalSession)).one()
    relay.decisions = [_decision(row, "approved")]
    accepted = _NOW + timedelta(minutes=7)
    mobile.sync(execute=True, now=accepted)

    session.refresh(proposal)
    assert proposal.status == TP_APPROVED
    assert proposal.approved_at.replace(tzinfo=UTC) == accepted
    session.refresh(row)
    assert row.state == MA_SYNCHRONIZED
    # 承認は queue に入るだけ。公開の行も試行も作られない。
    assert _count(session, ThreadsPublication) == 0
    assert _count(session, ThreadsPublicationAttempt) == 0


# == revocation ================================================================
def test_a_stale_proposal_is_revoked_without_touching_the_others(
    session: Session, articles
) -> None:
    a, b, c = (_proposal(session, x, seed=f"p{i}") for i, x in enumerate(articles[:3]))
    relay = _FakeRelay()
    service = _service(session, relay, _Notifier())
    service.send(now=_NOW, execute=True)

    articles[0].body = "記事が書き換わった。"
    session.commit()
    result = service.housekeeping(now=_NOW + timedelta(minutes=30), execute=True)

    states = {s.subject_id: s.state for s in session.scalars(select(MobileApprovalSession))}
    assert states == {a.id: MA_REVOKED, b.id: MA_PENDING, c.id: MA_PENDING}
    assert [item["proposal_id"] for item in result["revoke"]] == [a.id]
    assert len(relay.revoked) == 1


def test_an_expired_proposal_is_revoked_alone(session: Session, articles) -> None:
    a = _proposal(session, articles[0], seed="a", expires_at=_NOW + timedelta(hours=3))
    b = _proposal(session, articles[1], seed="b")
    service = _service(session, _FakeRelay(), _Notifier())
    service.send(now=_NOW, execute=True)

    service.housekeeping(now=_NOW + timedelta(hours=4), execute=True)
    states = {s.subject_id: s.state for s in session.scalars(select(MobileApprovalSession))}
    assert states == {a.id: MA_REVOKED, b.id: MA_PENDING}


def test_a_lapsed_session_expires_so_the_proposal_can_be_requested_again(
    session: Session, articles
) -> None:
    proposal = _proposal(session, articles[0], seed="a")
    service = _service(session, _FakeRelay(), _Notifier())
    service.send(now=_NOW, execute=True)

    later = _NOW + timedelta(hours=25)
    service.housekeeping(now=later, execute=True)
    assert session.scalars(select(MobileApprovalSession)).one().state == MA_EXPIRED

    plan = service.plan(now=later)
    assert [i.candidate.proposal_id for i in plan.selected] == [proposal.id]


def test_an_active_request_is_not_requested_twice(session: Session, articles) -> None:
    _proposal(session, articles[0], seed="a")
    service = _service(session, _FakeRelay(), _Notifier())
    service.send(now=_NOW, execute=True)

    plan = service.plan(now=_NOW + timedelta(hours=2))
    assert plan.selected == ()
    assert [i.reason for i in plan.suppressed] == ["already_requested"]


# == security ==================================================================
def test_the_raw_capability_is_never_stored_or_sent_to_the_relay(
    session: Session, articles
) -> None:
    for i, article in enumerate(articles[:2]):
        _proposal(session, article, seed=f"p{i}")
    relay, notifier = _FakeRelay(), _Notifier()
    _service(session, relay, notifier).send(now=_NOW, execute=True)

    body = notifier.sent[0]["body"]
    capabilities = [line.split("#", 1)[1] for line in body.splitlines() if "確認   :" in line]
    assert len(capabilities) == 2
    stored = " ".join(
        str(v)
        for row in session.scalars(select(MobileApprovalSession))
        for v in (row.capability_digest, row.capability_binding, row.snapshot_json)
    )
    delivery = session.scalars(select(NotificationDelivery)).one()
    for secret in capabilities:
        assert secret not in stored
        assert secret not in str(relay.sessions)
        assert secret not in (delivery.subject + str(delivery.detail_json))
    assert "http" not in delivery.subject


def test_no_secret_reaches_the_email_or_the_delivery_record(session: Session, articles) -> None:
    _proposal(session, articles[0], seed="a")
    notifier = _Notifier()
    _service(session, _FakeRelay(), notifier).send(now=_NOW, execute=True)
    delivery = session.scalars(select(NotificationDelivery)).one()
    everything = notifier.sent[0]["body"] + notifier.sent[0]["html"] + str(delivery.detail_json)
    for secret in (_TOKEN, "smtp-password-never-stored", "approval-relay-secret"):
        assert secret not in everything


# == windows ===================================================================
def test_nothing_is_sent_at_night(session: Session, articles) -> None:
    _proposal(session, articles[0], seed="a")
    notifier = _Notifier()
    night = datetime(2026, 9, 25, 13, 30, tzinfo=UTC)  # 22:30 JST
    outcome = _service(session, _FakeRelay(), notifier).send(now=night, execute=True)
    assert outcome.sent is False
    assert "outside_notification_window" in outcome.reason
    assert notifier.sent == []
    assert _count(session, MobileApprovalSession) == 0


def test_a_second_send_within_the_cooldown_sends_nothing(session: Session, articles) -> None:
    _proposal(session, articles[0], seed="a")
    notifier = _Notifier()
    service = _service(session, _FakeRelay(), notifier)
    service.send(now=_NOW, execute=True)
    _proposal(session, articles[1], seed="b", created_at=_NOW - timedelta(hours=3))

    again = service.send(now=_NOW + timedelta(minutes=20), execute=True)
    assert again.sent is False
    assert "cooldown" in again.reason
    assert len(notifier.sent) == 1


# == manual notification-window override (T4.2 follow-up) =====================
_NIGHT = datetime(2026, 9, 25, 14, 30, tzinfo=UTC)  # 23:30 JST


def _override():
    from app.social.threads.digest import ManualWindowOverride

    return ManualWindowOverride(reason="T4.2 production approval-digest pilot")


def test_an_explicit_override_sends_at_night_and_is_audited(session: Session, articles) -> None:
    _proposal(session, articles[0], seed="a", created_at=_NIGHT - timedelta(hours=3))
    notifier = _Notifier()
    outcome = _service(session, _FakeRelay(), notifier).send(
        now=_NIGHT, execute=True, window_override=_override()
    )

    assert outcome.sent is True
    assert len(notifier.sent) == 1
    digest = session.scalars(select(ThreadsApprovalDigest)).one()
    assert digest.window_override_reason == "T4.2 production approval-digest pilot"
    assert digest.window_override_at.replace(tzinfo=UTC) == _NIGHT
    delivery = session.scalars(select(NotificationDelivery)).one()
    assert delivery.detail_json["notification_window_overridden"] is True
    assert delivery.detail_json["window_override_reason"] == (
        "T4.2 production approval-digest pilot"
    )
    # 期限は実際の送信時刻 (23:30) から 24 時間。
    row = session.scalars(select(MobileApprovalSession)).one()
    assert row.expires_at.replace(tzinfo=UTC) == _NIGHT + timedelta(hours=24)


def test_without_the_override_nothing_is_sent_at_night(session: Session, articles) -> None:
    _proposal(session, articles[0], seed="a", created_at=_NIGHT - timedelta(hours=3))
    notifier = _Notifier()
    outcome = _service(session, _FakeRelay(), notifier).send(now=_NIGHT, execute=True)
    assert outcome.sent is False
    assert notifier.sent == []
    assert _count(session, ThreadsApprovalDigest) == 0


def test_a_normal_daytime_digest_records_no_override(session: Session, articles) -> None:
    _proposal(session, articles[0], seed="a")
    _service(session, _FakeRelay(), _Notifier()).send(
        now=_NOW, execute=True, window_override=_override()
    )
    digest = session.scalars(select(ThreadsApprovalDigest)).one()
    # 窓の中なら上書きは何も飛ばしていない。記録にも上書きとして残さない。
    assert digest.window_override_reason is None
    assert digest.window_override_at is None


def test_the_override_keeps_every_other_rule(session: Session, articles) -> None:
    from app.services.threads_queue_control_service import ThreadsQueueControlService

    held = _proposal(session, articles[0], seed="h", created_at=_NIGHT - timedelta(hours=3))
    ThreadsQueueControlService(session).hold(held.id, reason="not yet", now=_NIGHT)
    stale = _proposal(session, articles[1], seed="s", created_at=_NIGHT - timedelta(hours=3))
    articles[1].body = "書き換わった本文。"
    session.commit()
    _proposal(
        session,
        articles[2],
        seed="e",
        created_at=_NIGHT - timedelta(hours=3),
        expires_at=_NIGHT - timedelta(minutes=5),
    )
    notifier = _Notifier()
    outcome = _service(session, _FakeRelay(), notifier).send(
        now=_NIGHT, execute=True, window_override=_override()
    )
    assert outcome.sent is False
    assert notifier.sent == []
    assert _count(session, MobileApprovalSession) == 0
    assert stale.id  # 在庫には残っている


def test_decisions_stay_individual_after_an_override(session: Session, articles) -> None:
    a = _proposal(session, articles[0], seed="a", created_at=_NIGHT - timedelta(hours=3))
    b = _proposal(session, articles[1], seed="b", created_at=_NIGHT - timedelta(hours=3))
    relay = _FakeRelay()
    mobile = MobileApprovalService(
        session, settings=_Settings(), relay_client=relay, notifier=_Notifier()
    )
    ThreadsApprovalDigestService(session, settings=_Settings(), mobile_approval=mobile).send(
        now=_NIGHT, execute=True, window_override=_override()
    )
    rows = {s.subject_id: s for s in session.scalars(select(MobileApprovalSession))}
    assert len({s.capability_digest for s in rows.values()}) == 2
    relay.decisions = [_decision(rows[a.id], "rejected")]
    mobile.sync(execute=True, now=_NIGHT + timedelta(minutes=5))
    session.refresh(a)
    session.refresh(b)
    assert a.status == TP_REJECTED
    assert b.status == TP_AWAITING_APPROVAL


# -- the CLI is the only way in -----------------------------------------------
def _factory(session: Session):
    class _Scoped:
        def __call__(self):
            return self

        def __enter__(self):
            return session

        def __exit__(self, *_exc):
            return False

    return _Scoped()


def test_the_cli_refuses_an_override_without_a_reason(session: Session, articles, capsys) -> None:
    from scripts.plan_threads_approval_digest import EXIT_REFUSED, main

    _proposal(session, articles[0], seed="a")
    notifier = _Notifier()
    for argv in (
        ["--execute", "--override-notification-window"],
        ["--execute", "--override-notification-window", "--override-reason", "   "],
        ["--execute", "--override-reason", "a reason but no flag"],
    ):
        code = main(
            argv,
            session_factory=_factory(session),
            settings=_Settings(),
            notifier=notifier,
            relay_client=_FakeRelay(),
        )
        assert code == EXIT_REFUSED
        assert "refused" in capsys.readouterr().out
    assert notifier.sent == []
    assert _count(session, ThreadsApprovalDigest) == 0
    assert _count(session, MobileApprovalSession) == 0


def test_the_cli_plan_shows_the_override_fields(session: Session, articles, capsys) -> None:
    from scripts.plan_threads_approval_digest import EXIT_OK, main

    _proposal(session, articles[0], seed="a")
    code = main(
        ["--override-notification-window", "--override-reason", "pilot"],
        session_factory=_factory(session),
        settings=_Settings(),
        notifier=_Notifier(),
        relay_client=_FakeRelay(),
    )
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "manual_override_requested = true" in out
    assert "override_reason           = pilot" in out
    assert "notification_window_open" in out
    assert "would_send_email" in out
    assert _count(session, ThreadsApprovalDigest) == 0  # PLAN は何も書かない


def test_the_resident_worker_cannot_override_the_window(session: Session, articles) -> None:
    """worker には上書きの経路そのものが無い。送信フラグを立てても夜は送らない。"""

    import inspect

    from app.services.threads_worker_service import ThreadsWorkerService

    for signature in (
        inspect.signature(ThreadsWorkerService.__init__),
        inspect.signature(ThreadsWorkerService.build_worker),
    ):
        assert not any("override" in name for name in signature.parameters)

    _proposal(session, articles[0], seed="a", created_at=_NIGHT - timedelta(hours=3))
    notifier = _Notifier()
    worker = ThreadsWorkerService(
        _factory(session),
        settings=_Settings(),
        send_approval_digests=True,
        notifier=notifier,
        relay_client=_FakeRelay(),
    )
    result = worker.handlers()["approval_notification_flush"](_NIGHT)
    assert result.summary["emails_sent"] == 0
    assert "outside_notification_window" in result.summary["waiting_for"]
    assert notifier.sent == []


def test_the_worker_cli_has_no_override_flag() -> None:
    from scripts.run_threads_worker import main

    with pytest.raises(SystemExit):
        main(["--once", "--override-notification-window"])


# == one authoritative clock for expiry (T5.1) =================================
# 期限切れの判定と TTL の起点は、呼び出し側が渡した ``now`` だけで決まる。旧実装は
# issue() → plan() で時刻を落とし、期限切れの判定だけが実時計を見ていた
# (2026-09-25 04:00 UTC を過ぎて test_an_expired_proposal_is_revoked_alone が落ちた)。
# 実時計から大きく離れた時刻で、両方向を固定する。
def _mobile(session: Session) -> MobileApprovalService:
    return MobileApprovalService(
        session, settings=_Settings(), relay_client=_FakeRelay(), notifier=_Notifier()
    )


@pytest.mark.parametrize(
    "injected",
    [
        datetime(2020, 1, 6, 1, 0, tzinfo=UTC),  # 実時計より遥か過去
        datetime(2099, 1, 6, 1, 0, tzinfo=UTC),  # 実時計より遥か未来
    ],
)
def test_expiry_is_judged_by_the_injected_clock_only(session: Session, articles, injected) -> None:
    from app.services.mobile_approval_service import SUBJECT_THREADS_POST

    live = _proposal(
        session,
        articles[0],
        seed="live",
        created_at=injected - timedelta(hours=2),
        expires_at=injected + timedelta(hours=3),
    )
    lapsed = _proposal(
        session,
        articles[1],
        seed="lapsed",
        created_at=injected - timedelta(hours=5),
        expires_at=injected - timedelta(minutes=1),
    )
    mobile = _mobile(session)

    assert mobile.plan(subject_type=SUBJECT_THREADS_POST, subject_id=live.id, now=injected).ok
    blocked = mobile.plan(subject_type=SUBJECT_THREADS_POST, subject_id=lapsed.id, now=injected)
    assert not blocked.ok
    assert any("expired" in reason for reason in blocked.blocked_reasons)

    issued = mobile.issue(
        subject_type=SUBJECT_THREADS_POST, subject_id=live.id, ttl_hours=24, now=injected
    )
    # TTL も同じ時計から数える。
    assert abs(_aware(issued.row.expires_at) - (injected + timedelta(hours=24))) < timedelta(
        seconds=1
    )


def test_the_expired_revocation_does_not_depend_on_the_real_date(
    session: Session, articles
) -> None:
    """test_an_expired_proposal_is_revoked_alone を、実時計から離れた時刻で再現する。"""

    now = datetime(2099, 3, 2, 1, 0, tzinfo=UTC)  # 10:00 JST、通知の時間帯の中
    a = _proposal(
        session,
        articles[0],
        seed="a",
        created_at=now - timedelta(hours=2),
        expires_at=now + timedelta(hours=3),
    )
    b = _proposal(session, articles[1], seed="b", created_at=now - timedelta(hours=2))
    service = _service(session, _FakeRelay(), _Notifier())
    service.send(now=now, execute=True)
    service.housekeeping(now=now + timedelta(hours=4), execute=True)
    states = {s.subject_id: s.state for s in session.scalars(select(MobileApprovalSession))}
    assert states == {a.id: MA_REVOKED, b.id: MA_PENDING}


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
