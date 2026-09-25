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
    TP_AWAITING_APPROVAL,
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
#: 本番の公開 #3 の頃 (2026-09-25 09:39 JST、公開窓の中)。
_FROZEN = datetime(2026, 9, 25, 0, 39, 9, tzinfo=UTC)


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
    return ThreadsWorkerService(
        _factory(session),
        settings=configured,
        threads_service=threads,
        policy=_disabled_policy(),
    )


def _disabled_policy():
    """自動公開を明示的に無効にしたポリシー。

    本番のポリシーファイルは運用で有効にされうる (2026-09-25 に有効化された) ので、
    「無効のとき」の振る舞いを確かめる試験は、コミット済みの値に頼らず自分で用意する。
    """

    from dataclasses import replace

    base = _committed_policy()
    return replace(
        base,
        raw={**base.raw, "automatic_publication": {"enabled": False, "preflight_read": True}},
    )


def _committed_policy():
    from app.social.threads.policy import get_operations_policy

    return get_operations_policy()


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
    # T4.2: 提案・承認の変化は、公開の評価とまとめ送りの評価の両方を前倒しする。
    assert second.wake == {
        SUBSYSTEM_PUBLICATION_EVALUATION: later,
        "approval_notification_flush": later,
    }
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
    assert "can publish=False" in out
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


# == T4.2: real read-only insight refresh =====================================
class _ReadOnlyThreads:
    """読むだけの Threads。書き込み系メソッドは存在しない。"""

    def __init__(self, *, values=None, error=None) -> None:
        self.calls: list[str] = []
        self._values = values if values is not None else {"views": 40, "likes": 0}
        self._error = error

    def describe(self):
        from app.social.threads.service import ThreadsConnectionStatus

        return ThreadsConnectionStatus(
            enabled=True,
            configured=True,
            api_version="v1.0",
            user_id_configured=True,
            access_token_configured=True,
        )

    def media_insights(self, media_id, metrics=None):
        from app.social.threads.models import ThreadsInsights

        self.calls.append(f"insights:{media_id}")
        if self._error:
            raise self._error
        return ThreadsInsights(
            subject=f"media:{media_id}", values=dict(self._values), missing=("shares",)
        )


def _collecting_service(session: Session, threads, **kw) -> ThreadsWorkerService:
    return ThreadsWorkerService(
        _factory(session), settings=_Settings(), threads_service=threads, **kw
    )


def test_the_worker_reads_insights_only_when_asked(session: Session, article: Article) -> None:
    publication = _publication(
        session, _proposal(session, article), published_at=_NOW - timedelta(minutes=40)
    )
    threads = _ReadOnlyThreads()

    plan_only = _collecting_service(session, threads).handlers()[SUBSYSTEM_INSIGHTS_REFRESH](_NOW)
    assert threads.calls == []
    assert plan_only.summary["network_calls"] == 0

    result = _collecting_service(session, threads, collect_insights=True).handlers()[
        SUBSYSTEM_INSIGHTS_REFRESH
    ](_NOW)
    assert threads.calls == ["insights:m1"]
    assert result.summary["network_calls"] == 1
    assert result.summary["threads_writes"] == 0
    row = session.scalars(select(ThreadsInsightSnapshot)).one()
    assert row.threads_publication_id == publication.id
    assert row.views == 40
    # 欠測は NULL のまま (C8 と同じ意味づけ)。
    assert row.shares is None


def test_the_worker_does_not_refetch_what_c8_just_observed(
    session: Session, article: Article
) -> None:
    """C8 の日次取り込みが 3 分前に観測していれば、worker は取りに行かない。"""

    from app.services.threads_insights_service import ThreadsInsightsService

    _publication(session, _proposal(session, article), published_at=_NOW - timedelta(hours=2))
    c8_threads = _ReadOnlyThreads()
    ThreadsInsightsService(session, settings=_Settings(), threads_service=c8_threads).collect(
        execute=True, now=_NOW - timedelta(minutes=3)
    )

    worker_threads = _ReadOnlyThreads()
    service = _collecting_service(session, worker_threads, collect_insights=True)
    result = service.handlers()[SUBSYSTEM_INSIGHTS_REFRESH](_NOW)
    assert worker_threads.calls == []
    assert result.summary["would_refresh"] == []
    assert session.scalar(select(func.count()).select_from(ThreadsInsightSnapshot)) == 1


