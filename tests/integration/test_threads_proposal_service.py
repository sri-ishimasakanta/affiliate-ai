"""Threads 投稿案の作成と、携帯での承認 (T2)。

pin する契約:

- 提案は明示した記事からしか作らない。検査を通らなければ保存しない。
- 記事本文 hash を凍結し、記事が変われば陳腐化する。
- 同じ内容の提案を二重に作らない。
- 携帯の確認画面には **実際に投稿される文字列そのもの** が出る。
- 承認は「公開してよい」になるだけで、**Threads API を呼ばない**。
- 既存の C9 承認経路は壊れていない。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.approval.relay_client import RelayDecision, RelayError
from app.article.draft_promotion_canonical import compute_text_hash
from app.models import (
    MA_SYNCHRONIZED,
    SUBJECT_THREADS_POST,
    TP_APPROVED,
    TP_AWAITING_APPROVAL,
    TP_REJECTED,
    TP_SUPERSEDED,
    Article,
    MobileApprovalSession,
    ThreadsPostProposal,
)
from app.operations.notifications import NotificationResult
from app.services.mobile_approval_service import MobileApprovalError, MobileApprovalService
from app.services.threads_proposal_service import ThreadsProposalError, ThreadsProposalService
from app.social.threads.proposal import LINK_PLACEHOLDER

_BASE = "https://bizfluxlab.com"
_NOW = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)

_ARTICLE_BODY = """AIガイドラインの記事本文。

体制を先に決めるほうが早い。2026年9月時点で総務省の資料を確認した。

