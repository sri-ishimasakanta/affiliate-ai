"""120 分の間隔の起点 = **実際の** 公開時刻 (T4.3、Meta には接続しない)。

2026-09-25 の公開 #2 の時系列をそのまま使う:

- サイクル開始      09:39:09 JST (published_at に記録されていた値)
- コンテナ作成      09:39:11 JST
- 公開 API が返った 09:39:44.97 JST
- Threads の投稿時刻 09:39:45 JST (remote_timestamp)
- 読み戻し完了      09:39:48 JST

pin する契約:

- 起点は Threads の投稿時刻。無ければ公開 API が成功を返した時刻。
- サイクル開始・候補の選択・評価・読み戻しの完了 の時刻は起点にしない。
- 11:39:44 は不可、11:39:45 から可。
- 再起動しても同じ起点・同じ時刻になる (保存済みの記録だけから求める)。
- 手動の公開も worker も同じ計算を使う。
- 人の --override-gap は間隔だけを飛ばす。worker は上書きできない。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from app.models import (
    PUB_PUBLISHED,
    TP_APPROVED,
    Article,
    ThreadsPostProposal,
    ThreadsPublication,
    ThreadsPublicationAttempt,
)
from app.services.threads_publication_service import (
    GAP_BASIS_LEGACY,
    GAP_BASIS_PUBLISH_RETURNED,
    GAP_BASIS_RECONCILED,
    GAP_BASIS_REMOTE,
    ManualGapOverride,
    ThreadsPublicationError,
    ThreadsPublicationService,
    parse_remote_timestamp,
)
from app.services.threads_queue_service import ThreadsQueueService
from app.social.threads.service import ThreadsService

_JST = ZoneInfo("Asia/Tokyo")


def _jst(h, mi, s=0, us=0) -> datetime:
    return datetime(2026, 9, 25, h, mi, s, us, tzinfo=_JST).astimezone(UTC)


CYCLE_START = _jst(9, 39, 9, 885214)
PUBLISH_RETURNED = _jst(9, 39, 44, 967827)
REMOTE_POSTED = _jst(9, 39, 45)
READBACK_DONE = _jst(9, 39, 48, 458709)


class _Settings:
    threads_enabled = True
    threads_user_id = "9876543210"
    threads_access_token = "THAAAsecret"
    threads_api_base_url = "https://graph.threads.net"
    threads_api_version = "v1.0"
    wordpress_base_url = "https://bizfluxlab.com"


class _NoHttp:
    def __getattr__(self, name):
        raise AssertionError(f"the gap calculation must not call Threads ({name})")


@pytest.fixture
def article(session: Session) -> Article:
    row = Article(
        id=21,
        title="記事",
        slug="a",
        body="本文。",
        status="published",
        published_url="https://bizfluxlab.com/a/",
        published_at=CYCLE_START - timedelta(days=10),
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
        angle=f"angle-{seed}",
        link_mode="none",
        content_text=text,
        character_count=len(text),
        content_seed=(seed * 64)[:64],
        proposal_hash=(seed.upper() * 64)[:64],
        policy_version="t2.1",
        generator_version="g",
        status=TP_APPROVED,
        approved_at=CYCLE_START - timedelta(hours=1),
    )
    session.add(row)
    session.commit()
    return row


def _publication_two(
    session: Session,
    article: Article,
    *,
    remote: str | None = "2026-09-25T00:39:45+0000",
    seed: str = "two",
    media: str = "18095684012104774",
) -> ThreadsPublication:
    """公開 #2 と同じ記録を作る (published_at はサイクル開始の値)。"""

    proposal = _proposal(session, article, seed)
    row = ThreadsPublication(
        proposal_id=proposal.id,
        proposal_hash=proposal.proposal_hash,
        source_article_id=proposal.source_article_id,
        angle=proposal.angle,
        exact_published_text=proposal.content_text,
        threads_media_id=media,
        status=PUB_PUBLISHED,
        trigger="automatic",
        publish_started_at=CYCLE_START,
        published_at=CYCLE_START,
        remote_timestamp=remote,
    )
    session.add(row)
    session.commit()
    for step, started, finished in (
        ("create_container", _jst(9, 39, 11), _jst(9, 39, 11, 500000)),
        ("publish_container", _jst(9, 39, 44), PUBLISH_RETURNED),
        ("readback", _jst(9, 39, 48), READBACK_DONE),
    ):
        session.add(
            ThreadsPublicationAttempt(
                threads_publication_id=row.id,
                step=step,
                outcome="succeeded",
                started_at=started,
                finished_at=finished,
            )
        )
    session.commit()
    return row


