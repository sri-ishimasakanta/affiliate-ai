"""投稿案の在庫の保守 (T6、DB・manual provider・worker・CLI)。

pin する契約:

- PLAN は DB にもファイルにも書かない。
- 保存される提案は必ず awaiting_approval。自動で承認・却下・公開しない。既存の提案と
  承認には触れない。
- 1 回で保存するのは最大 3 本。答え待ちの依頼があれば新しく出さない。同じ依頼を
  二重に出さない。停止明けも 1 回の保守だけ。
- 壊れた出力・検査落ち・重複・保存の失敗・provider の失敗は、その依頼だけを止める。
  アラートは cooldown つきで、同じ問題を繰り返し通知しない。重複は通知しない。
- migration 前の DB: PLAN は動き、依頼も保存もしない。migration 後は保存できる。
- 監査は読むだけで、指紋の一致・不一致を判定する。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.article.draft_promotion_canonical import compute_text_hash
from app.models import (
    TP_APPROVED,
    TP_AWAITING_APPROVAL,
    Article,
    Keyword,
    OperationsAlert,
    ThreadsPostProposal,
    ThreadsPublication,
)
from app.services.threads_generation_provider import DisabledAutomatedProvider, ManualFileProvider
from app.services.threads_proposal_service import ThreadsProposalService
from app.services.threads_proposal_stock_service import ThreadsProposalStockService

_BASE = "https://bizfluxlab.com"
_NOW = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)  # 10:00 JST


class _Settings:
    threads_enabled = True
    threads_user_id = "9876543210"
    threads_access_token = "THAAA-never-used"
    threads_api_base_url = "https://graph.threads.net"
    threads_api_version = "v1.0"
    wordpress_base_url = _BASE


class _ReadOnlyThreads:
    """ThreadsService の代役。**書き込み系のメソッドを持たない。**"""

    def describe(self):
        from app.social.threads.service import ThreadsConnectionStatus

        return ThreadsConnectionStatus(
            enabled=True,
            configured=True,
            api_version="v1.0",
            user_id_configured=True,
            access_token_configured=True,
        )


class _Notifier:
    name = "email"

    def __init__(self) -> None:
        self.sent = []

    def send(self, message):
        from app.operations.notifications import NotificationResult

        self.sent.append(message)
        return NotificationResult(self.name, True)


@pytest.fixture
def articles(session: Session) -> list[Article]:
    topics = {}
    for word in ("生成AI", "Make", "AIエージェント"):
        topics[word] = Keyword(keyword=word)
        session.add(topics[word])
    session.flush()
    rows = []
    for article_id, word in (
        (21, "生成AI"),
        (22, "生成AI"),
        (23, "Make"),
        (24, "AIエージェント"),
        (25, "Make"),
    ):
        row = Article(
            id=article_id,
            title=f"記事{article_id}",
            slug=f"article-{article_id}",
            body=f"記事{article_id}の本文。体制を先に決めるほうが早い。",
            status="published",
            published_url=f"{_BASE}/article-{article_id}/",
            published_at=_NOW - timedelta(days=60),
            article_type="informational",
            keyword_id=topics[word].id,
        )
        session.add(row)
        rows.append(row)
    session.commit()
    return rows


def _service(session, tmp_path, *, notifier=None, provider=None, **kwargs):
    return ThreadsProposalStockService(
        session,
        settings=_Settings(),
        threads_service=_ReadOnlyThreads(),
        provider=provider or ManualFileProvider(tmp_path / "gen"),
        alert_notifiers=[notifier] if notifier else [],
        **kwargs,
    )


def _answer(provider: ManualFileProvider, body=None, *, extra=0) -> list[str]:
    """人が外部で prompt を実行し、結果を置く (依頼ごとに 1 本、要求どおりの切り口)。"""

    answered = []
    for request in provider.pending():
        angle = request.angles[0]
        items = [
            {
                "angle": angle,
                "link_mode": "none",
                "body": body or f"記事{request.article_id}の要点を一つ。{angle}として短く書く。",
            }
        ]
        items += [
            {"angle": angle, "link_mode": "none", "body": f"追加の案{i}。{request.article_id}"}
            for i in range(extra)
        ]
        (provider.directory / "pending" / f"{request.request_id}.response.json").write_text(
            json.dumps({"proposals": items}, ensure_ascii=False), encoding="utf-8"
        )
        answered.append(request.request_id)
    return answered


def _proposal_count(session) -> int:
    return session.scalar(select(func.count()).select_from(ThreadsPostProposal))


# == PLAN =======================================================================
def test_plan_needs_generation_and_writes_nothing(session, articles, tmp_path) -> None:
    plan = _service(session, tmp_path).plan(now=_NOW)
    assert plan["stock"]["needs_generation"] is True
    assert plan["would_request"] == 3
    assert plan["learning"]["mode"] == "neutral"
    assert plan["migration"]["can_save"] is True
    assert plan["provider"]["name"] == "manual"
    assert not (tmp_path / "gen").exists()  # ファイルも作らない
    assert _proposal_count(session) == 0
    topics = [r["topic"] for r in plan["stock"]["requests"]]
    assert len(set(topics)) == 3


# == the loop: request -> human output -> awaiting_approval =====================
def test_execute_requests_once_then_ingests_into_awaiting_approval(
    session, articles, tmp_path
) -> None:
    service = _service(session, tmp_path)
    first = service.maintain(now=_NOW, execute=True)
    assert len(first["requests_created"]) == 3
    assert first["created"] == []
    provider = service.provider
    assert len(provider.pending()) == 3
    assert (provider.directory / "status.json").exists()

    # 答えが来るまでは、何回動いても依頼は増えない (決定的な ID・答え待ちの抑止)。
    again = service.maintain(now=_NOW + timedelta(hours=6), execute=True)
    assert again["requests_created"] == []
    assert len(provider.pending()) == 3

    _answer(provider)
    done = service.maintain(now=_NOW + timedelta(hours=7), execute=True)
    rows = session.scalars(select(ThreadsPostProposal)).all()
    assert len(done["created"]) == 3 == len(rows)
    assert {r.status for r in rows} == {TP_AWAITING_APPROVAL}
    assert all(r.approved_at is None for r in rows)
    assert all(r.learning_guidance_json["verified_against_prompt"] is True for r in rows)
    assert provider.pending() == []
    assert len(list((provider.directory / "done").glob("*.outcome.json"))) == 3
    assert session.scalar(select(func.count()).select_from(ThreadsPublication)) == 0


def test_a_response_with_extra_proposals_stores_only_the_requested_one(
    session, articles, tmp_path
) -> None:
    service = _service(session, tmp_path)
    service.maintain(now=_NOW, execute=True)
    _answer(service.provider, extra=4)
    outcome = service.maintain(now=_NOW + timedelta(hours=1), execute=True)
    assert len(outcome["created"]) == 3  # 1 回 3 本の上限
    assert any("outside the request" in s["reason"] for s in outcome["skipped"])


def test_existing_proposals_and_approvals_are_untouched(session, articles, tmp_path) -> None:
    approved = ThreadsPostProposal(
        source_article_id=21,
        source_article_body_hash=compute_text_hash(articles[0].body),
        angle="insight",
        link_mode="none",
        content_text="承認済みの案。",
        character_count=7,
        content_seed="s" * 64,
        proposal_hash="S" * 64,
        policy_version="t2.1",
        generator_version="threads-proposal-1",
        status=TP_APPROVED,
        approved_at=_NOW - timedelta(hours=2),
    )
    session.add(approved)
    session.commit()
    session.expire_all()
    before = (approved.status, approved.approved_at, approved.updated_at, approved.content_text)

    service = _service(session, tmp_path)
    service.maintain(now=_NOW, execute=True)
    _answer(service.provider)
    service.maintain(now=_NOW + timedelta(hours=1), execute=True)
    session.expire_all()
    row = session.get(ThreadsPostProposal, approved.id)
    assert (row.status, row.approved_at, row.updated_at, row.content_text) == before
    assert (
        session.scalar(
            select(func.count())
            .select_from(ThreadsPostProposal)
            .where(ThreadsPostProposal.status == TP_APPROVED)
        )
        == 1
    )


# == failures ===================================================================
def test_malformed_output_stores_nothing_and_alerts_once(session, articles, tmp_path) -> None:
    notifier = _Notifier()
    service = _service(session, tmp_path, notifier=notifier)
    service.maintain(now=_NOW, execute=True)
    for request in service.provider.pending():
        (service.provider.directory / "pending" / f"{request.request_id}.response.json").write_text(
            "not json at all", encoding="utf-8"
        )
    outcome = service.maintain(now=_NOW + timedelta(hours=1), execute=True)
    assert outcome["created"] == []
    assert all("malformed output" in f["reason"] for f in outcome["failures"][:3])
    assert len(list((service.provider.directory / "failed").glob("*.outcome.json"))) == 3
    assert len(notifier.sent) == 1  # 3 件の失敗でも、同じ問題として 1 通

    # 次の回もまた壊れていても、cooldown の間は通知しない。
    service.maintain(now=_NOW + timedelta(hours=2), execute=True)
    for request in service.provider.pending():
        (service.provider.directory / "pending" / f"{request.request_id}.response.json").write_text(
            "{}", encoding="utf-8"
        )
    service.maintain(now=_NOW + timedelta(hours=3), execute=True)
    assert len(notifier.sent) == 1
    assert _proposal_count(session) == 0


def test_a_duplicate_output_is_skipped_without_an_alert(session, articles, tmp_path) -> None:
    notifier = _Notifier()
    service = _service(session, tmp_path, notifier=notifier)
    service.maintain(now=_NOW, execute=True)
    _answer(service.provider, body="まったく同じ本文。")
    outcome = service.maintain(now=_NOW + timedelta(hours=1), execute=True)
    # 1 本目は保存され、同じ本文の残りは重複として止まる。
    assert _proposal_count(session) == 1
    assert len(outcome["failures"]) == 2
    assert notifier.sent == []


def test_a_save_failure_approves_nothing_and_keeps_the_request(
    session, articles, tmp_path, monkeypatch
) -> None:
    notifier = _Notifier()
    service = _service(session, tmp_path, notifier=notifier)
    service.maintain(now=_NOW, execute=True)
    _answer(service.provider)

    def _broken(self, **_kwargs):
        raise OperationalError("INSERT", {}, Exception("disk I/O error"))

    monkeypatch.setattr(ThreadsProposalService, "persist", _broken)
    outcome = service.maintain(now=_NOW + timedelta(hours=1), execute=True)
    assert outcome["created"] == []
    assert all("save failed" in f["reason"] for f in outcome["failures"])
    assert len(service.provider.pending()) == 3  # 次の回に再試行できる
    alert = session.scalars(
        select(OperationsAlert).where(OperationsAlert.fingerprint.like("threads_proposal_stock:%"))
    ).one()
    assert (alert.fingerprint, alert.severity) == ("threads_proposal_stock:save_failed", "error")
    assert _proposal_count(session) == 0


def test_a_provider_failure_does_not_break_maintenance(session, articles, tmp_path) -> None:
    class _Exploding(ManualFileProvider):
        def submit(self, request):
            raise RuntimeError("provider unavailable")

    outcome = _service(session, tmp_path, provider=_Exploding(tmp_path / "gen")).maintain(
        now=_NOW, execute=True
    )
    assert outcome["requests_created"] == []
    assert "provider failed" in outcome["failures"][0]["reason"]
    assert _proposal_count(session) == 0


def test_the_disabled_automated_provider_never_requests(session, articles, tmp_path) -> None:
    service = _service(session, tmp_path, provider=DisabledAutomatedProvider())
    plan = service.plan(now=_NOW)
    assert plan["provider"]["available"] is False
    assert plan["would_request"] == 0
    assert "no approved automated LLM provider" in plan["provider"]["reason"]
    assert service.maintain(now=_NOW, execute=True)["requests_created"] == []


def test_an_unanswered_request_goes_stale_and_frees_the_next_cycle(
    session, articles, tmp_path
) -> None:
    service = _service(session, tmp_path)
    service.maintain(now=_NOW, execute=True)
    later = _NOW + timedelta(hours=73)
    outcome = service.maintain(now=later, execute=True)
    assert len(outcome["stale_requests"]) == 3
    assert len(outcome["requests_created"]) == 3  # 新しい依頼 (別の as_of なので別の ID)


# == migration gate =============================================================
def _unmigrate(session):
    session.execute(text("ALTER TABLE threads_post_proposals DROP COLUMN learning_guidance_json"))
    session.commit()


def test_before_the_migration_plan_works_and_nothing_is_requested_or_saved(
    session, articles, tmp_path
) -> None:
    service = _service(session, tmp_path)
    service.maintain(now=_NOW, execute=True)  # 移行後の DB で依頼だけ出ている状態
    _answer(service.provider)
    _unmigrate(session)

    plan = service.plan(now=_NOW + timedelta(hours=1))
    assert plan["migration"]["can_save"] is False
    assert any("migration" in b for b in plan["blocked_by"])
    outcome = service.maintain(now=_NOW + timedelta(hours=1), execute=True)
    assert outcome["created"] == []
    assert outcome["requests_created"] == []
    assert {s["reason"] for s in outcome["skipped"]} == {"migration required"}
    assert _proposal_count(session) == 0
    assert len(service.provider.pending()) == 3  # 取り込めるようになるまで残る


# == worker =====================================================================
def _factory(session: Session):
    class _Scoped:
        def __call__(self):
            return self

        def __enter__(self):
            return session

        def __exit__(self, *_exc):
            return False

    return _Scoped()


def _worker(session, tmp_path, **kwargs):
    from app.services.threads_worker_service import ThreadsWorkerService

    return ThreadsWorkerService(
        _factory(session),
        settings=_Settings(),
        threads_service=_ReadOnlyThreads(),
        stock_provider=ManualFileProvider(tmp_path / "gen"),
        alert_notifiers=[],
        **kwargs,
    )


def test_stock_maintenance_is_off_unless_the_worker_is_started_with_the_flag(
    session, articles, tmp_path
) -> None:
    from app.social.threads.worker import SUBSYSTEM_PROPOSAL_STOCK_MAINTENANCE

    state = (
        _worker(session, tmp_path).build_schedule(_NOW).state(SUBSYSTEM_PROPOSAL_STOCK_MAINTENANCE)
    )
    assert state.enabled is False
    assert state.disabled_reason == "start with --maintain-proposal-stock"


def test_two_days_of_resident_cycles_request_only_one_bounded_batch(
    session, articles, tmp_path
) -> None:
    from app.social.threads.worker import SUBSYSTEM_PROPOSAL_STOCK_MAINTENANCE

    clock = {"now": _NOW}
    service = _worker(session, tmp_path, maintain_proposal_stock=True)
    worker = service.build_worker(
        now=_NOW,
        clock=lambda: clock["now"],
        sleep=lambda s: clock.__setitem__("now", clock["now"] + timedelta(seconds=s)),
    )
    worker.run(max_cycles=600)  # 5 分刻みで約 2 日
    state = worker.schedule.state(SUBSYSTEM_PROPOSAL_STOCK_MAINTENANCE)
    assert 8 <= state.runs <= 12  # 6 時間ごと (5 分ごとではない)
    assert len(list((tmp_path / "gen" / "pending").glob("*.request.json"))) == 3
    assert _proposal_count(session) == 0
    assert session.scalar(select(func.count()).select_from(ThreadsPublication)) == 0


def test_after_downtime_the_worker_runs_one_bounded_maintenance(
    session, articles, tmp_path
) -> None:
    from app.social.threads.worker import SUBSYSTEM_PROPOSAL_STOCK_MAINTENANCE

    first = _worker(session, tmp_path, maintain_proposal_stock=True)
    first.build_worker(now=_NOW, clock=lambda: _NOW).run_cycle()
    # 3 日止まっていた後に起動しても、保守は 1 回・依頼は増やさない (答え待ちがある)。
    after = _NOW + timedelta(days=2)
    second = _worker(session, tmp_path, maintain_proposal_stock=True)
    worker = second.build_worker(now=after, clock=lambda: after)
    worker.run_cycle()
    worker.run_cycle()
    assert worker.schedule.state(SUBSYSTEM_PROPOSAL_STOCK_MAINTENANCE).runs == 1
    assert len(list((tmp_path / "gen" / "pending").glob("*.request.json"))) == 3


# == audit ======================================================================
def _stored(session, articles, tmp_path) -> list[int]:
    service = _service(session, tmp_path)
    service.maintain(now=_NOW, execute=True)
    _answer(service.provider)
    return service.maintain(now=_NOW + timedelta(hours=1), execute=True)["created"]


def test_the_audit_matches_rebuilds_and_detects_tampering(session, articles, tmp_path) -> None:
    proposal_id = _stored(session, articles, tmp_path)[0]
    audit = ThreadsProposalService(session).audit_guidance(proposal_id)
    assert audit["result"] == "match"
    assert audit["saved"]["fingerprint"] == audit["rebuilt"]["fingerprint"]

    row = session.get(ThreadsPostProposal, proposal_id)
    session.execute(
        update(ThreadsPostProposal)
        .where(ThreadsPostProposal.id == proposal_id)
        .values(learning_guidance_json={**row.learning_guidance_json, "fingerprint": "0" * 64})
    )
    session.commit()
    session.expire_all()
    before = session.get(ThreadsPostProposal, proposal_id).updated_at
    assert ThreadsProposalService(session).audit_guidance(proposal_id)["result"] == "mismatch"
    session.expire_all()
    assert session.get(ThreadsPostProposal, proposal_id).updated_at == before  # 読むだけ


def test_the_audit_reports_missing_provenance_and_unmigrated_databases(
    session, articles, tmp_path
) -> None:
    old = ThreadsPostProposal(
        source_article_id=21,
        source_article_body_hash="h" * 64,
        angle="insight",
        link_mode="none",
        content_text="T5.5 より前の案。",
        character_count=10,
        content_seed="o" * 64,
        proposal_hash="O" * 64,
        policy_version="t2.1",
        generator_version="threads-proposal-1",
        status=TP_AWAITING_APPROVAL,
    )
    session.add(old)
    session.commit()
    assert ThreadsProposalService(session).audit_guidance(old.id)["result"] == "no_provenance"
    _unmigrate(session)
    assert ThreadsProposalService(session).audit_guidance(old.id)["result"] == "not_migrated"


# == CLI ========================================================================
def test_the_plan_cli_explains_itself_and_changes_nothing(
    session, articles, tmp_path, capsys
) -> None:
    from scripts.maintain_threads_proposal_stock import main

    code = main(
        [],
        session_factory=_factory(session),
        settings=_Settings(),
        overrides={
            "threads_service": _ReadOnlyThreads(),
            "provider": ManualFileProvider(tmp_path / "gen"),
            "alert_notifiers": [],
        },
        now=_NOW,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "=== threads proposal stock (PLAN) ===" in out
    assert "usable stock       = 0 (advisory floor 3, ceiling 15; not a quota)" in out
    assert "generation needed  = True" in out
    assert "would request      = 3 (max 3 per cycle, one per article)" in out
    assert "learning guidance  = neutral (insufficient_sample" in out
    assert "migration          = learning provenance column present" in out
    assert "PLAN only: nothing was written" in out
    assert not (tmp_path / "gen").exists()
    assert _proposal_count(session) == 0


def test_the_audit_cli_exit_codes(session, articles, tmp_path, capsys) -> None:
    from scripts.audit_threads_proposal_guidance import main

    proposal_id = _stored(session, articles, tmp_path)[0]
    assert main(["--proposal-id", str(proposal_id)], session_factory=_factory(session)) == 0
    assert "result     = match" in capsys.readouterr().out
    assert main(["--proposal-id", "999"], session_factory=_factory(session)) == 2


# == learning ===================================================================
def _mature_history(session, article, *, seed, hours_ago, rate, chars):
    from app.models import PUB_PUBLISHED, SNAPSHOT_OBSERVED, ThreadsInsightSnapshot

    proposal = ThreadsPostProposal(
        source_article_id=article.id,
        source_article_body_hash=compute_text_hash(article.body),
        angle="insight",
        link_mode="none",
        content_text=f"過去の投稿 {seed} " + "あ" * chars,
        character_count=chars,
        content_seed=seed * 64,
        proposal_hash=seed.upper() * 64,
        policy_version="t2.1",
        generator_version="threads-proposal-1",
        status=TP_APPROVED,
        created_at=_NOW - timedelta(hours=hours_ago + 1),
    )
    session.add(proposal)
    session.flush()
    published = _NOW - timedelta(hours=hours_ago)
    row = ThreadsPublication(
        proposal_id=proposal.id,
        proposal_hash=proposal.proposal_hash,
        source_article_id=article.id,
        angle="insight",
        exact_published_text=proposal.content_text,
        threads_media_id=f"m-{seed}",
        status=PUB_PUBLISHED,
        published_at=published,
    )
    session.add(row)
    session.flush()
    session.add(
        ThreadsInsightSnapshot(
            threads_publication_id=row.id,
            threads_media_id=row.threads_media_id,
            observed_at=published + timedelta(hours=73),
            age_hours=73,
            outcome=SNAPSHOT_OBSERVED,
            views=1000,
            likes=round(rate * 1000),
            replies=0,
            reposts=0,
            quotes=0,
            shares=0,
        )
    )
    session.commit()
    return row


def test_sufficient_evidence_flows_weakly_into_requests_and_provenance(
    session, articles, tmp_path
) -> None:
    source = articles[-1]
    for i, seed in enumerate("abc"):
        _mature_history(session, source, seed=seed, hours_ago=500 + i, rate=0.10, chars=200)
    for i, seed in enumerate("def"):
        _mature_history(session, source, seed=seed, hours_ago=600 + i, rate=0.05, chars=400)
    service = _service(session, tmp_path)
    plan = service.plan(now=_NOW)
    assert plan["learning"]["mode"] == "weak"
    # 学習は記事・切り口の多様性を崩さない (3 本とも違う切り口・違う記事)。
    requests = plan["stock"]["requests"]
    assert len({r["angle"] for r in requests}) == len(requests)
    assert len({r["article_id"] for r in requests}) == len(requests)

    service.maintain(now=_NOW, execute=True)
    pending = service.provider.pending()
    assert {r.guidance_fingerprint for r in pending} == {plan["learning"]["fingerprint"]}
    assert all("長さ medium" in r.prompt for r in pending)

    # 依頼の後に新しい成熟した証拠が入っても、依頼の as_of で作り直すので一致する。
    for i, seed in enumerate("ghi"):
        _mature_history(session, source, seed=seed, hours_ago=-(2 + i), rate=0.9, chars=400)
    _answer(service.provider)
    outcome = service.maintain(now=_NOW + timedelta(hours=1), execute=True)
    rows = [session.get(ThreadsPostProposal, pid) for pid in outcome["created"]]
    assert len(rows) >= 1
    for row in rows:
        assert row.learning_guidance_json["applied"] is True
        assert row.learning_guidance_json["fingerprint"] == plan["learning"]["fingerprint"]
        assert row.learning_guidance_json["verified_against_prompt"] is True
        assert row.status == TP_AWAITING_APPROVAL


# == T6.1: collect-only never submits ============================================
class _NoSubmit(ManualFileProvider):
    """submit が呼ばれたらテストを失敗させる (collect-only の保証)。"""

    def submit(self, request):
        raise AssertionError("collect-only must never submit a generation request")


def _collect(session, tmp_path, **kwargs):
    return _service(session, tmp_path, provider=_NoSubmit(tmp_path / "gen"), **kwargs)


def test_collect_only_below_the_floor_submits_nothing(session, articles, tmp_path) -> None:
    service = _collect(session, tmp_path)
    plan = service.plan(now=_NOW, collect_only=True)
    assert plan["stock"]["needs_generation"] is True  # 在庫は下限より少ない
    assert plan["would_request"] == 0
    assert plan["submission_suppressed"] is True
    assert any("collect-only" in b for b in plan["blocked_by"])
    outcome = service.maintain(now=_NOW, execute=True, collect_only=True)
    assert outcome["requests_created"] == []
    assert outcome["mode"] == "collect_only"
    assert not (tmp_path / "gen" / "pending").exists()


def test_collect_only_ingests_existing_responses_without_a_second_batch(
    session, articles, tmp_path
) -> None:
    normal = _service(session, tmp_path)
    assert len(normal.maintain(now=_NOW, execute=True)["requests_created"]) == 3
    _answer(normal.provider)
    outcome = _collect(session, tmp_path).maintain(
        now=_NOW + timedelta(hours=1), execute=True, collect_only=True
    )
    assert len(outcome["created"]) == 3  # 届いた答えはすべて取り込む (1 回 3 本の上限内)
    assert outcome["requests_created"] == []
    rows = session.scalars(select(ThreadsPostProposal)).all()
    assert {r.status for r in rows} == {TP_AWAITING_APPROVAL}
    status = json.loads((tmp_path / "gen" / "status.json").read_text(encoding="utf-8"))
    assert status["mode"] == "collect_only"


def test_collect_only_keeps_the_normal_skip_and_fail_semantics(session, articles, tmp_path) -> None:
    normal = _service(session, tmp_path)
    normal.maintain(now=_NOW, execute=True)
    _answer(normal.provider, body="まったく同じ本文。")  # 1 本目は保存、残りは重複
    outcome = _collect(session, tmp_path).maintain(
        now=_NOW + timedelta(hours=1), execute=True, collect_only=True
    )
    assert len(outcome["created"]) == 1
    assert len(outcome["failures"]) == 2
    assert outcome["requests_created"] == []


def test_collect_only_with_no_response_yet_waits_and_submits_nothing(
    session, articles, tmp_path
) -> None:
    normal = _service(session, tmp_path)
    normal.maintain(now=_NOW, execute=True)
    service = _collect(session, tmp_path)
    plan = service.plan(now=_NOW + timedelta(hours=1), collect_only=True)
    assert plan["would_collect"] == 0
    outcome = service.maintain(now=_NOW + timedelta(hours=1), execute=True, collect_only=True)
    assert outcome["created"] == [] and outcome["requests_created"] == []
    assert len(service.provider.pending()) == 3  # 答えを待つ依頼はそのまま


def test_collect_only_reports_provider_errors_normally(session, articles, tmp_path) -> None:
    normal = _service(session, tmp_path)
    normal.maintain(now=_NOW, execute=True)
    for request in normal.provider.pending():
        (normal.provider.directory / "pending" / f"{request.request_id}.response.json").write_text(
            "not json", encoding="utf-8"
        )
    outcome = _collect(session, tmp_path).maintain(
        now=_NOW + timedelta(hours=1), execute=True, collect_only=True
    )
    assert all("malformed output" in f["reason"] for f in outcome["failures"])
    assert outcome["requests_created"] == []


def test_the_default_mode_is_unchanged(session, articles, tmp_path) -> None:
    outcome = _service(session, tmp_path).maintain(now=_NOW, execute=True)
    assert outcome["mode"] == "maintain"
    assert outcome["submission_suppressed"] is False
    assert len(outcome["requests_created"]) == 3
    assert outcome["plan"]["would_request"] == 3


def test_the_collect_only_plan_cli_says_submission_is_suppressed(
    session, articles, tmp_path, capsys
) -> None:
    from scripts.maintain_threads_proposal_stock import main

    assert (
        main(
            ["--collect-only"],
            session_factory=_factory(session),
            settings=_Settings(),
            overrides={
                "threads_service": _ReadOnlyThreads(),
                "provider": _NoSubmit(tmp_path / "gen"),
                "alert_notifiers": [],
            },
            now=_NOW,
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "mode               = collect_only (submission suppressed" in out
    assert "would request      = 0" in out
    assert "collect-only: submitting new generation requests is suppressed" in out