## 手順
"""


class _Settings:
    wordpress_base_url = _BASE
    approval_relay_shared_secret = "approval-relay-secret"
    affiliate_runtime_shared_secret = "affiliate-runtime-secret"
    operations_email_enabled = True
    operations_email_smtp_host = "smtp.example.com"
    operations_email_smtp_port = 587
    operations_email_username = "ops@example.com"
    operations_email_password = "smtp-password"
    operations_email_from = "ops@example.com"
    operations_email_use_starttls = True
    operations_email_subject_prefix = "BizFluxLab"
    operations_webhook_url = None
    # Threads は有効だが、T2 は API を一切呼ばない。
    threads_enabled = True
    threads_user_id = "9876543210"
    threads_access_token = "token-never-used-in-t2"

    @property
    def operations_email_recipients(self):
        return ["owner@example.com"]


class _FakeRelay:
    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.decisions: list[RelayDecision] = []
        self.acked: list[str] = []
        self.revoked: list[str] = []

    def create_session(self, **kwargs):
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


def _generated(*items) -> str:
    return json.dumps({"proposals": list(items)}, ensure_ascii=False)


@pytest.fixture
def article(session: Session) -> Article:
    row = Article(
        id=21,
        title="生成AIガイドライン｜要点と実務上の注意点",
        slug="generative-ai-guidelines",
        body=_ARTICLE_BODY,
        status="published",
        published_url=f"{_BASE}/generative-ai-guidelines/",
        published_at=_NOW - timedelta(days=10),
        article_type="informational",
        monetization_mode="supporting",
        wordpress_post_id="84",
    )
    session.add(row)
    session.commit()
    return row


@pytest.fixture
def relay():
    return _FakeRelay()


@pytest.fixture
def notifier():
    return _RecordingNotifier()


def _service(session) -> ThreadsProposalService:
    return ThreadsProposalService(session)


def _approval(session, relay, notifier) -> MobileApprovalService:
    return MobileApprovalService(
        session, settings=_Settings(), relay_client=relay, notifier=notifier
    )


# -- prompt ---------------------------------------------------------------------
def test_the_prompt_is_deterministic_and_carries_the_article(session, article) -> None:
    first = _service(session).build_prompt(article_id=article.id, angles=["insight"])
    second = _service(session).build_prompt(article_id=article.id, angles=["insight"])

    assert first.prompt_hash == second.prompt_hash
    assert "体制を先に決めるほうが早い" in first.rendered_prompt
    assert "事実の境界" in first.rendered_prompt
    assert first.source_article_body_hash == compute_text_hash(article.body)


def test_an_unpublished_article_cannot_be_used(session, article) -> None:
    article.status = "draft"
    session.commit()

    with pytest.raises(ThreadsProposalError, match="not published"):
        _service(session).build_prompt(article_id=article.id)


# -- plan / persist --------------------------------------------------------------
def test_plan_validates_without_storing(session, article) -> None:
    prepared = _service(session).plan(
        article_id=article.id,
        generated_output=_generated(
            {"angle": "insight", "link_mode": "none", "body": "体制を先に決めたほうが早い。"}
        ),
    )

    assert prepared.acceptable == 1
    assert prepared.candidates[0]["angle"] == "insight"
    assert session.scalars(select(ThreadsPostProposal)).all() == []


def test_persist_stores_awaiting_approval_proposals(session, article) -> None:
    rows = _service(session).persist(
        article_id=article.id,
        generated_output=_generated(
            {"angle": "insight", "link_mode": "none", "body": "体制を先に決めたほうが早い。"},
            {
                "angle": "question",
                "link_mode": "article",
                "body": f"AIのルール、どこから作った？\n体制からが早い。\n{LINK_PLACEHOLDER}",
            },
        ),
        now=_NOW,
    )

    assert len(rows) == 2
    assert {r.status for r in rows} == {TP_AWAITING_APPROVAL}
    assert {r.angle for r in rows} == {"insight", "question"}
    linked = next(r for r in rows if r.angle == "question")
    assert linked.destination_url.startswith(f"{_BASE}/generative-ai-guidelines/?")
    assert "utm_source=threads" in linked.destination_url
    assert linked.destination_url in linked.content_text
    # 記事本文 hash が凍結されている。
    assert all(r.source_article_body_hash == compute_text_hash(article.body) for r in rows)


def test_an_overlength_proposal_is_not_stored(session, article) -> None:
    with pytest.raises(ThreadsProposalError, match="no candidate passed validation"):
        _service(session).persist(
            article_id=article.id,
            generated_output=_generated(
                {"angle": "insight", "link_mode": "none", "body": "あ" * 501}
            ),
        )

    assert session.scalars(select(ThreadsPostProposal)).all() == []


def test_an_affiliate_link_is_not_stored(session, article) -> None:
    with pytest.raises(ThreadsProposalError):
        _service(session).persist(
            article_id=article.id,
            generated_output=_generated(
                {
                    "angle": "insight",
                    "link_mode": "none",
                    "body": f"本文。\n{_BASE}/go/abc123",
                }
            ),
        )

    assert session.scalars(select(ThreadsPostProposal)).all() == []


def test_a_duplicate_proposal_is_refused(session, article) -> None:
    service = _service(session)
    payload = _generated({"angle": "insight", "link_mode": "none", "body": "同じ本文。"})
    service.persist(article_id=article.id, generated_output=payload, now=_NOW)

    prepared = service.plan(article_id=article.id, generated_output=payload)

    assert prepared.acceptable == 0
    assert any("already exists" in e for r in prepared.rejected for e in r["errors"])


def test_duplicates_within_one_batch_are_refused(session, article) -> None:
    prepared = _service(session).plan(
        article_id=article.id,
        generated_output=_generated(
            {"angle": "insight", "link_mode": "none", "body": "同じ本文。"},
            {"angle": "question", "link_mode": "none", "body": "同じ本文。"},
        ),
    )

    assert prepared.acceptable == 1
    assert any("duplicate" in e for r in prepared.rejected for e in r["errors"])


def test_malformed_generator_output_is_refused(session, article) -> None:
    with pytest.raises(ThreadsProposalError, match="could not be read"):
        _service(session).plan(article_id=article.id, generated_output="not json")


# -- staleness -------------------------------------------------------------------
def test_a_changed_article_makes_the_proposal_stale(session, article) -> None:
    service = _service(session)
    row = service.persist(
        article_id=article.id,
        generated_output=_generated({"angle": "insight", "link_mode": "none", "body": "本文。"}),
        now=_NOW,
    )[0]
    assert service.evaluate_staleness(row)[0] is False

    article.body = article.body + "\n追記。\n"
    session.commit()

    stale, reasons = service.evaluate_staleness(row)
    assert stale
    assert any("changed after the proposal" in r for r in reasons)


def test_regeneration_supersedes_instead_of_rewriting(session, article) -> None:
    service = _service(session)
    first = service.persist(
        article_id=article.id,
        generated_output=_generated({"angle": "insight", "link_mode": "none", "body": "初版。"}),
        now=_NOW,
    )[0]
    second = service.persist(
        article_id=article.id,
        generated_output=_generated({"angle": "insight", "link_mode": "none", "body": "改訂版。"}),
        now=_NOW,
    )[0]

    service.supersede(first.id, superseded_by_id=second.id, now=_NOW)

    session.refresh(first)
    assert first.status == TP_SUPERSEDED
    assert first.superseded_by_id == second.id
    # 元の文章は書き換えられていない。
    assert first.content_text == "初版。"


# -- mobile approval -------------------------------------------------------------
def test_a_threads_proposal_can_be_sent_for_mobile_approval(
    session, article, relay, notifier
) -> None:
    proposal = _service(session).persist(
        article_id=article.id,
        generated_output=_generated(
            {"angle": "insight", "link_mode": "none", "body": "体制を先に決めたほうが早い。"}
        ),
        now=_NOW,
    )[0]

    row = _approval(session, relay, notifier).send(
        subject_type=SUBJECT_THREADS_POST, subject_id=proposal.id, now=_NOW
    )

    assert row.subject_type == SUBJECT_THREADS_POST
    assert row.subject_hash == proposal.proposal_hash
    snapshot = relay.sessions[row.relay_session_id]["snapshot"]
    # 人が見るのは、実際に投稿される文字列そのもの。
    assert snapshot["publish_text"] == proposal.content_text
    assert snapshot["character_count"] == proposal.character_count
    assert snapshot["angle"] == "insight"
    assert snapshot["article_title"] == article.title
    assert snapshot["subject_hash_short"] == proposal.proposal_hash[:16]
    # 承認はまだされていない。
    assert session.get(ThreadsPostProposal, proposal.id).status == TP_AWAITING_APPROVAL


def test_the_approval_email_names_the_threads_post(session, article, relay, notifier) -> None:
    proposal = _service(session).persist(
        article_id=article.id,
        generated_output=_generated({"angle": "insight", "link_mode": "none", "body": "本文。"}),
        now=_NOW,
    )[0]

    _approval(session, relay, notifier).send(
        subject_type=SUBJECT_THREADS_POST, subject_id=proposal.id, now=_NOW
    )

    mail = notifier.sent[0]
    assert f"Change request {proposal.id}" in mail["subject"]
    assert "内容を確認" in mail["html"]
    # ワンクリック承認は無い。
    for forbidden in ("decision=approved", "action=approve", "approve="):
        assert forbidden not in mail["body"]
        assert forbidden not in mail["html"]


def test_approving_a_threads_proposal_does_not_publish(
    session, article, relay, notifier, monkeypatch
) -> None:
    """**承認は公開ではない。** Threads API は 1 度も呼ばれない。"""

    import httpx

    def _boom(*args, **kwargs):  # pragma: no cover
        raise AssertionError("approval must never call the Threads API")

    monkeypatch.setattr(httpx, "Client", _boom)
    monkeypatch.setattr(httpx, "post", _boom)

    proposal = _service(session).persist(
        article_id=article.id,
        generated_output=_generated({"angle": "insight", "link_mode": "none", "body": "本文。"}),
        now=_NOW,
    )[0]
    service = _approval(session, relay, notifier)
    row = service.send(subject_type=SUBJECT_THREADS_POST, subject_id=proposal.id, now=_NOW)
    relay.decisions = [
        RelayDecision.from_payload(
            {
                "relay_session_id": row.relay_session_id,
                "subject_type": SUBJECT_THREADS_POST,
                "subject_id": proposal.id,
                "subject_hash": proposal.proposal_hash,
                "subject_version": 1,
                "decision": "approved",
                "decision_reason": None,
                "decided_at": _NOW.isoformat(),
            }
        )
    ]

    outcome = service.sync(execute=True, now=_NOW)

    assert outcome.applied == 1
    stored = session.get(ThreadsPostProposal, proposal.id)
    assert stored.status == TP_APPROVED
    # 承認後も、公開された痕跡はどこにも無い。
    assert stored.content_text == proposal.content_text
    assert session.get(MobileApprovalSession, row.id).state == MA_SYNCHRONIZED


def test_rejecting_a_threads_proposal_records_the_reason(session, article, relay, notifier) -> None:
    proposal = _service(session).persist(
        article_id=article.id,
        generated_output=_generated({"angle": "insight", "link_mode": "none", "body": "本文。"}),
        now=_NOW,
    )[0]
    service = _approval(session, relay, notifier)
    row = service.send(subject_type=SUBJECT_THREADS_POST, subject_id=proposal.id, now=_NOW)
    relay.decisions = [
        RelayDecision.from_payload(
            {
                "relay_session_id": row.relay_session_id,
                "subject_type": SUBJECT_THREADS_POST,
                "subject_id": proposal.id,
                "subject_hash": proposal.proposal_hash,
                "subject_version": 1,
                "decision": "rejected",
                "decision_reason": "硬い",
                "decided_at": _NOW.isoformat(),
            }
        )
    ]

    service.sync(execute=True, now=_NOW)

    stored = session.get(ThreadsPostProposal, proposal.id)
    assert stored.status == TP_REJECTED
    assert stored.status_reason == "硬い"


def test_a_stale_threads_proposal_is_not_approved(session, article, relay, notifier) -> None:
    proposal = _service(session).persist(
        article_id=article.id,
        generated_output=_generated({"angle": "insight", "link_mode": "none", "body": "本文。"}),
        now=_NOW,
    )[0]
    service = _approval(session, relay, notifier)
    row = service.send(subject_type=SUBJECT_THREADS_POST, subject_id=proposal.id, now=_NOW)
    relay.decisions = [
        RelayDecision.from_payload(
            {
                "relay_session_id": row.relay_session_id,
                "subject_type": SUBJECT_THREADS_POST,
                "subject_id": proposal.id,
                "subject_hash": proposal.proposal_hash,
                "subject_version": 1,
                "decision": "approved",
                "decision_reason": None,
                "decided_at": _NOW.isoformat(),
            }
        )
    ]
    article.body = article.body + "\n人が編集した。\n"
    session.commit()

    outcome = service.sync(execute=True, now=_NOW)

    assert outcome.applied == 0
    assert session.get(ThreadsPostProposal, proposal.id).status == TP_AWAITING_APPROVAL


def test_a_replayed_threads_decision_cannot_approve_twice(
    session, article, relay, notifier
) -> None:
    proposal = _service(session).persist(
        article_id=article.id,
        generated_output=_generated({"angle": "insight", "link_mode": "none", "body": "本文。"}),
        now=_NOW,
    )[0]
    service = _approval(session, relay, notifier)
    row = service.send(subject_type=SUBJECT_THREADS_POST, subject_id=proposal.id, now=_NOW)
    relay.decisions = [
        RelayDecision.from_payload(
            {
                "relay_session_id": row.relay_session_id,
                "subject_type": SUBJECT_THREADS_POST,
                "subject_id": proposal.id,
                "subject_hash": proposal.proposal_hash,
                "subject_version": 1,
                "decision": "approved",
                "decision_reason": None,
                "decided_at": _NOW.isoformat(),
            }
        )
    ]

    service.sync(execute=True, now=_NOW)
    second = service.sync(execute=True, now=_NOW)

    assert second.applied == 0
    assert second.details[0]["reason"] == "already synchronized"


def test_an_already_approved_proposal_cannot_be_sent_again(
    session, article, relay, notifier
) -> None:
    proposal = _service(session).persist(
        article_id=article.id,
        generated_output=_generated({"angle": "insight", "link_mode": "none", "body": "本文。"}),
        now=_NOW,
    )[0]
    proposal.status = TP_APPROVED
    session.commit()

    with pytest.raises(MobileApprovalError, match="awaiting approval"):
        _approval(session, relay, notifier).send(
            subject_type=SUBJECT_THREADS_POST, subject_id=proposal.id, now=_NOW
        )


def test_the_threads_snapshot_escapes_nothing_and_stores_raw_text(session, article) -> None:
    """スナップショットはデータであって HTML ではない (描画側が必ずエスケープする)。"""

    from app.approval.review_snapshot import build_snapshot

    proposal = _service(session).persist(
        article_id=article.id,
        generated_output=_generated(
            {"angle": "insight", "link_mode": "none", "body": "記号 < と > を含む本文。"}
        ),
        now=_NOW,
    )[0]

    snapshot = build_snapshot(subject_type=SUBJECT_THREADS_POST, subject=proposal, article=article)

    assert snapshot["publish_text"] == proposal.content_text
    assert "&lt;" not in snapshot["publish_text"]


def test_the_threads_path_never_touches_the_relay_on_plan(
    session, article, relay, notifier
) -> None:
    proposal = _service(session).persist(
        article_id=article.id,
        generated_output=_generated({"angle": "insight", "link_mode": "none", "body": "本文。"}),
        now=_NOW,
    )[0]

    prepared = _approval(session, relay, notifier).plan(
        subject_type=SUBJECT_THREADS_POST, subject_id=proposal.id
    )

    assert prepared.ok
    assert relay.sessions == {}
    assert notifier.sent == []


def test_a_relay_failure_leaves_the_proposal_untouched(session, article, notifier) -> None:
    class _Broken(_FakeRelay):
        def create_session(self, **kwargs):
            raise RelayError("invalid_payload")

    proposal = _service(session).persist(
        article_id=article.id,
        generated_output=_generated({"angle": "insight", "link_mode": "none", "body": "本文。"}),
        now=_NOW,
    )[0]

    with pytest.raises(MobileApprovalError, match="relay refused"):
        _approval(session, _Broken(), notifier).send(
            subject_type=SUBJECT_THREADS_POST, subject_id=proposal.id, now=_NOW
        )

    assert session.get(ThreadsPostProposal, proposal.id).status == TP_AWAITING_APPROVAL
    assert session.scalars(select(MobileApprovalSession)).all() == []


# -- T6.1: the reviewed text is exactly the proposal text -----------------------
def _proposal_5_like(session, article) -> ThreadsPostProposal:
    """本番の #5 と同じ性質: 日本語の本文、記事リンク (URL に ? と &)、警告あり。"""

    return _service(session).persist(
        article_id=article.id,
        generated_output=_generated(
            {
                "angle": "question",
                "link_mode": "article",
                "body": (
                    "手元の録音ファイル、会議中じゃなくても文字起こしAIに任せられる？\n"
                    "どちらも公式にファイルのアップロード文字起こしを記載している。\n"
                    "実際の音声で試してから決めるのが安全。\n"
                    f"{LINK_PLACEHOLDER}"
                ),
            }
        ),
        now=_NOW,
    )[0]


