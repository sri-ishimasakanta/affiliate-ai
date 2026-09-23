"""モバイル承認の送信と取り込み (C8.8)。

pin する契約:

- 依頼は **明示的に指定された** 変更要求にだけ作られる。
- 生の capability は DB にも通知履歴にも残らない。
- 決定は必ず既存の ``ChangeRequestService`` を通って記録される。
- 携帯からの承認は「人の決定」として残る (``human-mobile``)。
- 提案 hash / 版が変われば反映しない。陳腐化していれば反映しない。
- 中継の再送で二重に承認しない。
- **適用はしない。** WordPress にも ``/go/`` にも触れない。
- スケジューラは決定を作り出せない。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.approval.relay_client import RelayDecision, RelayError, build_review_url
from app.models import (
    CR_APPROVED,
    CR_AWAITING_APPROVAL,
    CR_REJECTED,
    DECIDED_BY_MOBILE,
    MA_PENDING,
    MA_REVOKED,
    MA_STALE,
    MA_SYNCHRONIZED,
    NOTIFICATION_APPROVAL_REQUEST,
    Article,
    ChangeApplication,
    ChangeRequest,
    ChangeRequestApproval,
    MobileApprovalSession,
    NotificationDelivery,
    SeoImprovementCandidate,
    SeoImprovementRun,
)
from app.operations.notifications import NotificationResult
from app.services.change_request_service import ChangeRequestService
from app.services.mobile_approval_service import MobileApprovalError, MobileApprovalService

_BASE = "https://bizfluxlab.com"
_NOW = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)

_SOURCE_BODY = """記事の冒頭。

導入の段落です。ここでは手順を整理します。

## 事前準備