def test_the_shared_collector_skips_a_recent_observation_from_either_path(
    session: Session, article: Article
) -> None:
    from app.services.threads_insights_service import ThreadsInsightsService

    _publication(session, _proposal(session, article), published_at=_NOW - timedelta(hours=2))
    threads = _ReadOnlyThreads()
    collector = ThreadsInsightsService(session, settings=_Settings(), threads_service=threads)
    collector.collect(execute=True, now=_NOW)
    second = collector.collect(execute=True, now=_NOW + timedelta(minutes=4))

    assert threads.calls == ["insights:m1"]
    assert second.unchanged == 1
    assert "min ago" in second.details[0]["reason"]


def test_a_collection_failure_is_recorded_and_isolated(session: Session, article: Article) -> None:
    from app.social.threads.errors import ThreadsAuthError

    _publication(session, _proposal(session, article), published_at=_NOW - timedelta(minutes=40))
    threads = _ReadOnlyThreads(error=ThreadsAuthError("token rejected"))
    service = _collecting_service(session, threads, collect_insights=True)
    worker = service.build_worker(now=_NOW, clock=lambda: _NOW)
    worker.run_cycle()

    # 失敗は失敗の観測として残る (C8 と同じ)。worker の他の仕事は動いている。
    row = session.scalars(select(ThreadsInsightSnapshot)).one()
    assert row.outcome == "failed" and row.error_category == "threads_auth"
    assert worker.schedule.state("health").runs == 1
    assert worker.schedule.state(SUBSYSTEM_INSIGHTS_REFRESH).failures == 0


def test_low_views_from_the_worker_raise_no_alert(session: Session, article: Article) -> None:
    from app.services.threads_insights_service import ThreadsInsightsService

    _publication(session, _proposal(session, article), published_at=_NOW - timedelta(minutes=40))
    threads = _ReadOnlyThreads(values={"views": 0, "likes": 0})
    _collecting_service(session, threads, collect_insights=True).handlers()[
        SUBSYSTEM_INSIGHTS_REFRESH
    ](_NOW)
    drafts = ThreadsInsightsService(
        session, settings=_Settings(), threads_service=threads
    ).alert_drafts()
    assert drafts == []


# == T4.2: approval notification flush ========================================
def test_the_flush_is_enabled_but_sends_nothing_without_the_flag(
    session: Session, article: Article
) -> None:
    from app.models import TP_AWAITING_APPROVAL

    proposal = _proposal(session, article)
    proposal.status = TP_AWAITING_APPROVAL
    proposal.created_at = _NOW - timedelta(hours=3)
    session.commit()

    service = _service(session)
    handler = service.handlers()["approval_notification_flush"]
    result = handler(_NOW)

    assert result.summary["would_send"] is True
    assert result.summary["emails_sent"] == 0
    assert session.scalar(select(func.count()).select_from(NotificationDelivery)) == 0
    # 送れる状態でも PLAN なら、heartbeat ごとに空回りしない。
    assert result.next_run_at >= _NOW + timedelta(minutes=30)


def test_the_flush_does_not_run_every_heartbeat(session: Session, article: Article) -> None:
    clock = {"now": _NOW}

    def sleep(seconds: float) -> None:
        clock["now"] += timedelta(seconds=seconds)

    service = _service(session)
    worker = service.build_worker(now=_NOW, clock=lambda: clock["now"], sleep=sleep)
    worker.run(max_cycles=13)  # 1 時間
    flush = worker.schedule.state("approval_notification_flush")
    health = worker.schedule.state("health")
    queue = worker.schedule.state("queue_observation")
    assert health.runs == 13
    assert queue.runs == 13  # 5 分おき
    assert flush.runs <= 3


