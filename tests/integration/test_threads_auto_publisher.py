"""承認済み queue からの自動公開 (T4.3、Meta には一切接続しない)。

pin する契約:

- ポリシー・worker のフラグ・ロック・設定の **どれか 1 つでも** 欠けたら公開しない。
- 1 回の呼び出しで公開を試みるのは最大 1 件。資格のある候補が 5 件あっても 1 件。
- 公開した直後は間隔が始まり、次の呼び出しでは公開しない (取り返しをしない)。
- 公開窓の外・不確定な公開・T3 の判定で止まる。
- 読むだけの事前確認が失敗すれば、**コンテナを作る前に** 止まる
  (2026-09-25 の HTTP 400 / code 200 "API access blocked." のような状態)。
- 応答を取りこぼしたら uncertain で止まり、以後の公開も止まる。
- 公開は T3 の経路で、trigger=automatic として記録される。
- dry run は外にも DB にも触れない。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    PUB_TRIGGER_AUTOMATIC,
    PUB_UNCERTAIN,
    TP_APPROVED,
    Article,
    ThreadsPostProposal,
    ThreadsPublication,
    ThreadsPublicationAttempt,
)
from app.services.threads_auto_publisher import ThreadsAutoPublisher
from app.social.threads.errors import ThreadsPermissionError, ThreadsServerError
from app.social.threads.models import ThreadsContainer
from app.social.threads.models import ThreadsPublication as ThreadsPublicationDTO
from app.social.threads.policy import (
    get_measurement_policy,
    get_operations_policy,
    load_operations_policy,
)

_BASE = "https://bizfluxlab.com"
_NOW = datetime(2026, 9, 25, 2, 0, tzinfo=UTC)  # 11:00 JST
_JST = ZoneInfo("Asia/Tokyo")


class _Settings:
    threads_enabled = True
    threads_user_id = "9876543210"
    threads_access_token = "THAAAsecret-token"
    threads_api_base_url = "https://graph.threads.net"
    threads_api_version = "v1.0"
    wordpress_base_url = _BASE


class _FakeThreads:
    """Threads の代役。書き込み呼び出しを数える。"""

    def __init__(self, *, preflight_error=None, publish_error=None, state="ready") -> None:
        self.calls: list[str] = []
        self.texts: dict[str, str] = {}
        self._preflight_error = preflight_error
        self._publish_error = publish_error
        self._state = state

    def describe(self):
        from app.social.threads.service import ThreadsConnectionStatus

        return ThreadsConnectionStatus(
            enabled=self._state != "disabled",
            configured=self._state == "ready",
            api_version="v1.0",
            user_id_configured=True,
            access_token_configured=self._state == "ready",
        )

    def check_connection(self):
        self.calls.append("preflight")
        status = self.describe()
        if self._preflight_error is not None:
            status.reachable = False
            status.error = self._preflight_error.as_dict()
        else:
            status.reachable = True
        return status

    @property
    def client(self):
        return self

    def create_text_container(self, text):
        self.calls.append("create")
        self._last_text = text
        return ThreadsContainer(f"container-{len(self.calls)}")

    def publish_container(self, creation_id):
        self.calls.append("publish")
        if self._publish_error:
            raise self._publish_error
        media = f"media-{len(self.calls)}"
        self.texts[media] = self._last_text
        return ThreadsPublicationDTO(media)

    def fetch_publication(self, media_id):
        self.calls.append("readback")
        return {"id": media_id, "text": self.texts.get(media_id, ""), "permalink": "https://x"}

    @property
    def writes(self) -> int:
        return sum(1 for c in self.calls if c in ("create", "publish"))


@pytest.fixture
def enabled_policy(tmp_path):
    document = dict(get_operations_policy().raw)
    document["automatic_publication"] = {"enabled": True, "preflight_read": True}
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return load_operations_policy(path)


@pytest.fixture
def articles(session: Session) -> list[Article]:
    rows = []
    for article_id in range(21, 27):
        row = Article(
            id=article_id,
            title=f"記事{article_id}",
            slug=f"a{article_id}",
            body=f"本文{article_id}。",
            status="published",
            published_url=f"{_BASE}/a{article_id}/",
            published_at=_NOW - timedelta(days=10),
            article_type="informational",
            monetization_mode="supporting",
            wordpress_post_id=str(1000 + article_id),
        )
        session.add(row)
        rows.append(row)
    session.commit()
    return rows


def _approved(session: Session, article: Article, seed: str, hours_ago: float = 1.0):
    from app.article.draft_promotion_canonical import compute_text_hash

    text = f"{article.title} {seed} の投稿。"
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
        approved_at=_NOW - timedelta(hours=hours_ago),
    )
    session.add(row)
    session.commit()
    return row


def _publisher(session, fake, policy, *, flag=True, lock=True) -> ThreadsAutoPublisher:
    return ThreadsAutoPublisher(
        session,
        settings=_Settings(),
        threads_service=fake,
        policy=policy,
        measurement_policy=get_measurement_policy(),
        timezone=_JST,
        flag_enabled=flag,
        lock_held=lock,
        sleep=lambda _s: None,
    )


def _count(session: Session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


# == gates =====================================================================
def test_the_committed_policy_keeps_automatic_publication_off() -> None:
    assert get_operations_policy().automatic_publication_enabled is False


def test_the_committed_policy_publishes_nothing_even_with_the_flag(
    session: Session, articles
) -> None:
    _approved(session, articles[0], "a")
    fake = _FakeThreads()
    outcome = _publisher(session, fake, get_operations_policy()).publish_one(now=_NOW)
    assert outcome.outcome == "gated"
    assert "automatic_publication.enabled is false in the policy" in outcome.blocked_reasons
    assert fake.calls == []
    assert _count(session, ThreadsPublication) == 0


@pytest.mark.parametrize(
    ("flag", "lock", "state", "reason"),
    [
        (False, True, "ready", "the worker was not started with --auto-publish"),
        (True, False, "ready", "the worker does not hold the worker lock"),
        (True, True, "misconfigured", "Threads is not in the ready state"),
    ],
)
def test_every_gate_is_required(
    session: Session, articles, enabled_policy, flag, lock, state, reason
) -> None:
    _approved(session, articles[0], "a")
    fake = _FakeThreads(state=state)
    outcome = _publisher(session, fake, enabled_policy, flag=flag, lock=lock).publish_one(now=_NOW)
    assert outcome.outcome == "gated"
    assert reason in outcome.blocked_reasons
    assert fake.writes == 0


# == one post =================================================================
def test_all_gates_open_publishes_exactly_one_through_t3(
    session: Session, articles, enabled_policy
) -> None:
    for i, article in enumerate(articles[:5]):
        _approved(session, article, f"p{i}", hours_ago=5 - i)
    fake = _FakeThreads()
    outcome = _publisher(session, fake, enabled_policy).publish_one(now=_NOW)

    assert outcome.published is True
    assert outcome.threads_writes == 2
    assert fake.calls == ["preflight", "create", "publish", "readback"]
    rows = session.scalars(select(ThreadsPublication)).all()
    assert len(rows) == 1
    assert rows[0].trigger == PUB_TRIGGER_AUTOMATIC
    assert rows[0].gap_override_reason is None


def test_the_next_call_waits_for_the_gap_and_never_catches_up(
    session: Session, articles, enabled_policy
) -> None:
    for i, article in enumerate(articles[:5]):
        _approved(session, article, f"p{i}", hours_ago=5 - i)
    fake = _FakeThreads()
    publisher = _publisher(session, fake, enabled_policy)
    first = publisher.publish_one(now=_NOW)
    second = publisher.publish_one(now=_NOW + timedelta(minutes=5))

    assert first.published and not second.attempted
    assert "gap_not_elapsed" in second.blocked_reasons
    # 起点は公開 API が成功を返した時刻 (代役の読み戻しには投稿時刻が無いため)。
    gap_end = second.next_evaluation_at - (_NOW + timedelta(minutes=120))
    assert timedelta(0) <= gap_end < timedelta(seconds=1)
    assert _count(session, ThreadsPublication) == 1

    later = publisher.publish_one(now=_NOW + timedelta(minutes=121))
    assert later.published
    assert _count(session, ThreadsPublication) == 2


def test_nothing_is_published_outside_the_window(
    session: Session, articles, enabled_policy
) -> None:
    _approved(session, articles[0], "a")
    fake = _FakeThreads()
    night = datetime(2026, 9, 25, 15, 0, tzinfo=UTC)  # 00:00 JST
    outcome = _publisher(session, fake, enabled_policy).publish_one(now=night)
    assert "outside_publication_window" in outcome.blocked_reasons
    assert fake.writes == 0


def test_an_uncertain_publication_stops_automatic_publication(
    session: Session, articles, enabled_policy
) -> None:
    stuck = _approved(session, articles[0], "s")
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
    _approved(session, articles[1], "b")
    fake = _FakeThreads()
    outcome = _publisher(session, fake, enabled_policy).publish_one(now=_NOW)
    assert "uncertain_publication" in outcome.blocked_reasons
    assert fake.writes == 0


# == preflight =================================================================
def test_a_blocked_api_stops_before_any_container_is_created(
    session: Session, articles, enabled_policy
) -> None:
    """本番で起きた "API access blocked." (HTTP 400 / code 200) の状態を再現する。"""

    _approved(session, articles[0], "a")
    fake = _FakeThreads(
        preflight_error=ThreadsPermissionError(
            "/38467038166276847 failed: API access blocked.", status=400, api_code="200"
        )
    )
    outcome = _publisher(session, fake, enabled_policy).publish_one(now=_NOW)
    assert outcome.outcome == "preflight_failed"
    assert "read-only preflight failed (threads_permission)" in outcome.blocked_reasons
    assert fake.calls == ["preflight"]
    assert _count(session, ThreadsPublication) == 0


def test_a_lost_publish_response_becomes_uncertain_and_stops(
    session: Session, articles, enabled_policy
) -> None:
    _approved(session, articles[0], "a")
    _approved(session, articles[1], "b")
    fake = _FakeThreads(publish_error=ThreadsServerError("publish timed out", status=500))
    publisher = _publisher(session, fake, enabled_policy)
    first = publisher.publish_one(now=_NOW)
    assert first.outcome == "uncertain"
    assert first.attempted is True

    fake._publish_error = None
    again = publisher.publish_one(now=_NOW + timedelta(hours=5))
    assert again.attempted is False
    assert "uncertain_publication" in again.blocked_reasons
    assert fake.writes == 2  # create + (lost) publish のみ。再送していない。


# == dry run ===================================================================
def test_the_dry_run_touches_nothing(session: Session, articles) -> None:
    proposal = _approved(session, articles[0], "a")
    fake = _FakeThreads()
    before = (_count(session, ThreadsPublication), _count(session, ThreadsPublicationAttempt))

    dry = _publisher(session, fake, get_operations_policy()).dry_run(now=_NOW)

    assert fake.calls == []
    assert (_count(session, ThreadsPublication), _count(session, ThreadsPublicationAttempt)) == (
        before
    )
    assert dry.proposal_id == proposal.id
    assert dry.gates["policy_enabled"] is False
    assert any("policy" in r for r in dry.blocked_reasons)


def test_the_dry_run_names_the_candidate_when_everything_else_is_ready(
    session: Session, articles, enabled_policy
) -> None:
    proposal = _approved(session, articles[0], "a")
    dry = _publisher(session, _FakeThreads(), enabled_policy).dry_run(now=_NOW)
    assert dry.blocked_reasons == []
    assert any(f"would publish proposal {proposal.id}" in n for n in dry.notes)