def _t3(session: Session) -> ThreadsPublicationService:
    settings = _Settings()
    return ThreadsPublicationService(
        session, settings=settings, threads_service=ThreadsService(settings, client=_NoHttp())
    )


def _queue(session: Session) -> ThreadsQueueService:
    settings = _Settings()
    return ThreadsQueueService(
        session, settings=settings, threads_service=ThreadsService(settings, client=_NoHttp())
    )


def _gap_blocked(plan) -> bool:
    return any("gap since publication" in r for r in plan.blocked_reasons)


# == the basis =================================================================
def test_the_basis_is_the_threads_post_time_not_the_cycle_start(
    session: Session, article: Article
) -> None:
    row = _publication_two(session, article)
    basis = _t3(session).gap_basis(row)
    assert basis.source == GAP_BASIS_REMOTE
    assert basis.at == REMOTE_POSTED
    assert basis.at != CYCLE_START


def test_publication_two_is_blocked_until_11_39_45(session: Session, article: Article) -> None:
    _publication_two(session, article)
    target = _proposal(session, article, "next")
    t3 = _t3(session)

    for moment, blocked in (
        (_jst(11, 39, 9, 885214), True),  # 旧実装がここで許していた
        (_jst(11, 39, 44), True),
        (_jst(11, 39, 44, 999999), True),
        (_jst(11, 39, 45), False),
    ):
        plan = t3.plan(proposal_id=target.id, now=moment, trigger="automatic")
        assert _gap_blocked(plan) is blocked, moment
    earliest = t3.plan(proposal_id=target.id, now=_jst(10, 0)).gap["earliest_at"]
    assert earliest == _jst(11, 39, 45).isoformat()


def test_without_a_remote_time_the_publish_return_time_is_used(
    session: Session, article: Article
) -> None:
    row = _publication_two(session, article, remote=None)
    basis = _t3(session).gap_basis(row)
    assert basis.source == GAP_BASIS_PUBLISH_RETURNED
    assert basis.at == PUBLISH_RETURNED


def test_readback_latency_neither_shortens_nor_extends_the_gap(
    session: Session, article: Article
) -> None:
    """読み戻しの完了 (09:39:48) は起点にならない。どちらの経路でも。"""

    with_remote = _t3(session).gap_basis(_publication_two(session, article))
    assert with_remote.at < READBACK_DONE
    without_remote = _t3(session).gap_basis(
        _publication_two(session, article, remote=None, seed="bis", media="m-bis")
    )
    assert without_remote.at < READBACK_DONE
    assert without_remote.source == GAP_BASIS_PUBLISH_RETURNED


def test_an_unreadable_remote_timestamp_is_not_guessed(session: Session, article: Article) -> None:
    row = _publication_two(session, article, remote="yesterday afternoon")
    assert _t3(session).gap_basis(row).source == GAP_BASIS_PUBLISH_RETURNED
    assert parse_remote_timestamp("2026-09-25T00:39:45") is None  # 時刻帯が無い値は使わない


def test_a_reconciled_post_uses_the_confirmation_time(session: Session, article: Article) -> None:
    """応答を取りこぼして照合で確定した公開: 実際より後の時刻 = 間隔は長め (安全側)。"""

    row = _publication_two(session, article, remote=None)
    session.query(ThreadsPublicationAttempt).filter(
        ThreadsPublicationAttempt.step == "publish_container"
    ).update({"outcome": "uncertain"})
    confirmed = _jst(9, 55)
    session.add(
        ThreadsPublicationAttempt(
            threads_publication_id=row.id,
            step="reconcile",
            outcome="succeeded",
            started_at=confirmed - timedelta(seconds=1),
            finished_at=confirmed,
        )
    )
    session.commit()
    basis = _t3(session).gap_basis(row)
    assert basis.source == GAP_BASIS_RECONCILED
    assert basis.at == confirmed


def test_a_legacy_row_keeps_its_recorded_time(session: Session, article: Article) -> None:
    row = _publication_two(session, article, remote=None)
    session.query(ThreadsPublicationAttempt).delete()
    session.commit()
    basis = _t3(session).gap_basis(row)
    assert basis.source == GAP_BASIS_LEGACY
    assert basis.at == CYCLE_START


# == restart / shared logic ====================================================
def test_a_restart_computes_the_same_earliest_time(session: Session, article: Article) -> None:
    _publication_two(session, article)
    target = _proposal(session, article, "next")
    first = _t3(session).plan(proposal_id=target.id, now=_jst(10, 0)).gap["earliest_at"]
    session.expire_all()  # 新しいプロセスと同じく、保存済みの記録だけから求める
    second = _t3(session).plan(proposal_id=target.id, now=_jst(10, 30)).gap["earliest_at"]
    assert first == second == _jst(11, 39, 45).isoformat()