# -- failures are surfaced, not hidden (2026-09-25) ----------------------------
def test_the_cli_shows_a_failed_last_attempt(session: Session, article: Article, capsys) -> None:
    """成功した最後の観測だけを見せると、取得が失敗し続けても気付けない。"""

    from scripts.run_threads_worker import main

    publication = _publication(
        session, _proposal(session, article), published_at=_NOW - timedelta(hours=30)
    )
    session.add_all(
        [
            ThreadsInsightSnapshot(
                threads_publication_id=publication.id,
                threads_media_id="m1",
                observed_at=_NOW - timedelta(hours=20),
                outcome=SNAPSHOT_OBSERVED,
                views=146,
            ),
            ThreadsInsightSnapshot(
                threads_publication_id=publication.id,
                threads_media_id="m1",
                observed_at=_NOW - timedelta(hours=1),
                outcome="failed",
                error_category="threads_permission",
                error_message="/m1/insights failed: API access blocked.",
            ),
        ]
    )
    session.commit()

    status = _once(_service(session))
    latest = status["latest_publication"]
    assert latest["latest_attempt_outcome"] == "failed"
    assert latest["latest_attempt_error_category"] == "threads_permission"

    main([], session_factory=_factory(session), settings=_Settings())
    out = capsys.readouterr().out
    assert "LAST ATTEMPT FAILED" in out
    assert "API access blocked." in out
    assert _TOKEN not in out


# == T4.3: automatic publication through the worker ===========================
class _PublishingThreads(_ReadOnlyThreads):
    """公開できる Threads の代役 (外には出ない)。"""

    def __init__(self) -> None:
        super().__init__()
        self._texts: dict[str, str] = {}
        self._last = ""

    def check_connection(self):
        self.calls.append("preflight")
        status = self.describe()
        status.reachable = True
        return status

    @property
    def client(self):
        return self

    def create_text_container(self, text):
        from app.social.threads.models import ThreadsContainer

        self.calls.append("create")
        self._last = text
        return ThreadsContainer(f"c{len(self.calls)}")

    def publish_container(self, creation_id):
        from app.social.threads.models import ThreadsPublication as Dto

        self.calls.append("publish")
        media = f"media{len(self.calls)}"
        self._texts[media] = self._last
        return Dto(media)

    def fetch_publication(self, media_id):
        return {"id": media_id, "text": self._texts.get(media_id, ""), "permalink": "https://x"}


def _enabled_policy(tmp_path):
    import json

    from app.social.threads.policy import get_operations_policy, load_operations_policy

    document = dict(get_operations_policy().raw)
    document["automatic_publication"] = {"enabled": True, "preflight_read": True}
    path = tmp_path / "enabled.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return load_operations_policy(path)


def test_the_resident_worker_publishes_one_post_per_gap(
    session: Session, article: Article, tmp_path
) -> None:
    for i in range(5):
        proposal = _proposal(session, article, seed=f"q{i}", angle=f"angle{i}")
        proposal.approved_at = _NOW - timedelta(hours=5 - i)
    session.commit()

    threads = _PublishingThreads()
    clock = {"now": _NOW}

    def sleep(seconds: float) -> None:
        clock["now"] += timedelta(seconds=seconds)

    service = ThreadsWorkerService(
        _factory(session),
        settings=_Settings(),
        threads_service=threads,
        policy=_enabled_policy(tmp_path),
        auto_publish=True,
        publish_sleep=lambda _s: None,
    )
    lock = ThreadsWorkerLock(_factory(session), stale_after_minutes=15, owner_label="w")
    run = service.build_worker(now=_NOW, clock=lambda: clock["now"], sleep=sleep, lock=lock).run(
        max_cycles=60
    )  # 約 5 時間

    assert all(cycle.publications <= 1 for cycle in run.cycles)
    rows = session.scalars(
        select(ThreadsPublication).order_by(ThreadsPublication.published_at)
    ).all()
    assert 2 <= len(rows) <= 3
    assert all(r.trigger == "automatic" for r in rows)
    times = [r.published_at for r in rows]
    for earlier, later in zip(times, times[1:], strict=False):
        assert later - earlier >= timedelta(minutes=120)


