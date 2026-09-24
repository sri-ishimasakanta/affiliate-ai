"""人の queue 操作と時刻の制約 (T4.2、DB 連携)。

pin する契約:

- hold は選ばれなくする。release で元に戻る。
- prefer_next は並び順の先頭に来るだけ。**安全の条件を 1 つも飛ばさない。**
- not_before より前は資格が無い。expires_at を過ぎたら資格が無い (消さない)。
- 常緑 (期限なし) の提案は、どれだけ古くても期限切れにならない。
- どの操作も誰が・いつ・なぜを履歴に残す。本文には触れない。
- 承認されただけでは公開されない (queue に入るだけ)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    PUB_PUBLISHED,
    PUB_UNCERTAIN,
    TP_APPROVED,
    TP_REJECTED,
    Article,
    ThreadsPostProposal,
    ThreadsPublication,
    ThreadsQueueControlEvent,
)
from app.services.threads_queue_control_service import (
    ThreadsQueueControlError,
    ThreadsQueueControlService,
)
from app.services.threads_queue_service import ThreadsQueueService
from app.social.threads.service import ThreadsService

_BASE = "https://bizfluxlab.com"
# 2026-09-25 10:00 JST。公開窓の中、直前の公開なし。
_NOW = datetime(2026, 9, 25, 1, 0, tzinfo=UTC)


class _Settings:
    threads_enabled = True
    threads_user_id = "9876543210"
    threads_access_token = "THAAAsecret"
    threads_api_base_url = "https://graph.threads.net"
    threads_api_version = "v1.0"
    wordpress_base_url = _BASE


class _NoHttp:
    def __getattr__(self, name):
        raise AssertionError(f"queue evaluation must not call Threads ({name})")


@pytest.fixture
def articles(session: Session) -> list[Article]:
    rows = []
    for article_id in (21, 22, 23):
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


def _approved(session, article, *, seed, angle="insight", approved_hours_ago=1.0, **kw):
    from app.article.draft_promotion_canonical import compute_text_hash

    text = kw.pop("text", f"{article.title} {seed} の投稿案。")
    row = ThreadsPostProposal(
        source_article_id=article.id,
        source_article_body_hash=compute_text_hash(article.body),
        angle=angle,
        link_mode="none",
        content_text=text,
        character_count=kw.pop("character_count", len(text)),
        destination_url=None,
        content_seed=(seed * 64)[:64],
        proposal_hash=(seed.upper() * 64)[:64],
        policy_version="t2.1",
        generator_version="threads-proposal-1",
        status=kw.pop("status", TP_APPROVED),
        approved_at=_NOW - timedelta(hours=approved_hours_ago),
        **kw,
    )
    session.add(row)
    session.commit()
    return row


def _queue(session: Session) -> ThreadsQueueService:
    settings = _Settings()
    return ThreadsQueueService(
        session, settings=settings, threads_service=ThreadsService(settings, client=_NoHttp())
    )


def _evaluate(session: Session, now: datetime = _NOW):
    return _queue(session).evaluate(now=now)


def _reasons(evaluation) -> dict[int, str]:
    return {c.proposal_id: c.reason for c in evaluation.candidates}


# == hold / release ===========================================================
def test_hold_blocks_and_release_restores(session: Session, articles) -> None:
    proposal = _approved(session, articles[0], seed="a")
    control = ThreadsQueueControlService(session)

    control.hold(proposal.id, reason="wait for the event date", now=_NOW)
    assert _reasons(_evaluate(session))[proposal.id] == "held"
    assert _evaluate(session).next_candidate is None

    control.release(proposal.id, reason="event confirmed", now=_NOW)
    evaluation = _evaluate(session)
    assert _reasons(evaluation)[proposal.id] == "eligible"
    assert evaluation.next_candidate.proposal_id == proposal.id


def test_a_hold_needs_a_reason_and_is_audited(session: Session, articles) -> None:
    proposal = _approved(session, articles[0], seed="a")
    control = ThreadsQueueControlService(session)
    with pytest.raises(ThreadsQueueControlError):
        control.hold(proposal.id, reason="  ")

    control.hold(proposal.id, reason="check the numbers first", now=_NOW)
    control.release(proposal.id, now=_NOW + timedelta(hours=1))
    history = control.history(proposal.id)
    assert [e.action for e in history] == ["hold", "release"]
    assert history[0].actor == "human-cli"
    assert history[0].reason == "check the numbers first"
    assert history[1].detail_json["hold_reason"] == "check the numbers first"


def test_controls_never_touch_the_text(session: Session, articles) -> None:
    proposal = _approved(session, articles[0], seed="a")
    before = (proposal.content_text, proposal.proposal_hash, proposal.status)
    control = ThreadsQueueControlService(session)
    control.hold(proposal.id, reason="x", now=_NOW)
    control.release(proposal.id, now=_NOW)
    control.prefer_next(proposal.id, now=_NOW)
    session.refresh(proposal)
    assert (proposal.content_text, proposal.proposal_hash, proposal.status) == before


# == prefer_next ==============================================================
def test_prefer_next_moves_a_proposal_to_the_front(session: Session, articles) -> None:
    older = _approved(session, articles[0], seed="a", approved_hours_ago=5)
    newer = _approved(session, articles[1], seed="b", approved_hours_ago=1)
    assert _evaluate(session).next_candidate.proposal_id == older.id

    ThreadsQueueControlService(session).prefer_next(newer.id, reason="timely", now=_NOW)
    assert _evaluate(session).next_candidate.proposal_id == newer.id


@pytest.mark.parametrize(
    "blocker",
    ["rejected", "stale", "expired", "not_before", "content_integrity", "already_published"],
)
def test_prefer_next_cannot_bypass_a_candidate_blocker(
    session: Session, articles, blocker: str
) -> None:
    preferred = _approved(session, articles[0], seed="p", approved_hours_ago=1)
    other = _approved(session, articles[1], seed="o", approved_hours_ago=5)
    ThreadsQueueControlService(session).prefer_next(preferred.id, now=_NOW)

    if blocker == "rejected":
        preferred.status = TP_REJECTED
    elif blocker == "stale":
        articles[0].body = "書き換わった本文。"
    elif blocker == "expired":
        preferred.expires_at = _NOW - timedelta(minutes=1)
    elif blocker == "not_before":
        preferred.not_before = _NOW + timedelta(hours=3)
    elif blocker == "content_integrity":
        preferred.character_count = preferred.character_count + 5
    elif blocker == "already_published":
        session.add(
            ThreadsPublication(
                proposal_id=preferred.id,
                proposal_hash=preferred.proposal_hash,
                source_article_id=preferred.source_article_id,
                angle=preferred.angle,
                exact_published_text=preferred.content_text,
                threads_media_id="m-preferred",
                status=PUB_PUBLISHED,
                published_at=_NOW - timedelta(hours=5),
            )
        )
    session.commit()

    evaluation = _evaluate(session)
    # 優先した提案は選ばれない。通常の候補が評価される。
    assert evaluation.next_candidate is not None
    assert evaluation.next_candidate.proposal_id == other.id
    if blocker != "rejected":
        assert any(f"preferred proposal {preferred.id} is blocked" in n for n in evaluation.notes)


def test_prefer_next_cannot_bypass_an_uncertain_publication(session: Session, articles) -> None:
    preferred = _approved(session, articles[0], seed="p")
    stuck = _approved(session, articles[1], seed="s")
    ThreadsQueueControlService(session).prefer_next(preferred.id, now=_NOW)
    session.add(
        ThreadsPublication(
            proposal_id=stuck.id,
            proposal_hash=stuck.proposal_hash,
            source_article_id=stuck.source_article_id,
            angle=stuck.angle,
            exact_published_text=stuck.content_text,
            status=PUB_UNCERTAIN,
        )
    )
    session.commit()

    evaluation = _evaluate(session)
    assert "uncertain_publication" in evaluation.blockers
    assert evaluation.would_publish_now is False


def test_prefer_next_cannot_bypass_the_window_or_the_gap(session: Session, articles) -> None:
    preferred = _approved(session, articles[0], seed="p")
    ThreadsQueueControlService(session).prefer_next(preferred.id, now=_NOW)

    night = datetime(2026, 9, 25, 16, 0, tzinfo=UTC)  # 01:00 JST
    assert "outside_publication_window" in _evaluate(session, night).blockers

    last = _approved(session, articles[1], seed="l")
    session.add(
        ThreadsPublication(
            proposal_id=last.id,
            proposal_hash=last.proposal_hash,
            source_article_id=last.source_article_id,
            angle=last.angle,
            exact_published_text=last.content_text,
            threads_media_id="m-last",
            status=PUB_PUBLISHED,
            published_at=_NOW - timedelta(minutes=30),
        )
    )
    session.commit()
    assert "gap_not_elapsed" in _evaluate(session).blockers


def test_a_published_proposal_cannot_be_controlled(session: Session, articles) -> None:
    proposal = _approved(session, articles[0], seed="a")
    session.add(
        ThreadsPublication(
            proposal_id=proposal.id,
            proposal_hash=proposal.proposal_hash,
            source_article_id=proposal.source_article_id,
            angle=proposal.angle,
            exact_published_text=proposal.content_text,
            threads_media_id="m1",
            status=PUB_PUBLISHED,
            published_at=_NOW - timedelta(hours=5),
        )
    )
    session.commit()
    with pytest.raises(ThreadsQueueControlError, match="publication"):
        ThreadsQueueControlService(session).prefer_next(proposal.id)


# == not_before / expires_at ==================================================
def test_not_before_blocks_early_and_wakes_exactly_on_time(session: Session, articles) -> None:
    """not_before が近ければその時刻ちょうどに、遠くても決してそれより後には見直さない。"""

    proposal = _approved(session, articles[0], seed="a")
    control = ThreadsQueueControlService(session)

    soon = _NOW + timedelta(minutes=17)
    control.set_timing(proposal.id, not_before=soon, expires_at=None, now=_NOW)
    early = _evaluate(session)
    assert _reasons(early)[proposal.id] == "not_before"
    assert early.next_evaluation_at == soon

    far = _NOW + timedelta(hours=2, minutes=13)
    control.set_timing(proposal.id, not_before=far, expires_at=None, now=_NOW)
    assert _evaluate(session).next_evaluation_at <= far

    assert _reasons(_evaluate(session, far))[proposal.id] == "eligible"


def test_expires_at_blocks_late_without_deleting(session: Session, articles) -> None:
    proposal = _approved(session, articles[0], seed="a")
    ThreadsQueueControlService(session).set_timing(
        proposal.id, not_before=None, expires_at=_NOW + timedelta(hours=3), now=_NOW
    )
    assert _reasons(_evaluate(session))[proposal.id] == "eligible"

    late = _evaluate(session, _NOW + timedelta(hours=4))
    assert _reasons(late)[proposal.id] == "expired"
    assert session.get(ThreadsPostProposal, proposal.id) is not None


def test_an_evergreen_proposal_never_expires_just_by_age(session: Session, articles) -> None:
    proposal = _approved(session, articles[0], seed="a", approved_hours_ago=24 * 90)
    evaluation = _evaluate(session, _NOW)
    verdict = next(c for c in evaluation.candidates if c.proposal_id == proposal.id)
    assert verdict.reason == "eligible"
    assert verdict.expires_at is None
    # 古い常緑の提案には、弱い信号の後回しも効かない (starvation guard)。
    assert verdict.starvation_guard_active is True


def test_timing_is_validated(session: Session, articles) -> None:
    proposal = _approved(session, articles[0], seed="a")
    control = ThreadsQueueControlService(session)
    with pytest.raises(ThreadsQueueControlError, match="earlier"):
        control.set_timing(
            proposal.id,
            not_before=_NOW + timedelta(hours=5),
            expires_at=_NOW + timedelta(hours=2),
            now=_NOW,
        )
    with pytest.raises(ThreadsQueueControlError, match="past"):
        control.set_timing(
            proposal.id, not_before=None, expires_at=_NOW - timedelta(hours=1), now=_NOW
        )
    with pytest.raises(ThreadsQueueControlError, match="timezone"):
        control.set_timing(
            proposal.id, not_before=datetime(2026, 10, 1, 9, 0), expires_at=None, now=_NOW
        )


def test_timing_changes_are_audited_in_utc(session: Session, articles) -> None:
    from zoneinfo import ZoneInfo

    proposal = _approved(session, articles[0], seed="a")
    jst = datetime(2026, 9, 26, 9, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    ThreadsQueueControlService(session).set_timing(
        proposal.id, not_before=jst, expires_at=None, reason="launch day", now=_NOW
    )
    session.refresh(proposal)
    assert proposal.not_before.replace(tzinfo=UTC) == datetime(2026, 9, 26, 0, 0, tzinfo=UTC)
    event = session.scalars(select(ThreadsQueueControlEvent)).one()
    assert event.action == "set_timing"
    assert event.detail_json["after"]["not_before"] == "2026-09-26T00:00:00+00:00"


# == approval != publication ==================================================
def test_approval_only_makes_a_proposal_queue_eligible(session: Session, articles) -> None:
    proposal = _approved(session, articles[0], seed="a")
    evaluation = _evaluate(session)
    assert evaluation.next_candidate.proposal_id == proposal.id
    assert evaluation.would_publish_now is False
    assert session.scalar(select(func.count()).select_from(ThreadsPublication)) == 0


def test_authoritative_approved_at_orders_the_queue(session: Session, articles) -> None:
    """``updated_at`` がどう動いても、承認の順序は ``approved_at`` で決まる。"""

    first = _approved(session, articles[0], seed="a", approved_hours_ago=5)
    second = _approved(session, articles[1], seed="b", approved_hours_ago=1)
    first.status_reason = "an unrelated edit bumps updated_at"
    session.commit()
    assert _evaluate(session).next_candidate.proposal_id == first.id
    assert second.id != first.id