def test_the_review_snapshot_carries_the_exact_text_with_its_url(
    session, article, relay, notifier
) -> None:
    proposal = _proposal_5_like(session, article)
    assert "/?utm_" in proposal.content_text and "&utm_source=threads" in proposal.content_text
    row = _approval(session, relay, notifier).send(
        subject_type=SUBJECT_THREADS_POST, subject_id=proposal.id, now=_NOW
    )
    snapshot = relay.sessions[row.relay_session_id]["snapshot"]
    assert snapshot["publish_text"] == proposal.content_text  # byte-for-byte, URL included
    assert snapshot["subject_type"] == SUBJECT_THREADS_POST


def test_a_blank_proposal_text_is_never_sent_for_review(session, article, relay, notifier) -> None:
    proposal = _proposal_5_like(session, article)
    proposal.content_text = "   \n "
    session.commit()
    approval = _approval(session, relay, notifier)
    plan = approval.plan(subject_type=SUBJECT_THREADS_POST, subject_id=proposal.id)
    assert "the proposal has no text for the human to review" in plan.blocked_reasons
    with pytest.raises(MobileApprovalError, match="no text for the human to review"):
        approval.send(subject_type=SUBJECT_THREADS_POST, subject_id=proposal.id, now=_NOW)
    assert relay.sessions == {}