def test_the_worker_does_not_publish_without_the_lock(
    session: Session, article: Article, tmp_path
) -> None:
    _proposal(session, article, seed="a")
    threads = _PublishingThreads()
    service = ThreadsWorkerService(
        _factory(session),
        settings=_Settings(),
        threads_service=threads,
        policy=_enabled_policy(tmp_path),
        auto_publish=True,
        publish_sleep=lambda _s: None,
    )
    worker = service.build_worker(now=_NOW, clock=lambda: _NOW)  # ロック無し
    worker.run_cycle()
    summary = worker.schedule.state(SUBSYSTEM_PUBLICATION_EVALUATION).last_summary
    assert summary["auto_publish"]["outcome"] == "gated"
    assert "create" not in threads.calls
    assert session.scalar(select(func.count()).select_from(ThreadsPublication)) == 0


def test_a_disabled_policy_keeps_the_worker_from_publishing(
    session: Session, article: Article
) -> None:
    _proposal(session, article, seed="a")
    threads = _PublishingThreads()
    service = ThreadsWorkerService(
        _factory(session),
        settings=_Settings(),
        threads_service=threads,
        auto_publish=True,
        policy=_disabled_policy(),
        publish_sleep=lambda _s: None,
    )
    lock = ThreadsWorkerLock(_factory(session), stale_after_minutes=15, owner_label="w")
    clock = {"now": _NOW}
    service.build_worker(
        now=_NOW,
        clock=lambda: clock["now"],
        sleep=lambda s: clock.__setitem__("now", clock["now"] + timedelta(seconds=s)),
        lock=lock,
    ).run(max_cycles=20)
    assert service.capabilities["publish"] is False
    assert "create" not in threads.calls
    assert session.scalar(select(func.count()).select_from(ThreadsPublication)) == 0


def test_the_status_carries_a_publication_dry_run(session: Session, article: Article) -> None:
    proposal = _proposal(session, article, seed="a")
    status = _once(_service(session))
    dry = status["publication_dry_run"]
    assert dry["proposal_id"] == proposal.id
    assert dry["gates"]["policy_enabled"] is False
    assert status["side_effects"]["threads_writes"] == 0


# == T4.3: approval sync in the worker ========================================
class _SyncRelay:
    def __init__(self, decisions) -> None:
        self._decisions = decisions

    def fetch_decisions(self):
        return list(self._decisions)

    def acknowledge(self, *, relay_session_id, local_outcome):
        return {"state": "consumed"}


def test_approval_sync_runs_only_with_the_flag(session: Session, article: Article) -> None:
    service = _service(session)
    worker = service.build_worker(now=_NOW, clock=lambda: _NOW)
    state = worker.schedule.state("approval_sync")
    assert state.enabled is False
    assert state.disabled_reason == "start with --sync-approvals"


def test_approval_sync_pulls_the_publication_evaluation_forward(
    session: Session, article: Article, monkeypatch
) -> None:
    from app.services.mobile_approval_service import MobileApprovalService, SyncOutcome

    monkeypatch.setattr(
        MobileApprovalService,
        "sync",
        lambda self, *, execute, now: SyncOutcome(fetched=1, applied=1, executed=execute),
    )
    service = ThreadsWorkerService(
        _factory(session),
        settings=_Settings(),
        threads_service=_ReadOnlyThreads(),
        sync_approvals=True,
        relay_client=_SyncRelay([]),
    )
    result = service.handlers()["approval_sync"](_NOW)
    assert result.summary["applied"] == 1
    assert result.wake == {
        SUBSYSTEM_PUBLICATION_EVALUATION: _NOW,
        SUBSYSTEM_QUEUE_OBSERVATION: _NOW,
    }
    assert result.next_run_at == _NOW + timedelta(minutes=5)


# == T4.3: problems in automatic publication are reported at once =============
class _RecordingNotifier:
    name = "email"

    def __init__(self) -> None:
        self.sent = []

    def send(self, message):
        from app.operations.notifications import NotificationResult

        self.sent.append(message)
        return NotificationResult(self.name, True)


class _LosingThreads(_PublishingThreads):
    """公開の応答を取りこぼす Threads の代役。"""

    def publish_container(self, creation_id):
        from app.social.threads.errors import ThreadsServerError

        self.calls.append("publish")
        raise ThreadsServerError("publish response lost", status=500)


class _BlockedThreads(_PublishingThreads):
    def check_connection(self):
        from app.social.threads.errors import ThreadsPermissionError

        self.calls.append("preflight")
        status = self.describe()
        status.reachable = False
        status.error = ThreadsPermissionError(
            "/me failed: API access blocked.", status=400, api_code="200"
        ).as_dict()
        return status


