"""常駐 Threads worker (T4.1、DB 連携、**Meta には一切接続しない**)。

pin する契約:

- 既定の 1 回評価は DB に何も書かない (ロック行さえ作らない)。
- どのモードでも Threads への呼び出しは 0 件 (HTTP 層に触れたら失敗する)。
- 承認メール・WordPress・/go/・スケジューラへの副作用は 0 件。
- 常駐モードが書くのは自分のロック行だけ。2 つ目の worker は何もせず終わる。
- 死んだ worker のロックは stale を過ぎれば回収できる (再起動に備える)。
- T3 の不確定な公開は hard blocker として出る。
- 「今日」の本数は JST の暦日で数える。
- 承認済みで資格があっても、T4.1 では公開行を作らない。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    PUB_PUBLISHED,
    PUB_UNCERTAIN,
    SNAPSHOT_OBSERVED,
    TP_APPROVED,
    Article,
    NotificationDelivery,
    OperationsLock,
    ThreadsInsightSnapshot,
    ThreadsPostProposal,
    ThreadsPublication,
    ThreadsPublicationAttempt,
    WordPressContentUpdateRun,
)
from app.services.threads_worker_service import (
    WORKER_LOCK_NAME,
    ThreadsWorkerLock,
    ThreadsWorkerService,
)
from app.social.threads.service import ThreadsService
from app.social.threads.worker import (
    EXIT_ALREADY_RUNNING,
    SUBSYSTEM_INSIGHTS_REFRESH,
    SUBSYSTEM_PUBLICATION_EVALUATION,
    SUBSYSTEM_QUEUE_OBSERVATION,
)

_BASE = "https://bizfluxlab.com"
_TOKEN = "THAAAsecret-token-must-never-appear"
_TEXT = "体制を先に決めたほうが早い。"
# 2026-09-24 10:00 JST。公開窓の中。
_NOW = datetime(2026, 9, 24, 1, 0, tzinfo=UTC)


class _Settings:
    threads_enabled = True
    threads_user_id = "9876543210"
    threads_access_token = _TOKEN
    threads_api_base_url = "https://graph.threads.net"
    threads_api_version = "v1.0"
    wordpress_base_url = _BASE


class _ExplodingClient:
    """HTTP 層の代役。**どのメソッドが呼ばれてもテストを失敗させる。**"""

    calls: list[str] = []

    def __getattr__(self, name):
        def _refuse(*_args, **_kwargs):
            _ExplodingClient.calls.append(name)
            raise AssertionError(f"the T4.1 worker must not call Threads ({name})")

        return _refuse


def _factory(session: Session):
    class _Scoped:
        def __call__(self):
            return self

        def __enter__(self):
            return session

        def __exit__(self, *_exc):
            return False

    return _Scoped()


def _service(session: Session, **settings) -> ThreadsWorkerService:
    configured = type("S", (_Settings,), settings)()
    threads = ThreadsService(configured, client=_ExplodingClient())
    return ThreadsWorkerService(_factory(session), settings=configured, threads_service=threads)


@pytest.fixture(autouse=True)
def _reset_client_calls():
    _ExplodingClient.calls = []
    yield
    assert _ExplodingClient.calls == [], "the worker called the Threads HTTP layer"


@pytest.fixture
def article(session: Session) -> Article:
    row = Article(
        id=21,
        title="生成AIガイドライン",
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


def _proposal(session: Session, article: Article, *, seed="a", angle="insight"):
    from app.article.draft_promotion_canonical import compute_text_hash

    row = ThreadsPostProposal(
        source_article_id=article.id,
        source_article_body_hash=compute_text_hash(article.body),
        angle=angle,
        link_mode="none",
        content_text=_TEXT,
        character_count=len(_TEXT),
        destination_url=None,
        content_seed=seed * 64,
        proposal_hash=seed.upper() * 64,
        policy_version="t2.1",
        generator_version="threads-proposal-1",
        status=TP_APPROVED,
    )
    session.add(row)
    session.commit()
    return row


def _publication(session: Session, proposal, *, published_at, status=PUB_PUBLISHED, media="m1"):
    row = ThreadsPublication(
        proposal_id=proposal.id,
        proposal_hash=proposal.proposal_hash,
        source_article_id=proposal.source_article_id,
        angle=proposal.angle,
        exact_published_text=proposal.content_text,
        threads_media_id=media if status == PUB_PUBLISHED else None,
        status=status,
        published_at=published_at if status == PUB_PUBLISHED else None,
    )
    session.add(row)
    session.commit()
    return row


def _counts(session: Session) -> dict:
    models = (
        ThreadsPublication,
        ThreadsPublicationAttempt,
        ThreadsInsightSnapshot,
        NotificationDelivery,
        WordPressContentUpdateRun,
        OperationsLock,
    )
    return {m.__name__: session.scalar(select(func.count()).select_from(m)) for m in models}


def _worker_lock_row(session: Session) -> OperationsLock:
    return session.scalars(
        select(OperationsLock).where(OperationsLock.lock_name == WORKER_LOCK_NAME)
    ).one()


def _once(service: ThreadsWorkerService, now: datetime = _NOW) -> dict:
    worker = service.build_worker(now=now, clock=lambda: now)
    worker.run_cycle()
    return service.status(now=now, schedule=worker.schedule)


# == default PLAN pass ========================================================
def test_the_default_pass_writes_nothing_and_calls_nothing(
    session: Session, article: Article
) -> None:
    _publication(session, _proposal(session, article), published_at=_NOW - timedelta(hours=3))
    before = _counts(session)

    status = _once(_service(session))

    assert _counts(session) == before
    assert status["side_effects"] == {
        "threads_writes": 0,
        "network_calls": 0,
        "approval_emails": 0,
        "wordpress_writes": 0,
        "go_probes": 0,
        "scheduler_changes": 0,
    }


def test_status_reports_everything_the_operator_needs(session: Session, article: Article) -> None:
    _publication(session, _proposal(session, article), published_at=_NOW - timedelta(minutes=40))
    status = _once(_service(session))

    assert status["now_local"].startswith("2026-09-24T10:00")
    assert status["timezone"] == "Asia/Tokyo"
    assert status["worker_mode"] == "plan"
    assert status["automatic_publication_enabled"] is False
    assert status["threads"]["state"] == "ready"
    assert status["threads"]["healthy"] is True
    assert status["publication_window"]["open_now"] is True
    assert status["approval_notification_window"]["open_now"] is True
    assert status["approval_notification_window"]["delivery_enabled"] is False
    assert status["latest_publication"]["minutes_since"] == 40.0
    assert status["latest_publication"]["maturity"] == "just_published"
    assert status["latest_publication"]["comparable"] is False
    assert status["soft_gap"]["reasons"] == ["gap_not_elapsed"]
    assert status["daily_activity"]["advisory"] is True
    assert status["proposals"]["approved"] == 1
    assert "automatic_publication_disabled" in status["hard_blockers"]
    names = {s["name"] for s in status["subsystems"]}
    assert {
        "health",
        "queue_observation",
        "publication_evaluation",
        "insights_refresh",
        "approval_notification_flush",
    } <= names


def test_the_token_never_appears_in_the_status(session: Session, article: Article) -> None:
    status = _once(_service(session))
    assert _TOKEN not in str(status)
    assert "THAAA" not in str(status)


# == timezone =================================================================
def test_todays_count_follows_the_jst_calendar(session: Session, article: Article) -> None:
    """18:24 UTC 09-23 は JST では 09-24 03:24。JST の「今日」(09-24) に数える。"""

    _publication(
        session, _proposal(session, article), published_at=datetime(2026, 9, 23, 18, 24, tzinfo=UTC)
    )
    status = _once(_service(session))
    assert status["daily_activity"]["count"] == 1
    assert status["latest_publication"]["published_local"].startswith("2026-09-24T03:24")


# == configuration semantics ==================================================
def test_disabled_threads_is_healthy(session: Session, article: Article) -> None:
    status = _once(_service(session, threads_enabled=False, threads_access_token=None))
    assert status["threads"]["state"] == "disabled"
    assert status["threads"]["healthy"] is True
    assert status["threads"]["config_issues"] == []
    assert status["problems"] == []


def test_enabled_but_missing_token_is_a_configuration_problem(
    session: Session, article: Article
) -> None:
    status = _once(_service(session, threads_access_token=None))
    assert status["threads"]["state"] == "misconfigured"
    assert status["threads"]["healthy"] is False
    assert status["threads"]["config_issues"] == ["THREADS_ACCESS_TOKEN is missing"]
    assert "threads_misconfigured" in status["problems"]


# == T3 safety ================================================================
def test_an_uncertain_publication_is_a_hard_blocker(session: Session, article: Article) -> None:
    _publication(
        session, _proposal(session, article, seed="u"), published_at=None, status=PUB_UNCERTAIN
    )
    _proposal(session, article, seed="b", angle="question")

    status = _once(_service(session))
    assert "uncertain_publication" in status["hard_blockers"]
    assert "uncertain_publication" in status["problems"]


def test_a_reconciliation_flag_is_also_a_hard_blocker(session: Session, article: Article) -> None:
    row = _publication(session, _proposal(session, article), published_at=_NOW - timedelta(days=1))
    row.reconciliation_required = True
    session.commit()

    assert "uncertain_publication" in _once(_service(session))["hard_blockers"]


def test_an_eligible_approved_proposal_is_not_published(session: Session, article: Article) -> None:
    """承認は公開ではない。資格があっても T4.1 は公開行を作らない。"""

    proposal = _proposal(session, article)
    status = _once(_service(session))

    assert status["queue"]["next_candidate"]["proposal_id"] == proposal.id
    assert status["queue"]["would_publish_now"] is False
    assert session.scalar(select(func.count()).select_from(ThreadsPublication)) == 0


# == subsystem timing =========================================================
def test_publication_evaluation_is_scheduled_at_the_gap_end(
    session: Session, article: Article
) -> None:
    last = datetime(2026, 9, 24, 0, 17, tzinfo=UTC)  # 09:17 JST
    _publication(session, _proposal(session, article), published_at=last)
    _proposal(session, article, seed="b", angle="question")
    now = datetime(2026, 9, 24, 0, 40, tzinfo=UTC)

    service = _service(session)
    worker = service.build_worker(now=now, clock=lambda: now)
    worker.run_cycle()
    state = worker.schedule.state(SUBSYSTEM_PUBLICATION_EVALUATION)
    assert state.next_run_at == datetime(2026, 9, 24, 2, 17, tzinfo=UTC)  # 11:17 JST


def test_insights_refresh_is_due_for_a_young_post_without_a_snapshot(
    session: Session, article: Article
) -> None:
    publication = _publication(
        session, _proposal(session, article), published_at=_NOW - timedelta(minutes=20)
    )
    service = _service(session)
    result = service.handlers()[SUBSYSTEM_INSIGHTS_REFRESH](_NOW)

    assert result.summary["would_refresh"] == [publication.id]
    assert result.summary["network_calls"] == 0
    # PLAN なので取得はしない。空回りしないよう、次の確認は 30 分後。
    assert result.next_run_at == _NOW + timedelta(minutes=30)
    assert session.scalar(select(func.count()).select_from(ThreadsInsightSnapshot)) == 0


def test_insights_refresh_waits_after_a_recent_snapshot(session: Session, article: Article) -> None:
    publication = _publication(
        session, _proposal(session, article), published_at=_NOW - timedelta(hours=2)
    )
    session.add(
        ThreadsInsightSnapshot(
            threads_publication_id=publication.id,
            threads_media_id="m1",
            observed_at=_NOW - timedelta(minutes=10),
            age_hours=1.8,
            outcome=SNAPSHOT_OBSERVED,
            views=10,
        )
    )
    session.commit()

    result = _service(session).handlers()[SUBSYSTEM_INSIGHTS_REFRESH](_NOW)
    assert result.summary["would_refresh"] == []


def test_mature_posts_are_refreshed_rarely(session: Session, article: Article) -> None:
    _publication(session, _proposal(session, article), published_at=_NOW - timedelta(hours=100))
    result = _service(session).handlers()[SUBSYSTEM_INSIGHTS_REFRESH](_NOW)
    assert result.next_run_at == _NOW + timedelta(minutes=360)


def test_a_new_approval_pulls_the_publication_evaluation_forward(
    session: Session, article: Article
) -> None:
    service = _service(session)
    handler = service.handlers()[SUBSYSTEM_QUEUE_OBSERVATION]
    first = handler(_NOW)
    assert first.wake == {}

    _proposal(session, article)
    later = _NOW + timedelta(minutes=10)
    second = handler(later)
    assert second.wake == {SUBSYSTEM_PUBLICATION_EVALUATION: later}
    assert second.summary["changed_since_last_observation"] is True


# == locking ==================================================================
def test_a_second_resident_worker_exits_safely(session: Session, article: Article) -> None:
    factory = _factory(session)
    first = ThreadsWorkerLock(factory, stale_after_minutes=15, owner_label="worker-1")
    assert first.acquire(_NOW)["acquired"] is True

    service = _service(session)
    second_lock = ThreadsWorkerLock(factory, stale_after_minutes=15, owner_label="worker-2")
    worker = service.build_worker(
        now=_NOW, clock=lambda: _NOW, sleep=lambda _s: None, lock=second_lock
    )
    run = worker.run(max_cycles=3)

    assert run.exit_code == EXIT_ALREADY_RUNNING
    assert run.cycles == []
    row = _worker_lock_row(session)
    # 1 つ目のロックは生きたまま (2 つ目が解放していない)。
    assert row.released_at is None
    assert row.owner_label == "worker-1"


def test_the_worker_lock_is_separate_from_the_c8_pipeline_lock(session: Session) -> None:
    from app.operations.lock import DEFAULT_LOCK_NAME, OperationsLockService

    OperationsLockService(session).acquire(lock_name=DEFAULT_LOCK_NAME, owner_run_id=1, now=_NOW)
    lock = ThreadsWorkerLock(_factory(session), stale_after_minutes=15, owner_label="w")
    assert lock.acquire(_NOW)["acquired"] is True


def test_a_dead_workers_lock_is_reclaimed_after_it_goes_stale(session: Session) -> None:
    factory = _factory(session)
    ThreadsWorkerLock(factory, stale_after_minutes=15, owner_label="dead").acquire(_NOW)

    fresh = ThreadsWorkerLock(factory, stale_after_minutes=15, owner_label="new")
    assert fresh.acquire(_NOW + timedelta(minutes=10))["acquired"] is False
    reclaimed = fresh.acquire(_NOW + timedelta(minutes=16))
    assert reclaimed["acquired"] is True
    assert reclaimed["reclaimed_stale"] is True


def test_a_resident_run_writes_only_its_own_lock_row(session: Session, article: Article) -> None:
    _publication(session, _proposal(session, article), published_at=_NOW - timedelta(hours=3))
    before = _counts(session)

    clock = {"now": _NOW}

    def sleep(seconds: float) -> None:
        clock["now"] += timedelta(seconds=seconds)

    service = _service(session)
    lock = ThreadsWorkerLock(_factory(session), stale_after_minutes=15, owner_label="w")
    run = service.build_worker(now=_NOW, clock=lambda: clock["now"], sleep=sleep, lock=lock).run(
        max_cycles=6
    )

    after = _counts(session)
    assert run.publications == 0
    assert after["OperationsLock"] == before["OperationsLock"] + 1
    assert {k: v for k, v in after.items() if k != "OperationsLock"} == {
        k: v for k, v in before.items() if k != "OperationsLock"
    }
    row = _worker_lock_row(session)
    assert row.released_at is not None  # 終了時に解放済み


# == CLI ======================================================================
def test_the_cli_default_is_read_only(session: Session, article: Article, capsys) -> None:
    from scripts.run_threads_worker import main

    _publication(session, _proposal(session, article), published_at=_NOW - timedelta(hours=3))
    before = _counts(session)
    settings = _Settings()

    code = main([], session_factory=_factory(session), settings=settings)
    out = capsys.readouterr().out

    assert code == 0
    assert _counts(session) == before
    assert "PLAN" in out
    assert "automatic publication = False" in out
    assert "threads_writes=0" in out
    assert _TOKEN not in out


def test_no_http_no_email_no_subprocess_in_any_mode(
    session: Session, article: Article, monkeypatch
) -> None:
    """HTTP (Threads / WordPress / /go/)、SMTP (承認メール)、subprocess (schtasks) の
    どれかに触れたら即失敗させて、既定の 1 回評価と常駐ループの両方を回す。"""

    import smtplib
    import subprocess

    import httpx

    def _refuse(*_args, **_kwargs):
        raise AssertionError("the T4.1 worker must not perform external I/O")

    monkeypatch.setattr(httpx.Client, "__init__", _refuse)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", _refuse)
    monkeypatch.setattr(smtplib.SMTP, "__init__", _refuse)
    monkeypatch.setattr(smtplib.SMTP_SSL, "__init__", _refuse)
    monkeypatch.setattr(subprocess, "run", _refuse)
    monkeypatch.setattr(subprocess, "Popen", _refuse)

    _publication(session, _proposal(session, article), published_at=_NOW - timedelta(hours=3))
    _proposal(session, article, seed="b", angle="question")

    # worker は仕事の例外を握りつぶして続行する設計なので、拒否された I/O は
    # 「その仕事の失敗」として記録される。**失敗が 0 件** であることを確かめる。
    once_service = _service(session)
    once = once_service.build_worker(now=_NOW, clock=lambda: _NOW)
    once.run_cycle()
    once_service.status(now=_NOW, schedule=once.schedule)
    assert [s.last_error for s in once.schedule.states() if s.failures] == []

    clock = {"now": _NOW}
    service = _service(session)
    lock = ThreadsWorkerLock(_factory(session), stale_after_minutes=15, owner_label="w")
    resident = service.build_worker(
        now=_NOW,
        clock=lambda: clock["now"],
        sleep=lambda s: clock.__setitem__("now", clock["now"] + timedelta(seconds=s)),
        lock=lock,
    )
    run = resident.run(max_cycles=30)

    assert run.publications == 0
    assert [s.last_error for s in resident.schedule.states() if s.failures] == []
    assert sum(s.runs for s in resident.schedule.states()) > len(run.cycles)