参考: [公式](https://official.example.jp/docs)
"""


class _Settings:
    wordpress_base_url = _BASE
    # 承認中継は専用の secret を使う (C8.8.1)。affiliate runtime の鍵とは別。
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

    @property
    def operations_email_recipients(self):
        return ["owner@example.com"]


class _FakeRelay:
    """中継のふりをするだけ。ネットワークには出ない。"""

    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.decisions: list[RelayDecision] = []
        self.acked: list[str] = []
        self.revoked: list[str] = []
        self.fail_on_create = False

    def create_session(self, **kwargs):
        if self.fail_on_create:
            raise RelayError("invalid_payload")
        self.sessions[kwargs["relay_session_id"]] = dict(kwargs)
        return {"relay_session_id": kwargs["relay_session_id"], "state": "pending"}

    def fetch_decisions(self):
        return list(self.decisions)

    def acknowledge(self, *, relay_session_id, local_outcome):
        self.acked.append(relay_session_id)
        return {"state": "consumed"}

    def revoke(self, *, relay_session_id, reason):
        self.revoked.append(relay_session_id)
        return {"state": "revoked"}


class _RecordingNotifier:
    name = "email"

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send_report(self, *, subject, body, html_body=None):
        self.sent.append({"subject": subject, "body": body, "html": html_body})
        return NotificationResult(self.name, True)


def _article(session: Session, article_id: int, slug: str, *, body: str) -> Article:
    article = Article(
        id=article_id,
        title=f"記事{article_id}",
        slug=slug,
        body=body,
        status="published",
        published_url=f"{_BASE}/{slug}/",
        published_at=_NOW - timedelta(days=30),
        article_type="informational",
        monetization_mode="supporting",
        wordpress_post_id=str(1000 + article_id),
    )
    session.add(article)
    session.commit()
    return article


@pytest.fixture
def awaiting(session: Session):
    source = _article(session, 18, "rpa-implementation", body=_SOURCE_BODY)
    target = _article(session, 17, "rpa-comparison", body="# 比較\n\n本文\n")
    run = SeoImprovementRun(
        policy_version="seo-policy-1",
        window_start=date(2026, 8, 1),
        window_end=date(2026, 8, 31),
        evaluated_article_count=2,
        candidate_count=1,
    )
    session.add(run)
    session.commit()
    candidate = SeoImprovementCandidate(
        seo_improvement_run_id=run.id,
        article_id=source.id,
        candidate_type="INTERNAL_LINK_OPPORTUNITY",
        reason_code="DEFERRED_PAIR",
        priority="low",
        evidence_strength="structural",
        suggested_action="内部リンクを 1 本足す",
        evidence_json={"target_article_id": target.id, "relation": "deferred pair"},
        dedupe_key="18:INTERNAL_LINK_OPPORTUNITY:17",
    )
    session.add(candidate)
    session.commit()
    request = ChangeRequestService(session).propose_from_seo_candidate(
        candidate_id=candidate.id, now=_NOW
    )
    return source, target, request


@pytest.fixture
def relay():
    return _FakeRelay()


@pytest.fixture
def notifier():
    return _RecordingNotifier()


def _service(session, relay, notifier) -> MobileApprovalService:
    return MobileApprovalService(
        session, settings=_Settings(), relay_client=relay, notifier=notifier
    )


def _decision(row: MobileApprovalSession, decision="approved", reason=None, **overrides):
    payload = {
        "relay_session_id": row.relay_session_id,
        "subject_type": row.subject_type,
        "subject_id": row.subject_id,
        "subject_hash": row.subject_hash,
        "subject_version": row.subject_version,
        "decision": decision,
        "decision_reason": reason,
        "decided_at": _NOW.isoformat(),
    }
    payload.update(overrides)
    return RelayDecision.from_payload(payload)


# -- sending -------------------------------------------------------------------
def test_plan_does_not_create_a_session_or_send(session, awaiting, relay, notifier) -> None:
    _, _, request = awaiting

    prepared = _service(session, relay, notifier).plan(change_request_id=request.id)

    assert prepared.ok
    assert prepared.snapshot["subject_hash_short"] == request.proposal_hash[:16]
    assert relay.sessions == {}
    assert notifier.sent == []
    assert session.scalars(select(MobileApprovalSession)).all() == []


def test_send_creates_one_pending_session_and_one_email(session, awaiting, relay, notifier) -> None:
    _, _, request = awaiting

    row = _service(session, relay, notifier).send(change_request_id=request.id, now=_NOW)

    assert row.state == MA_PENDING
    assert row.subject_hash == request.proposal_hash
    assert len(relay.sessions) == 1
    assert len(notifier.sent) == 1
    # 承認はされていない。
    assert session.get(ChangeRequest, request.id).status == CR_AWAITING_APPROVAL
    assert session.scalars(select(ChangeRequestApproval)).all() == []


def test_a_second_send_is_refused_while_one_is_active(session, awaiting, relay, notifier) -> None:
    _, _, request = awaiting
    service = _service(session, relay, notifier)
    service.send(change_request_id=request.id, now=_NOW)

    with pytest.raises(MobileApprovalError, match="already exists"):
        service.send(change_request_id=request.id, now=_NOW)

    assert len(notifier.sent) == 1


def test_a_revoked_session_frees_the_request_for_a_new_send(
    session, awaiting, relay, notifier
) -> None:
    _, _, request = awaiting
    service = _service(session, relay, notifier)
    first = service.send(change_request_id=request.id, now=_NOW)

    service.revoke(session_id=first.id, reason="作り直す")

    assert session.get(MobileApprovalSession, first.id).state == MA_REVOKED
    assert relay.revoked == [first.relay_session_id]
    second = service.send(change_request_id=request.id, now=_NOW)
    assert second.id != first.id


def test_an_already_approved_request_cannot_be_sent(session, awaiting, relay, notifier) -> None:
    _, _, request = awaiting
    ChangeRequestService(session).approve(request.id, proposal_hash=request.proposal_hash, now=_NOW)

    with pytest.raises(MobileApprovalError, match="only a request awaiting approval"):
        _service(session, relay, notifier).send(change_request_id=request.id, now=_NOW)


def test_a_stale_request_cannot_be_sent(session, awaiting, relay, notifier) -> None:
    source, _, request = awaiting
    source.body = source.body + "\n人が編集した\n"
    session.commit()

    with pytest.raises(MobileApprovalError, match="body changed"):
        _service(session, relay, notifier).send(change_request_id=request.id, now=_NOW)


# -- capability leak prevention -------------------------------------------------
def test_the_raw_capability_never_reaches_the_database(session, awaiting, relay, notifier) -> None:
    _, _, request = awaiting
    row = _service(session, relay, notifier).send(change_request_id=request.id, now=_NOW)

    # メール本文の URL から実際の capability を取り出して照合する。
    import re

    match = re.search(r"https://\S*/bfl-approval/\w+#(\S+)", notifier.sent[0]["body"])
    assert match is not None
    capability = match.group(1)
    assert len(capability) >= 43

    stored = repr(
        {
            "session": {c.name: getattr(row, c.name) for c in row.__table__.columns},
            "relay": relay.sessions,
            "deliveries": [
                {c.name: getattr(d, c.name) for c in d.__table__.columns}
                for d in session.scalars(select(NotificationDelivery)).all()
            ],
            "events": [e.detail for e in row.events],
        }
    )
    assert capability not in stored
    # 中継が受け取るのも digest だけ。
    assert all(
        "capability" not in k or k == "capability_digest"
        for k in relay.sessions[row.relay_session_id]
    )


def test_the_delivery_record_holds_no_review_url(session, awaiting, relay, notifier) -> None:
    _, _, request = awaiting
    _service(session, relay, notifier).send(change_request_id=request.id, now=_NOW)

    delivery = session.scalars(select(NotificationDelivery)).one()

    assert delivery.notification_type == NOTIFICATION_APPROVAL_REQUEST
    assert "bfl-approval" not in delivery.subject
    assert "#" not in delivery.subject
    assert "capability" not in repr(delivery.detail_json)


def test_the_review_url_puts_the_capability_in_the_fragment() -> None:
    url = build_review_url(_Settings(), relay_session_id="a" * 32, capability="c" * 43)

    path, fragment = url.split("#", 1)
    assert path == f"{_BASE}/bfl-approval/{'a' * 32}"
    assert fragment == "c" * 43
    # query string には入れない (プロキシ/アクセスログに残るため)。
    assert "?" not in url


# -- the email ------------------------------------------------------------------
def test_the_email_opens_the_review_page_but_cannot_approve(
    session, awaiting, relay, notifier
) -> None:
    _, _, request = awaiting
    _service(session, relay, notifier).send(change_request_id=request.id, now=_NOW)
    mail = notifier.sent[0]

    assert mail["subject"] == f"[BizFluxLab] Approval required - Change request {request.id}"
    assert "内容を確認" in mail["html"]
    # ワンクリック承認の導線は存在しない。
    for forbidden in ("approve=", "decision=approved", "/approve?", "action=approve"):
        assert forbidden not in mail["body"]
        assert forbidden not in mail["html"]
    # plain text の代替も必ずある。
    assert "内容を確認する:" in mail["body"]
    assert "開いても承認にはならない" in mail["body"]


def test_the_email_html_escapes_proposal_text(session, awaiting, relay, notifier) -> None:
    source, _, _ = awaiting
    # 提案の理由に HTML を混ぜても、そのまま描画されない。
    request = session.scalars(select(ChangeRequest)).one()
    request.rationale = "<script>alert(1)</script>"
    session.commit()

    _service(session, relay, notifier).send(change_request_id=request.id, now=_NOW)

    html = notifier.sent[0]["html"]
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# -- synchronization ------------------------------------------------------------
def test_sync_plan_records_nothing(session, awaiting, relay, notifier) -> None:
    _, _, request = awaiting
    service = _service(session, relay, notifier)
    row = service.send(change_request_id=request.id, now=_NOW)
    relay.decisions = [_decision(row)]

    outcome = service.sync(execute=False, now=_NOW)

    assert outcome.fetched == 1
    assert outcome.applied == 0
    assert outcome.details[0]["result"] == "would_apply"
    assert session.get(ChangeRequest, request.id).status == CR_AWAITING_APPROVAL
    assert session.scalars(select(ChangeRequestApproval)).all() == []


def test_a_mobile_approval_is_recorded_through_the_c9_service(
    session, awaiting, relay, notifier
) -> None:
    _, _, request = awaiting
    service = _service(session, relay, notifier)
    row = service.send(change_request_id=request.id, now=_NOW)
    relay.decisions = [_decision(row)]

    outcome = service.sync(execute=True, now=_NOW)

    assert outcome.applied == 1
    stored = session.get(ChangeRequest, request.id)
    assert stored.status == CR_APPROVED
    approval = session.scalars(select(ChangeRequestApproval)).one()
    # 人の決定として残り、経路だけが区別される。
    assert approval.decided_by == DECIDED_BY_MOBILE
    assert approval.decision == "approved"
    assert approval.approved_proposal_hash == request.proposal_hash
    assert approval.approved_proposal_version == request.proposal_version
    assert session.get(MobileApprovalSession, row.id).state == MA_SYNCHRONIZED
    assert relay.acked == [row.relay_session_id]


def test_a_mobile_rejection_keeps_the_human_reason(session, awaiting, relay, notifier) -> None:
    _, _, request = awaiting
    service = _service(session, relay, notifier)
    row = service.send(change_request_id=request.id, now=_NOW)
    relay.decisions = [_decision(row, decision="rejected", reason="リンク先が弱い")]

    service.sync(execute=True, now=_NOW)

    approval = session.scalars(select(ChangeRequestApproval)).one()
    assert approval.decision == "rejected"
    assert approval.reason == "リンク先が弱い"
    assert approval.decided_by == DECIDED_BY_MOBILE
    assert session.get(ChangeRequest, request.id).status == CR_REJECTED


def test_a_replayed_decision_cannot_approve_twice(session, awaiting, relay, notifier) -> None:
    _, _, request = awaiting
    service = _service(session, relay, notifier)
    row = service.send(change_request_id=request.id, now=_NOW)
    relay.decisions = [_decision(row)]

    service.sync(execute=True, now=_NOW)
    second = service.sync(execute=True, now=_NOW)

    assert second.applied == 0
    assert second.details[0]["reason"] == "already synchronized"
    assert len(session.scalars(select(ChangeRequestApproval)).all()) == 1


def test_a_changed_proposal_hash_blocks_synchronization(session, awaiting, relay, notifier) -> None:
    _, _, request = awaiting
    service = _service(session, relay, notifier)
    row = service.send(change_request_id=request.id, now=_NOW)
    relay.decisions = [_decision(row)]
    # 提案が差し替わった。
    stored = session.get(ChangeRequest, request.id)
    stored.proposal_hash = "f" * 64
    session.commit()

    outcome = service.sync(execute=True, now=_NOW)

    assert outcome.applied == 0
    assert session.scalars(select(ChangeRequestApproval)).all() == []
    assert session.get(MobileApprovalSession, row.id).state == MA_STALE


def test_a_changed_proposal_version_blocks_synchronization(
    session, awaiting, relay, notifier
) -> None:
    _, _, request = awaiting
    service = _service(session, relay, notifier)
    row = service.send(change_request_id=request.id, now=_NOW)
    relay.decisions = [_decision(row)]
    stored = session.get(ChangeRequest, request.id)
    stored.proposal_version = 2
    session.commit()

    outcome = service.sync(execute=True, now=_NOW)

    assert outcome.applied == 0
    assert session.get(MobileApprovalSession, row.id).state == MA_STALE


def test_an_envelope_that_disagrees_with_the_session_is_refused(
    session, awaiting, relay, notifier
) -> None:
    _, _, request = awaiting
    service = _service(session, relay, notifier)
    row = service.send(change_request_id=request.id, now=_NOW)
    relay.decisions = [_decision(row, subject_hash="e" * 64)]

    outcome = service.sync(execute=True, now=_NOW)

    assert outcome.applied == 0
    assert outcome.details[0]["reason"] == "envelope mismatch"
    assert session.scalars(select(ChangeRequestApproval)).all() == []


def test_a_stale_source_blocks_synchronization(session, awaiting, relay, notifier) -> None:
    source, _, request = awaiting
    service = _service(session, relay, notifier)
    row = service.send(change_request_id=request.id, now=_NOW)
    relay.decisions = [_decision(row)]
    source.body = source.body + "\n人が別途編集した\n"
    session.commit()

    outcome = service.sync(execute=True, now=_NOW)

    assert outcome.applied == 0
    assert session.scalars(select(ChangeRequestApproval)).all() == []
    assert session.get(MobileApprovalSession, row.id).state == MA_STALE


def test_an_expired_session_cannot_be_synchronized(session, awaiting, relay, notifier) -> None:
    _, _, request = awaiting
    service = _service(session, relay, notifier)
    row = service.send(change_request_id=request.id, ttl_hours=1, now=_NOW)
    relay.decisions = [_decision(row)]

    outcome = service.sync(execute=True, now=_NOW + timedelta(hours=2))

    assert outcome.applied == 0
    assert outcome.details[0]["reason"] == "session expired"
    assert session.scalars(select(ChangeRequestApproval)).all() == []


def test_an_already_applied_request_cannot_receive_a_mobile_approval(
    session, awaiting, relay, notifier
) -> None:
    _, _, request = awaiting
    service = _service(session, relay, notifier)
    row = service.send(change_request_id=request.id, now=_NOW)
    relay.decisions = [_decision(row)]
    stored = session.get(ChangeRequest, request.id)
    stored.status = "applied"
    session.commit()

    outcome = service.sync(execute=True, now=_NOW)

    assert outcome.applied == 0
    assert session.scalars(select(ChangeRequestApproval)).all() == []


def test_a_decision_without_a_local_session_is_ignored(session, awaiting, relay, notifier) -> None:
    """中継が知らないセッションの決定を持ってきても、承認は作られない。"""

    _, _, request = awaiting
    service = _service(session, relay, notifier)
    row = service.send(change_request_id=request.id, now=_NOW)
    relay.decisions = [_decision(row, relay_session_id="f" * 32)]

    outcome = service.sync(execute=True, now=_NOW)

    assert outcome.applied == 0
    assert session.scalars(select(ChangeRequestApproval)).all() == []


def test_a_relay_failure_never_creates_an_approval(session, awaiting, relay, notifier) -> None:
    class _Broken(_FakeRelay):
        def fetch_decisions(self):
            raise RelayError("transport failure: TimeoutError")

    service = _service(session, _Broken(), notifier)

    outcome = service.sync(execute=True, now=_NOW)

    assert outcome.failed == 1
    assert outcome.applied == 0
    assert session.scalars(select(ChangeRequestApproval)).all() == []


# -- boundaries -----------------------------------------------------------------
def test_synchronization_never_applies_a_change(session, awaiting, relay, notifier) -> None:
    source, _, request = awaiting
    body_before = source.body
    service = _service(session, relay, notifier)
    row = service.send(change_request_id=request.id, now=_NOW)
    relay.decisions = [_decision(row)]

    service.sync(execute=True, now=_NOW)

    session.refresh(source)
    assert source.body == body_before
    # 適用の履歴は 1 行も作られない。
    assert session.scalars(select(ChangeApplication)).all() == []


def test_synchronization_performs_no_wordpress_or_go_request(
    session, awaiting, relay, notifier, monkeypatch
) -> None:
    import httpx

    def _boom(*args, **kwargs):  # pragma: no cover
        raise AssertionError("mobile approval must not perform HTTP requests")

    monkeypatch.setattr(httpx, "Client", _boom)
    monkeypatch.setattr(httpx, "get", _boom)
    monkeypatch.setattr(httpx, "post", _boom)

    _, _, request = awaiting
    service = _service(session, relay, notifier)
    row = service.send(change_request_id=request.id, now=_NOW)
    relay.decisions = [_decision(row)]

    outcome = service.sync(execute=True, now=_NOW)

    assert outcome.applied == 1


def test_the_operations_runner_cannot_fabricate_or_apply_an_approval() -> None:
    """C8 は決定を運べるが、作ることも適用することもできない。"""

    import inspect

    from app.services import operations_runner_service as runner_mod

    source = inspect.getsource(runner_mod)

    # 適用経路は相変わらず import されない。
    assert "change_application_service" not in source
    assert "ChangeApplicationService" not in source
    # 承認を直接呼ぶ経路も無い (取り込みは専用 CLI が人の指示で動かす)。
    assert "ChangeRequestService" not in source
    assert ".approve(" not in source


def test_the_sync_service_cannot_decide_on_its_own(session, awaiting, relay, notifier) -> None:
    """中継に決定が無ければ、何も記録されない。"""

    _, _, request = awaiting
    service = _service(session, relay, notifier)
    service.send(change_request_id=request.id, now=_NOW)
    relay.decisions = []

    outcome = service.sync(execute=True, now=_NOW)

    assert outcome.fetched == 0
    assert outcome.applied == 0
    assert session.get(ChangeRequest, request.id).status == CR_AWAITING_APPROVAL
    assert session.scalars(select(ChangeRequestApproval)).all() == []


# -- future subjects ------------------------------------------------------------
def test_the_envelope_can_represent_a_threads_post_without_implementing_threads() -> None:
    from app.approval.review_snapshot import UnsupportedSubjectError, build_snapshot
    from app.models import SUBJECT_THREADS_POST, SUBJECT_TYPES, SUBJECT_TYPES_SUPPORTED_IN_V1

    # 封筒としては表現できる。
    assert SUBJECT_THREADS_POST in SUBJECT_TYPES
    # だが V1 では作れない (Threads は未実装)。
    assert SUBJECT_THREADS_POST not in SUBJECT_TYPES_SUPPORTED_IN_V1
    with pytest.raises(UnsupportedSubjectError):
        build_snapshot(subject_type=SUBJECT_THREADS_POST, subject=object())


def test_a_threads_decision_is_not_synchronized_yet(session, awaiting, relay, notifier) -> None:
    _, _, request = awaiting
    service = _service(session, relay, notifier)
    row = service.send(change_request_id=request.id, now=_NOW)
    relay.decisions = [_decision(row, subject_type="threads_post")]

    outcome = service.sync(execute=True, now=_NOW)

    assert outcome.applied == 0
    assert session.scalars(select(ChangeRequestApproval)).all() == []