def _auto_worker(session, threads, tmp_path, notifier):
    service = ThreadsWorkerService(
        _factory(session),
        settings=_Settings(),
        threads_service=threads,
        policy=_enabled_policy(tmp_path),
        auto_publish=True,
        publish_sleep=lambda _s: None,
        alert_notifiers=[notifier],
    )
    lock = ThreadsWorkerLock(_factory(session), stale_after_minutes=15, owner_label="w")
    lock.acquire(_NOW)
    return service, service.build_worker(now=_NOW, clock=lambda: _NOW, lock=lock)


def test_an_uncertain_automatic_publication_is_alerted_immediately(
    session: Session, article: Article, tmp_path
) -> None:
    from app.models import OperationsAlert

    _proposal(session, article, seed="a")
    notifier = _RecordingNotifier()
    _service_, worker = _auto_worker(session, _LosingThreads(), tmp_path, notifier)
    worker.run_cycle()

    alert = session.scalars(select(OperationsAlert)).one()
    assert alert.fingerprint.startswith("threads_publication_uncertain:")
    assert alert.severity == "error"
    assert len(notifier.sent) == 1
    assert "THAAA" not in str(alert.evidence_json)


def test_a_repeated_problem_is_not_re_notified_every_cycle(
    session: Session, article: Article, tmp_path
) -> None:
    from app.models import OperationsAlert

    _proposal(session, article, seed="a")
    notifier = _RecordingNotifier()
    _service_, worker = _auto_worker(session, _BlockedThreads(), tmp_path, notifier)
    worker.run_cycle()
    worker.schedule.state(SUBSYSTEM_PUBLICATION_EVALUATION).next_run_at = _NOW
    worker.run_cycle()

    alert = session.scalars(select(OperationsAlert)).one()
    assert alert.fingerprint == "threads_autopublish_preflight:threads_permission"
    assert alert.occurrence_count == 2
    assert len(notifier.sent) == 1  # cooldown 中は再通知しない
    assert session.scalar(select(func.count()).select_from(ThreadsPublication)) == 0


def test_a_successful_automatic_publication_raises_no_alert(
    session: Session, article: Article, tmp_path
) -> None:
    from app.models import OperationsAlert

    _proposal(session, article, seed="a")
    notifier = _RecordingNotifier()
    _service_, worker = _auto_worker(session, _PublishingThreads(), tmp_path, notifier)
    worker.run_cycle()
    assert session.scalar(select(func.count()).select_from(ThreadsPublication)) == 1
    assert session.scalars(select(OperationsAlert)).all() == []
    assert notifier.sent == []


def test_the_daily_monitoring_also_sees_an_uncertain_publication(
    session: Session, article: Article
) -> None:
    from app.models import PUB_UNCERTAIN
    from app.services.threads_insights_service import ThreadsInsightsService

    proposal = _proposal(session, article, seed="a")
    _publication(session, proposal, published_at=None, status=PUB_UNCERTAIN)
    drafts = ThreadsInsightsService(
        session, settings=_Settings(), threads_service=_ReadOnlyThreads()
    ).alert_drafts(now=_NOW)
    assert any(d.fingerprint.startswith("threads_publication_uncertain:") for d in drafts)


# == first-pilot output regression (2026-09-25 09:39 JST) =====================
def _pilot_cli(session, article, tmp_path, threads, capsys, *, enabled=True):
    """--once --auto-publish を、本番の 1 本目と同じ条件で CLI から動かす。"""

    from scripts.run_threads_worker import main

    earlier = _proposal(session, article, seed="first", angle="insight")
    _publication(session, earlier, published_at=_NOW - timedelta(hours=30))
    for seed, angle in (("second", "beginner_tip"), ("third", "comparison")):
        proposal = _proposal(session, article, seed=seed, angle=angle)
        proposal.approved_at = _NOW - timedelta(hours=1)
    session.commit()

    overrides = {
        "threads_service": threads,
        "publish_sleep": lambda _s: None,
        "alert_notifiers": [],
    }
    overrides["policy"] = _enabled_policy(tmp_path) if enabled else _disabled_policy()
    code = main(
        ["--once", "--auto-publish"],
        session_factory=_factory(session),
        settings=_Settings(),
        overrides=overrides,
    )
    return code, capsys.readouterr().out