def test_manual_and_worker_paths_use_the_same_gap(session: Session, article: Article) -> None:
    _publication_two(session, article)
    target = _proposal(session, article, "next")
    manual = _t3(session).plan(proposal_id=target.id, now=_jst(10, 0), trigger="manual")
    automatic = _t3(session).plan(proposal_id=target.id, now=_jst(10, 0), trigger="automatic")
    queue = _queue(session).evaluate(now=_jst(10, 0), publication_enabled=True)

    assert manual.gap["earliest_at"] == automatic.gap["earliest_at"]
    assert queue.timing.gap_elapsed_at == _jst(11, 39, 45)
    assert queue.next_evaluation_at == _jst(11, 39, 45)
    assert "gap_not_elapsed" in queue.blockers


def test_the_queue_is_blocked_at_11_39_44_and_open_at_11_39_45(
    session: Session, article: Article
) -> None:
    _publication_two(session, article)
    _proposal(session, article, "next")
    queue = _queue(session)
    before = queue.evaluate(now=_jst(11, 39, 44), publication_enabled=True)
    assert "gap_not_elapsed" in before.blockers
    assert queue.evaluate(now=_jst(11, 39, 45), publication_enabled=True).would_publish_now


# == overrides ===================================================================
def test_a_human_override_still_bypasses_only_the_gap(session: Session, article: Article) -> None:
    _publication_two(session, article)
    target = _proposal(session, article, "next")
    plan = _t3(session).plan(
        proposal_id=target.id,
        now=_jst(10, 0),
        gap_override=ManualGapOverride(reason="breaking follow-up"),
    )
    assert plan.ok
    assert plan.gap["overridden"] is True
    assert plan.gap_overridden_reason == "breaking follow-up"


def test_the_worker_still_cannot_override_the_gap(session: Session, article: Article) -> None:
    _publication_two(session, article)
    target = _proposal(session, article, "next")
    with pytest.raises(ThreadsPublicationError, match="never override"):
        _t3(session).plan(
            proposal_id=target.id,
            now=_jst(10, 0),
            trigger="automatic",
            gap_override=ManualGapOverride(reason="x"),
        )


# == through the real publish path =============================================
class _TimelineThreads:
    """公開 #2 と同じく、Threads が 09:39:45 の投稿時刻を返す代役。"""

    def __init__(self, *, remote: str | None) -> None:
        self._remote = remote
        self._text = ""

    def describe(self):
        return ThreadsService(_Settings(), client=_NoHttp()).describe()

    @property
    def client(self):
        return self

    def create_text_container(self, text):
        from app.social.threads.models import ThreadsContainer

        self._text = text
        return ThreadsContainer("c-timeline")

    def publish_container(self, creation_id):
        from app.social.threads.models import ThreadsPublication as Dto

        return Dto("m-timeline")

    def fetch_publication(self, media_id):
        media = {"id": media_id, "text": self._text, "permalink": "https://x"}
        if self._remote:
            media["timestamp"] = self._remote
        return media


@pytest.mark.parametrize("remote", ["2026-09-25T00:39:45+0000", None])
def test_a_new_publication_persists_a_reproducible_basis(
    session: Session, article: Article, remote: str | None
) -> None:
    proposal = _proposal(session, article, "live")
    service = ThreadsPublicationService(
        session,
        settings=_Settings(),
        threads_service=_TimelineThreads(remote=remote),
        sleep=lambda _s: None,
    )
    outcome = service.publish(proposal_id=proposal.id, execute=True, now=CYCLE_START)
    assert outcome.outcome == "published"

    session.expire_all()  # 再起動と同じく、保存済みの記録だけから求める
    row = session.get(ThreadsPublication, outcome.publication_id)
    basis = _t3(session).gap_basis(row)
    if remote:
        assert basis.source == GAP_BASIS_REMOTE
        assert basis.at == REMOTE_POSTED
    else:
        # 公開 API が返った時刻。操作の時計で刻むので、サイクル開始より後・秒未満の差。
        assert basis.source == GAP_BASIS_PUBLISH_RETURNED
        assert CYCLE_START <= basis.at < CYCLE_START + timedelta(seconds=1)
    # サイクル開始そのものは (Threads の投稿時刻があるときは) 起点にならない。
    assert row.published_at.replace(tzinfo=UTC) == CYCLE_START
