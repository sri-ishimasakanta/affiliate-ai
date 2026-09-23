"""Threads 公開ワークフロー (T3、Meta には一切接続しない)。

pin する契約:

- **承認は公開ではない。** 公開は人が明示的に実行したときだけ起きる。
- PLAN は Meta へ 1 度も書かない。
- 送るのは承認された文字列そのもの (作り直さない)。
- **同じ提案を二度と公開しない。** DB の一意制約・状態機械・media id で守る。
- 応答を取りこぼしたら ``uncertain`` で止まり、照合するまで再送しない。
- 読み戻して内容が違えば、提案ではなく **照合すべき問題** として立てる。
- token はログにも DB にも例外にも出ない。
- スケジューラは公開できない。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    PUB_FAILED,
    PUB_PUBLISHED,
    PUB_UNCERTAIN,
    TP_APPROVED,
    TP_AWAITING_APPROVAL,
    TP_REJECTED,
    TP_STALE,
    TP_SUPERSEDED,
    Article,
    ThreadsPostProposal,
    ThreadsPublication,
    ThreadsPublicationAttempt,
)
from app.services.threads_publication_service import (
    ThreadsPublicationError,
    ThreadsPublicationService,
)
from app.social.threads.errors import (
    ThreadsAuthError,
    ThreadsPermissionError,
    ThreadsRateLimitError,
    ThreadsResponseError,
    ThreadsTimeoutError,
)
from app.social.threads.models import (
    CONTAINER_ERROR,
    CONTAINER_IN_PROGRESS,
    CONTAINER_PUBLISHED,
    RECOMMENDED_PUBLISH_DELAY_SECONDS,
    ThreadsContainer,
    ThreadsInsights,
)
from app.social.threads.models import (
    ThreadsPublication as ThreadsPublicationDTO,
)

_BASE = "https://bizfluxlab.com"
_NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
_TOKEN = "THAAAsecret-token-must-never-appear"
_TEXT = "体制を先に決めたほうが早い。\n参照先と版を残しておくほうが更新しやすい。"


class _Settings:
    threads_enabled = True
    threads_user_id = "9876543210"
    threads_access_token = _TOKEN
    threads_api_base_url = "https://graph.threads.net"
    threads_api_version = "v1.0"
    wordpress_base_url = _BASE


def _settings(**overrides):
    return type("S", (_Settings,), overrides)()


class _FakeThreads:
    """ThreadsService の代役。呼ばれた順序を記録し、外へは出ない。"""

    def __init__(
        self,
        *,
        create_error=None,
        publish_error=None,
        media=None,
        container_status=None,
        status_error=None,
        insights=None,
    ) -> None:
        self.calls: list[str] = []
        self._create_error = create_error
        self._publish_error = publish_error
        self._media = (
            media
            if media is not None
            else {
                "id": "media-1",
                "text": _TEXT,
                "permalink": "https://www.threads.net/@bizfluxlab/post/abc",
                "timestamp": "2026-09-24T12:00:30+0000",
                "username": "bizfluxlab",
            }
        )
        self._container_status = container_status
        self._status_error = status_error
        self._insights = insights

    # -- the service reads config through describe() --------------------------
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
        self.calls.append(f"create:{text}")
        if self._create_error:
            raise self._create_error
        return ThreadsContainer("container-1")

    def publish_container(self, creation_id):
        self.calls.append(f"publish:{creation_id}")
        if self._publish_error:
            raise self._publish_error
        return ThreadsPublicationDTO("media-1")

    def fetch_container_status(self, creation_id):
        self.calls.append(f"status:{creation_id}")
        if self._status_error:
            raise self._status_error
        return {"status": self._container_status, "error_message": None}

    def fetch_publication(self, media_id):
        self.calls.append(f"readback:{media_id}")
        return self._media

    def media_insights(self, media_id, metrics=None):
        self.calls.append(f"insights:{media_id}")
        return self._insights or ThreadsInsights(
            subject=f"media:{media_id}", values={"views": 12}, missing=("likes",)
        )


@pytest.fixture
def article(session: Session) -> Article:
    row = Article(
        id=21,
        title="生成AIガイドライン｜要点と実務上の注意点",
        slug="generative-ai-guidelines",
        body="記事本文。",
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
def approved(session: Session, article: Article) -> ThreadsPostProposal:
    from app.article.draft_promotion_canonical import compute_text_hash

    row = ThreadsPostProposal(
        source_article_id=article.id,
        source_article_body_hash=compute_text_hash(article.body),
        angle="insight",
        link_mode="none",
        content_text=_TEXT,
        character_count=len(_TEXT),
        destination_url=None,
        content_seed="a" * 64,
        proposal_hash="b" * 64,
        policy_version="t2.1",
        generator_version="threads-proposal-1",
        status=TP_APPROVED,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _service(session, threads=None, sleeps=None) -> ThreadsPublicationService:
    return ThreadsPublicationService(
        session,
        settings=_settings(),
        threads_service=threads or _FakeThreads(),
        sleep=(sleeps.append if sleeps is not None else (lambda _s: None)),
    )


# -- eligibility -----------------------------------------------------------------
def test_an_approved_proposal_is_eligible(session, approved) -> None:
    plan = _service(session).plan(proposal_id=approved.id)

    assert plan.ok
    assert plan.publish_text == _TEXT
    assert plan.proposal_status == TP_APPROVED
    assert plan.stale is False
    assert plan.existing_publication is None
    assert any("threads_publish" in c for c in plan.would_call)


@pytest.mark.parametrize("status", [TP_AWAITING_APPROVAL, TP_REJECTED, TP_STALE, TP_SUPERSEDED])
def test_only_an_approved_proposal_can_be_published(session, approved, status) -> None:
    approved.status = status
    session.commit()

    plan = _service(session).plan(proposal_id=approved.id)

    assert not plan.ok
    assert any("only an approved proposal" in r for r in plan.blocked_reasons)


def test_a_changed_source_article_blocks_publishing(session, approved, article) -> None:
    article.body = article.body + "\n人が編集した。\n"
    session.commit()

    plan = _service(session).plan(proposal_id=approved.id)

    assert not plan.ok
    assert any("changed after the proposal was approved" in r for r in plan.blocked_reasons)


def test_an_unpublished_article_blocks_publishing(session, approved, article) -> None:
    article.status = "draft"
    session.commit()

    plan = _service(session).plan(proposal_id=approved.id)

    assert not plan.ok
    assert any("no longer published" in r for r in plan.blocked_reasons)


def test_a_text_length_mismatch_blocks_publishing(session, approved) -> None:
    approved.character_count = 999
    session.commit()

    plan = _service(session).plan(proposal_id=approved.id)

    assert not plan.ok
    assert any("character count" in r for r in plan.blocked_reasons)


def test_overlength_text_blocks_publishing(session, approved) -> None:
    approved.content_text = "あ" * 501
    approved.character_count = 501
    session.commit()

    plan = _service(session).plan(proposal_id=approved.id)

    assert not plan.ok
    assert any("between 1 and 500" in r for r in plan.blocked_reasons)


def test_disabled_threads_blocks_publishing(session, approved) -> None:
    from app.social.threads.service import ThreadsConnectionStatus

    class _Disabled(_FakeThreads):
        def describe(self):
            return ThreadsConnectionStatus(
                enabled=False,
                configured=False,
                api_version="v1.0",
                user_id_configured=False,
                access_token_configured=False,
            )

    plan = _service(session, _Disabled()).plan(proposal_id=approved.id)

    assert not plan.ok
    assert any("not enabled/configured" in r for r in plan.blocked_reasons)


# -- PLAN makes no writes ---------------------------------------------------------
def test_plan_makes_zero_meta_calls(session, approved) -> None:
    threads = _FakeThreads()

    _service(session, threads).plan(proposal_id=approved.id)

    assert threads.calls == []
    assert session.scalars(select(ThreadsPublication)).all() == []


def test_publish_without_execute_is_plan_only(session, approved) -> None:
    threads = _FakeThreads()

    outcome = _service(session, threads).publish(proposal_id=approved.id, execute=False)

    assert outcome.executed is False
    assert outcome.outcome == "planned"
    assert threads.calls == []
    assert session.scalars(select(ThreadsPublication)).all() == []


# -- the happy path ---------------------------------------------------------------
def test_execute_follows_the_documented_two_step_flow(session, approved) -> None:
    threads = _FakeThreads()
    sleeps: list[float] = []

    outcome = _service(session, threads, sleeps).publish(
        proposal_id=approved.id, execute=True, now=_NOW
    )

    assert outcome.outcome == "published"
    assert outcome.media_id == "media-1"
    assert outcome.creation_id == "container-1"
    # 承認された文字列がそのまま送られる。
    assert threads.calls[0] == f"create:{_TEXT}"
    assert threads.calls[1] == "publish:container-1"
    assert threads.calls[2] == "readback:media-1"
    # 公式が推奨する待ち時間を守る。
    assert sleeps == [RECOMMENDED_PUBLISH_DELAY_SECONDS]

    row = session.scalars(select(ThreadsPublication)).one()
    assert row.status == PUB_PUBLISHED
    assert row.threads_media_id == "media-1"
    assert row.permalink == "https://www.threads.net/@bizfluxlab/post/abc"
    assert row.remote_timestamp == "2026-09-24T12:00:30+0000"
    assert row.remote_username == "bizfluxlab"
    assert row.published_at is not None
    assert row.reconciliation_required is False
    assert row.exact_published_text == _TEXT


def test_the_link_is_published_exactly_as_approved(session, approved, article) -> None:
    url = f"{_BASE}/generative-ai-guidelines/?utm_campaign=article-21-insight&utm_source=threads"
    approved.link_mode = "article"
    approved.destination_url = url
    approved.content_text = f"本文。\n{url}"
    approved.character_count = len(approved.content_text)
    session.commit()
    threads = _FakeThreads(media={"id": "media-1", "text": approved.content_text})

    outcome = _service(session, threads).publish(proposal_id=approved.id, execute=True, now=_NOW)

    assert outcome.outcome == "published"
    sent = threads.calls[0].removeprefix("create:")
    assert sent == approved.content_text
    assert url in sent
    assert "/go/" not in sent


# -- duplicate protection ---------------------------------------------------------
def test_a_published_proposal_is_never_published_twice(session, approved) -> None:
    service = _service(session)
    service.publish(proposal_id=approved.id, execute=True, now=_NOW)

    threads = _FakeThreads()
    second = _service(session, threads).publish(proposal_id=approved.id, execute=True, now=_NOW)

    assert second.outcome == "blocked"
    assert any("already published" in r for r in second.blocked_reasons)
    assert threads.calls == []
    assert len(session.scalars(select(ThreadsPublication)).all()) == 1


def test_a_rerun_of_the_cli_cannot_duplicate_the_post(session, approved) -> None:
    service = _service(session)
    service.publish(proposal_id=approved.id, execute=True, now=_NOW)

    for _ in range(3):
        plan = service.plan(proposal_id=approved.id)
        assert not plan.ok

    assert len(session.scalars(select(ThreadsPublication)).all()) == 1


def test_the_database_enforces_one_publication_per_proposal(session, approved) -> None:
    from sqlalchemy.exc import IntegrityError

    session.add(
        ThreadsPublication(
            proposal_id=approved.id,
            proposal_hash=approved.proposal_hash,
            source_article_id=approved.source_article_id,
            angle=approved.angle,
            exact_published_text=approved.content_text,
            status="planned",
        )
    )
    session.commit()
    session.add(
        ThreadsPublication(
            proposal_id=approved.id,
            proposal_hash=approved.proposal_hash,
            source_article_id=approved.source_article_id,
            angle=approved.angle,
            exact_published_text=approved.content_text,
            status="planned",
        )
    )

    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# -- failures ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "error",
    [
        ThreadsAuthError("invalid token", status=401),
        ThreadsPermissionError("missing scope", status=403),
        ThreadsRateLimitError("slow down", status=429),
        ThreadsResponseError("bad request", status=400),
    ],
)
def test_a_container_failure_leaves_nothing_published(session, approved, error) -> None:
    threads = _FakeThreads(create_error=error)

    outcome = _service(session, threads).publish(proposal_id=approved.id, execute=True, now=_NOW)

    assert outcome.outcome == "failed"
    assert outcome.media_id is None
    row = session.scalars(select(ThreadsPublication)).one()
    assert row.status == PUB_FAILED
    assert row.threads_media_id is None
    # コンテナ作成で落ちたなら外には何も出ていないので、再試行はできる。
    assert threads.calls == [f"create:{_TEXT}"]


def test_a_timeout_after_the_publish_request_becomes_uncertain(session, approved) -> None:
    """**盲目的に再送しない。** 出たかどうか分からない状態で止める。"""

    threads = _FakeThreads(publish_error=ThreadsTimeoutError("publish timed out"))

    outcome = _service(session, threads).publish(proposal_id=approved.id, execute=True, now=_NOW)

    assert outcome.outcome == "uncertain"
    assert outcome.creation_id == "container-1"
    assert outcome.media_id is None
    assert any("reconcile" in n for n in outcome.notes)
    row = session.scalars(select(ThreadsPublication)).one()
    assert row.status == PUB_UNCERTAIN
    assert row.threads_creation_id == "container-1"


def test_an_uncertain_publication_blocks_a_blind_retry(session, approved) -> None:
    threads = _FakeThreads(publish_error=ThreadsTimeoutError("publish timed out"))
    service = _service(session, threads)
    service.publish(proposal_id=approved.id, execute=True, now=_NOW)

    retry = _FakeThreads()
    outcome = _service(session, retry).publish(proposal_id=approved.id, execute=True, now=_NOW)

    assert outcome.outcome == "blocked"
    assert any("reconcile it before retrying" in r for r in outcome.blocked_reasons)
    assert retry.calls == []


# -- reconciliation ---------------------------------------------------------------
def test_reconcile_confirms_a_post_that_actually_went_out(session, approved) -> None:
    """コンテナが PUBLISHED なら、応答を取りこぼしただけ。二重投稿しない。"""

    service = _service(session, _FakeThreads(publish_error=ThreadsTimeoutError("timeout")))
    service.publish(proposal_id=approved.id, execute=True, now=_NOW)

    reconciler = _FakeThreads(container_status=CONTAINER_PUBLISHED)
    outcome = _service(session, reconciler).reconcile(proposal_id=approved.id, now=_NOW)

    assert outcome.outcome == "published"
    assert "status:container-1" in reconciler.calls
    # 照合で公開扱いになっても、publish は呼び直さない。
    assert not any(c.startswith("publish:") for c in reconciler.calls)
    row = session.scalars(select(ThreadsPublication)).one()
    assert row.status == PUB_PUBLISHED


def test_reconcile_frees_a_container_that_errored(session, approved) -> None:
    service = _service(session, _FakeThreads(publish_error=ThreadsTimeoutError("timeout")))
    service.publish(proposal_id=approved.id, execute=True, now=_NOW)

    outcome = _service(session, _FakeThreads(container_status=CONTAINER_ERROR)).reconcile(
        proposal_id=approved.id, now=_NOW
    )

    assert outcome.outcome == "failed"
    row = session.scalars(select(ThreadsPublication)).one()
    assert row.status == PUB_FAILED
    assert row.threads_media_id is None


def test_reconcile_stays_uncertain_while_the_container_is_in_progress(session, approved) -> None:
    service = _service(session, _FakeThreads(publish_error=ThreadsTimeoutError("timeout")))
    service.publish(proposal_id=approved.id, execute=True, now=_NOW)

    outcome = _service(session, _FakeThreads(container_status=CONTAINER_IN_PROGRESS)).reconcile(
        proposal_id=approved.id, now=_NOW
    )

    assert outcome.outcome == "uncertain"
    assert session.scalars(select(ThreadsPublication)).one().status == PUB_UNCERTAIN


def test_reconcile_on_an_already_published_row_does_nothing(session, approved) -> None:
    service = _service(session)
    service.publish(proposal_id=approved.id, execute=True, now=_NOW)

    reconciler = _FakeThreads()
    outcome = _service(session, reconciler).reconcile(proposal_id=approved.id, now=_NOW)

    assert outcome.outcome == "published"
    assert reconciler.calls == []


# -- readback ---------------------------------------------------------------------
def test_a_readback_mismatch_flags_reconciliation_without_touching_the_proposal(
    session, approved
) -> None:
    """承認された内容が「送るべきだったもの」であり続ける。"""

    threads = _FakeThreads(media={"id": "media-1", "text": "まったく別の文章", "permalink": None})

    outcome = _service(session, threads).publish(proposal_id=approved.id, execute=True, now=_NOW)

    assert outcome.outcome == "published"
    assert outcome.reconciliation_required is True
    row = session.scalars(select(ThreadsPublication)).one()
    assert row.reconciliation_required is True
    assert row.exact_published_text == _TEXT
    # 提案は書き換えられていない。
    assert session.get(ThreadsPostProposal, approved.id).content_text == _TEXT


def test_attempts_are_append_only(session, approved) -> None:
    _service(session).publish(proposal_id=approved.id, execute=True, now=_NOW)

    attempts = session.scalars(
        select(ThreadsPublicationAttempt).order_by(ThreadsPublicationAttempt.id)
    ).all()

    assert [a.step for a in attempts] == ["create_container", "publish_container", "readback"]
    assert all(a.outcome == "succeeded" for a in attempts)


# -- insights (read-only) ---------------------------------------------------------
def test_inspect_reads_without_writing(session, approved) -> None:
    service = _service(session)
    service.publish(proposal_id=approved.id, execute=True, now=_NOW)
    row = session.scalars(select(ThreadsPublication)).one()

    threads = _FakeThreads()
    payload = _service(session, threads).inspect(publication_id=row.id)

    assert payload["media_id"] == "media-1"
    assert payload["text_matches_approved"] is True
    assert payload["insights"]["values"] == {"views": 12}
    # 取れなかった指標は 0 にしない。
    assert payload["insights"]["missing"] == ["likes"]
    assert not any(c.startswith(("create:", "publish:")) for c in threads.calls)


def test_inspect_refuses_a_publication_without_a_media_id(session, approved) -> None:
    session.add(
        ThreadsPublication(
            proposal_id=approved.id,
            proposal_hash=approved.proposal_hash,
            source_article_id=approved.source_article_id,
            angle=approved.angle,
            exact_published_text=approved.content_text,
            status="planned",
        )
    )
    session.commit()
    row = session.scalars(select(ThreadsPublication)).one()

    with pytest.raises(ThreadsPublicationError, match="no media id"):
        _service(session).inspect(publication_id=row.id)


# -- the token never leaks --------------------------------------------------------
def test_the_token_never_reaches_the_database_or_an_outcome(session, approved) -> None:
    threads = _FakeThreads(publish_error=ThreadsAuthError(f"bad access_token={_TOKEN}", status=401))

    outcome = _service(session, threads).publish(proposal_id=approved.id, execute=True, now=_NOW)

    stored = repr(
        [
            {c.name: getattr(r, c.name) for c in r.__table__.columns}
            for r in session.scalars(select(ThreadsPublication)).all()
        ]
        + [
            {c.name: getattr(r, c.name) for c in r.__table__.columns}
            for r in session.scalars(select(ThreadsPublicationAttempt)).all()
        ]
    )
    assert _TOKEN not in stored
    assert _TOKEN not in repr(outcome.as_dict())
    assert "[redacted]" in repr(outcome.as_dict())


# -- boundaries -------------------------------------------------------------------
def test_the_operations_runner_cannot_publish() -> None:
    import inspect

    from app.services import operations_runner_service as runner_mod

    source = inspect.getsource(runner_mod)

    assert "threads_publication_service" not in source
    assert "ThreadsPublicationService" not in source
    assert "publish_container" not in source


def test_mobile_approval_cannot_publish() -> None:
    import inspect

    from app.services import mobile_approval_service as mobile_mod

    source = inspect.getsource(mobile_mod)

    assert "ThreadsPublicationService" not in source
    assert "publish_container" not in source
    assert "create_text_container" not in source


def test_email_delivery_cannot_publish() -> None:
    import inspect

    from app.services import operations_notification_service as notify_mod

    source = inspect.getsource(notify_mod)

    assert "ThreadsPublicationService" not in source
    assert "publish" not in source.replace("send_weekly_report", "").replace(
        "publish_threads_post", ""
    )