def test_a_real_automatic_post_is_reported_as_executed_not_as_plan(
    session: Session, article: Article, tmp_path, capsys, monkeypatch
) -> None:
    from scripts import run_threads_worker

    # 公開窓の中の時刻で動かす (CLI は現在時刻を使う)。
    monkeypatch.setattr(run_threads_worker, "datetime", _FrozenDatetime, raising=True)
    code, out = _pilot_cli(session, article, tmp_path, _PublishingThreads(), capsys)

    assert code == 0
    heading = out.splitlines()[0]
    assert "EXECUTED: published 1 post" in heading
    assert "PLAN" not in heading
    # 実行したのに「投稿はしていない」とは決して言わない。
    assert "投稿はしていない" not in out
    assert "1 本公開した" in out


def test_the_three_phases_are_labelled_separately(
    session: Session, article: Article, tmp_path, capsys, monkeypatch
) -> None:
    from scripts import run_threads_worker

    monkeypatch.setattr(run_threads_worker, "datetime", _FrozenDatetime, raising=True)
    _code, out = _pilot_cli(session, article, tmp_path, _PublishingThreads(), capsys)

    pre = out.split("--- (a) ")[1].split("--- (b)")[0]
    result = out.split("--- (b) ")[1].split("--- (c)")[0]
    after = out.split("--- (c) ")[1].split("--- subsystems")[0]

    # (a) 実行前: ロックを持っている状態の計画。
    assert pre.startswith("pre-execution plan")
    assert "worker_lock=True" in pre
    # (b) 実行結果: 1 本、書き込み 2 件 (作成・公開)、通信 4 件 (事前確認・作成・公開・読み戻し)。
    assert "posts published     = 1" in result
    assert "threads write calls = 2" in result
    assert "network calls       = 4" in result
    assert "auto-publish        = published" in result
    # (c) 実行後: 次のサイクルの話であり、ロックは解放済みだと明記する。
    assert after.startswith("next-cycle preview after this run")
    assert "lock was released" in after
    assert "worker_lock=False" in after


def test_blockers_do_not_claim_publication_is_disabled_when_it_ran(
    session: Session, article: Article, tmp_path, capsys, monkeypatch
) -> None:
    from scripts import run_threads_worker

    monkeypatch.setattr(run_threads_worker, "datetime", _FrozenDatetime, raising=True)
    _code, out = _pilot_cli(session, article, tmp_path, _PublishingThreads(), capsys)

    blockers = next(line for line in out.splitlines() if line.startswith("hard blockers"))
    assert "automatic_publication_disabled" not in blockers
    # 公開した直後なので、次は間隔待ち。
    assert "gap_not_elapsed" in blockers


def test_the_pilot_still_publishes_only_one_post(
    session: Session, article: Article, tmp_path, capsys, monkeypatch
) -> None:
    from scripts import run_threads_worker

    monkeypatch.setattr(run_threads_worker, "datetime", _FrozenDatetime, raising=True)
    _pilot_cli(session, article, tmp_path, _PublishingThreads(), capsys)
    rows = session.scalars(select(ThreadsPublication)).all()
    automatic = [r for r in rows if r.trigger == "automatic"]
    assert len(automatic) == 1  # 資格のある候補が 2 件あっても 1 件だけ


def test_a_lost_publish_counts_both_write_calls_and_says_so(
    session: Session, article: Article, tmp_path, capsys, monkeypatch
) -> None:
    from scripts import run_threads_worker

    monkeypatch.setattr(run_threads_worker, "datetime", _FrozenDatetime, raising=True)
    _code, out = _pilot_cli(session, article, tmp_path, _LosingThreads(), capsys)

    result = out.split("--- (b) ")[1].split("--- (c)")[0]
    assert "posts published     = 0" in result
    assert "threads write calls = 2" in result  # 作成 + (応答を取りこぼした) 公開
    assert "network calls       = 3" in result  # 事前確認 + 作成 + 公開 (読み戻しは無い)
    assert "auto-publish        = uncertain" in result
    assert "投稿はしていない" not in out
    assert "公開は確定していない" in out


