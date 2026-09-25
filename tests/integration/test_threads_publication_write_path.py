"""T3 の書き込み経路に入れた安全の条件 (T4.3、Meta には一切接続しない)。

pin する契約:

- 前回の **実際の** 公開から 120 分空いていなければ、手動でも公開しない。
- 人が理由を付けて明示したときだけ、間隔 **だけ** を上書きできる。理由は公開の行に残る。
- 自動の公開は間隔を上書きできない。
- 他の公開が不確定・照合待ちなら、どの提案も公開しない (上書きもできない)。
- 誰が公開を始めたか (manual / automatic) が公開の行に残る。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models import (
    PUB_PUBLISHED,
    PUB_UNCERTAIN,
    TP_APPROVED,
    Article,
    ThreadsPostProposal,
    ThreadsPublication,
)
from app.services.threads_publication_service import (
    ManualGapOverride,
    ThreadsPublicationError,
    ThreadsPublicationService,
)
from app.social.threads.models import ThreadsContainer
from app.social.threads.models import ThreadsPublication as ThreadsPublicationDTO

_BASE = "https://bizfluxlab.com"
_NOW = datetime(2026, 9, 25, 2, 0, tzinfo=UTC)  # 11:00 JST


class _Settings:
    threads_enabled = True
    threads_user_id = "9876543210"
    threads_access_token = "THAAAsecret"
    threads_api_base_url = "https://graph.threads.net"
    threads_api_version = "v1.0"
    wordpress_base_url = _BASE


class _FakeThreads:
    def __init__(self, text: str) -> None:
        self.calls: list[str] = []
        self._text = text

    def describe(self):
        from app.social.threads.service import ThreadsConnectionStatus

        return ThreadsConnectionStatus(
            enabled=True,
            configured=True,
            api_version="v1.0",
            user_id_configured=True,
            access_token_configured=True,
        )

    @property
    def client(self):
        return self

    def create_text_container(self, text):
        self.calls.append("create")
        return ThreadsContainer("container-9")

    def publish_container(self, creation_id):
        self.calls.append("publish")
        return ThreadsPublicationDTO("media-9")

    def fetch_publication(self, media_id):
        self.calls.append("readback")
        return {"id": media_id, "text": self._text, "permalink": "https://www.threads.net/p/x"}


@pytest.fixture
def article(session: Session) -> Article:
    row = Article(
        id=21,
        title="記事",
        slug="a",
        body="本文。",
        status="published",
        published_url=f"{_BASE}/a/",
        published_at=_NOW - timedelta(days=10),
        article_type="informational",
        monetization_mode="supporting",
        wordpress_post_id="84",
    )
    session.add(row)
    session.commit()
    return row


def _proposal(session: Session, article: Article, seed: str) -> ThreadsPostProposal:
    from app.article.draft_promotion_canonical import compute_text_hash

    text = f"投稿 {seed}。"
    row = ThreadsPostProposal(
        source_article_id=article.id,
        source_article_body_hash=compute_text_hash(article.body),
        angle="insight",
        link_mode="none",
        content_text=text,
        character_count=len(text),
        content_seed=(seed * 64)[:64],
        proposal_hash=(seed.upper() * 64)[:64],
        policy_version="t2.1",
        generator_version="g",
        status=TP_APPROVED,
        approved_at=_NOW - timedelta(hours=1),
    )
    session.add(row)
    session.commit()
    return row


def _published(session, proposal, *, minutes_ago: int, status=PUB_PUBLISHED) -> ThreadsPublication:
    row = ThreadsPublication(
        proposal_id=proposal.id,
        proposal_hash=proposal.proposal_hash,
        source_article_id=proposal.source_article_id,
        angle=proposal.angle,
        exact_published_text=proposal.content_text,
        threads_media_id=f"m-{proposal.id}" if status == PUB_PUBLISHED else None,
        status=status,
        published_at=_NOW - timedelta(minutes=minutes_ago) if status == PUB_PUBLISHED else None,
    )
    session.add(row)
    session.commit()
    return row


def _service(session: Session, fake: _FakeThreads) -> ThreadsPublicationService:
    return ThreadsPublicationService(
        session, settings=_Settings(), threads_service=fake, sleep=lambda _s: None
    )


# == gap ======================================================================
def test_a_manual_publish_inside_the_gap_is_blocked(session: Session, article: Article) -> None:
    _published(session, _proposal(session, article, "a"), minutes_ago=40)
    target = _proposal(session, article, "b")
    fake = _FakeThreads(target.content_text)

    plan = _service(session, fake).plan(proposal_id=target.id, now=_NOW)
    assert not plan.ok
    assert any("120-minute gap" in r for r in plan.blocked_reasons)

    outcome = _service(session, fake).publish(proposal_id=target.id, execute=True, now=_NOW)
    assert outcome.outcome == "blocked"
    assert fake.calls == []


def test_the_gap_is_measured_from_the_real_publication(session: Session, article: Article) -> None:
    _published(session, _proposal(session, article, "a"), minutes_ago=120)
    target = _proposal(session, article, "b")
    plan = _service(session, _FakeThreads(target.content_text)).plan(
        proposal_id=target.id, now=_NOW
    )
    assert plan.ok
    assert plan.gap["elapsed"] is True


def test_a_human_override_with_a_reason_is_allowed_and_recorded(
    session: Session, article: Article
) -> None:
    _published(session, _proposal(session, article, "a"), minutes_ago=40)
    target = _proposal(session, article, "b")
    fake = _FakeThreads(target.content_text)
    override = ManualGapOverride(reason="breaking news follow-up")

    outcome = _service(session, fake).publish(
        proposal_id=target.id, execute=True, now=_NOW, gap_override=override
    )
    assert outcome.outcome == "published"
    row = session.get(ThreadsPublication, outcome.publication_id)
    assert row.trigger == "manual"
    assert row.gap_override_reason == "breaking news follow-up"
    assert row.gap_override_at.replace(tzinfo=UTC) == _NOW


def test_an_override_without_a_reason_is_refused() -> None:
    for reason in ("", "   ", None):
        with pytest.raises(ValueError):
            ManualGapOverride(reason=reason)
    with pytest.raises(ValueError):
        ManualGapOverride(reason="x", source="resident-worker")


def test_an_override_outside_the_gap_is_not_recorded(session: Session, article: Article) -> None:
    target = _proposal(session, article, "b")
    outcome = _service(session, _FakeThreads(target.content_text)).publish(
        proposal_id=target.id,
        execute=True,
        now=_NOW,
        gap_override=ManualGapOverride(reason="not needed"),
    )
    row = session.get(ThreadsPublication, outcome.publication_id)
    assert row.gap_override_reason is None


def test_automatic_publication_can_never_override_the_gap(
    session: Session, article: Article
) -> None:
    target = _proposal(session, article, "b")
    with pytest.raises(ThreadsPublicationError, match="never override"):
        _service(session, _FakeThreads(target.content_text)).plan(
            proposal_id=target.id,
            now=_NOW,
            trigger="automatic",
            gap_override=ManualGapOverride(reason="x"),
        )


def test_the_trigger_is_recorded(session: Session, article: Article) -> None:
    target = _proposal(session, article, "b")
    outcome = _service(session, _FakeThreads(target.content_text)).publish(
        proposal_id=target.id, execute=True, now=_NOW, trigger="automatic"
    )
    assert session.get(ThreadsPublication, outcome.publication_id).trigger == "automatic"


# == other publications in flight =============================================
def test_an_uncertain_publication_blocks_every_other_proposal(
    session: Session, article: Article
) -> None:
    _published(session, _proposal(session, article, "a"), minutes_ago=0, status=PUB_UNCERTAIN)
    target = _proposal(session, article, "b")
    fake = _FakeThreads(target.content_text)

    plan = _service(session, fake).plan(
        proposal_id=target.id, now=_NOW, gap_override=ManualGapOverride(reason="x")
    )
    assert not plan.ok
    assert any("reconcile it before publishing anything else" in r for r in plan.blocked_reasons)
    outcome = _service(session, fake).publish(proposal_id=target.id, execute=True, now=_NOW)
    assert outcome.outcome == "blocked"
    assert fake.calls == []


def test_a_reconciliation_flag_blocks_every_other_proposal(
    session: Session, article: Article
) -> None:
    earlier = _published(session, _proposal(session, article, "a"), minutes_ago=300)
    earlier.reconciliation_required = True
    session.commit()
    target = _proposal(session, article, "b")
    plan = _service(session, _FakeThreads(target.content_text)).plan(
        proposal_id=target.id, now=_NOW
    )
    assert any("requires reconciliation" in r for r in plan.blocked_reasons)