def test_a_snapshot_that_would_not_show_the_exact_text_is_never_sent(
    session, article, relay, notifier
) -> None:
    proposal = _proposal_5_like(session, article)
    # 最終防壁の sanitize が書き換える文字列 (/go/ の URL)。人が見る本文が変わってしまう。
    proposal.content_text = "試してから決める。 https://bizfluxlab.com/go/abc123"
    session.commit()
    plan = _approval(session, relay, notifier).plan(
        subject_type=SUBJECT_THREADS_POST, subject_id=proposal.id
    )
    assert "the review snapshot does not carry the exact proposal text" in plan.blocked_reasons


def test_review_text_problems_is_pure_and_subject_aware() -> None:
    from types import SimpleNamespace

    from app.approval.review_snapshot import review_text_problems
    from app.models import SUBJECT_CHANGE_REQUEST

    ok = SimpleNamespace(content_text="本文")
    assert (
        review_text_problems(
            subject_type=SUBJECT_THREADS_POST, subject=ok, snapshot={"publish_text": "本文"}
        )
        == []
    )
    assert review_text_problems(
        subject_type=SUBJECT_THREADS_POST, subject=ok, snapshot={"publish_text": "本文 "}
    ) == ["the review snapshot does not carry the exact proposal text"]
    assert (
        review_text_problems(
            subject_type=SUBJECT_CHANGE_REQUEST, subject=None, snapshot={"inserted_paragraph": "x"}
        )
        == []
    )
    assert review_text_problems(
        subject_type=SUBJECT_CHANGE_REQUEST, subject=None, snapshot={"inserted_paragraph": ""}
    ) == ["the change request has no inserted paragraph for the human to review"]