def test_with_a_disabled_policy_the_flag_alone_is_reported_honestly(
    session: Session, article: Article, tmp_path, capsys, monkeypatch
) -> None:
    from scripts import run_threads_worker

    monkeypatch.setattr(run_threads_worker, "datetime", _FrozenDatetime, raising=True)
    threads = _PublishingThreads()
    _code, out = _pilot_cli(session, article, tmp_path, threads, capsys, enabled=False)

    assert "EXECUTED" not in out.splitlines()[0]
    assert "投稿はしていない" in out
    blockers = next(line for line in out.splitlines() if line.startswith("hard blockers"))
    assert "automatic_publication_disabled" in blockers
    assert "create" not in threads.calls


class _FrozenDatetime(datetime):
    """CLI の現在時刻を固定する (2026-09-25 09:39 JST、公開窓の中)。"""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 9, 25, 0, 39, 9, tzinfo=UTC)


def test_published_proposals_are_not_shown_as_waiting_stock(
    session: Session, article: Article
) -> None:
    published = _proposal(session, article, seed="p")
    _publication(session, published, published_at=_NOW - timedelta(hours=3))
    _proposal(session, article, seed="w")
    counts = _once(_service(session))["proposals"]
    assert counts["approved"] == 2
    assert counts["approved_unpublished"] == 1


# == resident observability (the log stayed 0 bytes) ==========================
def test_the_resident_cli_logs_startup_first_and_every_subsystem(
    session: Session, article: Article, capsys, monkeypatch
) -> None:
    """常駐モードは正常なら run() から戻らない。旧実装はその後にしか出力せず、
    ログが 0 バイトのままだった。いまは起動の直後から 1 行ずつ出す。"""

    from app.services.mobile_approval_service import MobileApprovalService, SyncOutcome
    from scripts import run_threads_worker

    publication = _publication(
        session, _proposal(session, article), published_at=_NOW - timedelta(minutes=40)
    )
    monkeypatch.setattr(run_threads_worker.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        MobileApprovalService,
        "sync",
        lambda self, *, execute, now: SyncOutcome(fetched=0, applied=0, executed=execute),
    )

    code = run_threads_worker.main(
        ["--resident", "--max-cycles", "2", "--collect-insights", "--sync-approvals"],
        session_factory=_factory(session),
        settings=_Settings(),
        overrides={
            "threads_service": _ReadOnlyThreads(),
            "relay_client": object(),
            "policy": _disabled_policy(),
        },
    )
    lines = capsys.readouterr().out.splitlines()

    assert code == 0
    assert "event=started" in lines[0]
    assert "mode=resident" in lines[0]
    assert "capabilities=collect_insights,sync_approvals" in lines[0]
    assert "auto_publish_policy=disabled" in lines[0]
    events = " ".join(lines)
    assert "event=lock_acquired" in events
    assert "event=health" in events
    assert 'event=approval_sync result="checked, no decisions"' in events
    assert f"#{publication.id}:imported" in events
    assert "event=publication_evaluation" in events
    assert "event=approval_notification_flush" in events
    assert "event=stopped reason=completed" in events
    # 眠るたびの行は無い: 2 サイクルで health の行は 1 本だけ (状態が変わらないため)。
    assert sum("event=health" in line for line in lines) == 1


def test_the_resident_log_carries_no_secret(
    session: Session, article: Article, capsys, monkeypatch
) -> None:
    from app.services.mobile_approval_service import MobileApprovalService
    from app.social.threads.errors import ThreadsAuthError
    from scripts import run_threads_worker

    _publication(session, _proposal(session, article), published_at=_NOW - timedelta(minutes=40))
    monkeypatch.setattr(run_threads_worker.time, "sleep", lambda _s: None)

    def failing_sync(self, *, execute, now):
        raise RuntimeError(
            "relay said https://bizfluxlab.com/bfl-approval/x#Zx8qP2vN5tR7yK1mW3eH9jL4uB6oC0dF "
            f"with access_token={_TOKEN} password=smtp-password-never-stored /go/aff_9f8e7d6c"
        )

    monkeypatch.setattr(MobileApprovalService, "sync", failing_sync)
    threads = _ReadOnlyThreads(error=ThreadsAuthError(f"rejected access_token={_TOKEN}"))
    run_threads_worker.main(
        ["--resident", "--max-cycles", "1", "--collect-insights", "--sync-approvals"],
        session_factory=_factory(session),
        settings=_Settings(),
        overrides={
            "threads_service": threads,
            "relay_client": object(),
            "policy": _disabled_policy(),
        },
    )
    out = capsys.readouterr().out

    assert "event=subsystem_failed" in out  # 失敗は出る
    assert "failed[threads_auth]" in out
    for secret in (
        _TOKEN,
        "THAAA",
        "smtp-password-never-stored",
        "Zx8qP2vN5tR7yK1mW3eH9jL4uB6oC0dF",
        "bfl-approval",
        "aff_9f8e7d6c",
    ):
        assert secret not in out


def test_an_already_running_resident_worker_says_so(
    session: Session, article: Article, capsys, monkeypatch
) -> None:
    from scripts import run_threads_worker

    monkeypatch.setattr(run_threads_worker.time, "sleep", lambda _s: None)
    ThreadsWorkerLock(_factory(session), stale_after_minutes=15, owner_label="first").acquire(
        datetime.now(UTC)
    )
    code = run_threads_worker.main(
        ["--resident", "--max-cycles", "1"],
        session_factory=_factory(session),
        settings=_Settings(),
        overrides={"threads_service": _ReadOnlyThreads(), "policy": _disabled_policy()},
    )
    out = capsys.readouterr().out
    assert code == 4
    assert "event=started" in out
    assert "event=already_running" in out


# == resident blocker log regression (publication #3, 2026-09-25) ==============
def test_the_resident_publication_line_reports_blockers_before_and_after(
    session: Session, article: Article, tmp_path
) -> None:
    """本番の常駐 worker が提案 #4 を公開 #3 として出したときのログを再現する。

    旧実装は、ポリシーも --auto-publish も有効で実際に公開したのに
    ``blockers=automatic_publication_disabled auto_publish=published`` と出していた
    (実行前の評価に publication_enabled を渡していなかった)。
    """

    from app.services.threads_worker_log import WorkerLogFormatter

    first = _proposal(session, article, seed="first", angle="insight")
    _publication(session, first, published_at=_FROZEN - timedelta(hours=30), media="m1")
    second = _proposal(session, article, seed="second", angle="beginner_tip")
    _publication(session, second, published_at=_FROZEN - timedelta(hours=3), media="m2")
    waiting = _proposal(session, article, seed="third", angle="comparison")
    waiting.status = TP_AWAITING_APPROVAL
    candidate = _proposal(session, article, seed="fourth", angle="mistake")
    candidate.approved_at = _FROZEN - timedelta(hours=1)
    session.commit()
    assert candidate.id == 4

    service = ThreadsWorkerService(
        _factory(session),
        settings=_Settings(),
        threads_service=_PublishingThreads(),
        policy=_enabled_policy(tmp_path),
        auto_publish=True,
        publish_sleep=lambda _s: None,
    )
    assert service.capabilities["publish"] is True
    formatter = WorkerLogFormatter(timezone=service.timezone)
    lines: list[str] = []

    def emit(event: dict) -> None:
        line = formatter.format(event)
        if line:
            lines.append(line)

    lock = ThreadsWorkerLock(_factory(session), stale_after_minutes=15, owner_label="w")
    service.build_worker(
        now=_FROZEN, clock=lambda: _FROZEN, sleep=lambda _s: None, lock=lock, on_event=emit
    ).run(max_cycles=1)

    line = next(line for line in lines if "event=publication_evaluation" in line)
    assert "next_candidate=4 blockers=(none) auto_publish=published publication=3" in line
    assert "automatic_publication_disabled" not in line
    # 公開の後の評価は別に出す: いま出したので次は間隔待ち。
    assert "next_blockers=" in line
    assert "gap_not_elapsed" in line.split("next_blockers=")[1]

    published = session.get(ThreadsPublication, 3)
    assert published.proposal_id == 4
    assert published.trigger == "automatic"
