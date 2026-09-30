"""Site Growth Orchestrator と運用の健康 (C10-3)。偽物だけ (外に問い合わせない)。

pin する契約:

- 行動の表: 10 の行動すべてに道筋と本番の状態がある。Threads は直接に投稿・model を呼ばない。
  Growth post は plan only、アフィリエイトの配置は C11 まで deferred。
- 発見の候補: 計画は 5 件まで・書かない。Keyword にするのは 1 件ずつ・指紋が同じときだけ。
  重なり・同じ語の Keyword があれば止まる。Keyword のほかに記事も計画の依頼も作らない。
  保存済みの Google Ads の値から signal を作る (呼び直さない)。
- 健康: 正常なら警告 0。夜の分析の失敗・実行漏れ、worker の停止、古いデータの出所、滞りを
  見つける。同じ問題は 1 つの警告 (繰り返し知らせない)。直れば自動で解決。承認済み
  (acknowledged) の警告は知らせない。日ごとの報告のメールに入れない。
- system_status CLI は書かない。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.growth import analysis as ga
from app.growth import orchestration as orch
from app.models import Article, Keyword, KeywordSignal, OperationsAlert, OperationsLock
from app.models.content_discovery import ContentDiscoveryCandidate, NightlyAnalysisRun
from app.operations import system_health as sh
from app.services.discovery_promotion_service import (
    DiscoveryPromotionError,
    DiscoveryPromotionService,
)
from app.services.system_health_service import SystemHealthService
from tests.support.google_ads_fakes import dummy_google_ads_settings

_NOW = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)  # 09:00 JST


# == 行動の表 ====================================================================================
def test_action_matrix_covers_every_action_with_a_safe_path() -> None:
    assert set(orch.ACTION_MATRIX) == set(ga.ACTION_TYPES)
    m = orch.ACTION_MATRIX
    assert m[ga.REVIEW_INTERNAL_LINKS].production == orch.ENABLED
    assert m[ga.CREATE_NEW_ARTICLE].production == orch.HUMAN_DRIVEN
    assert m[ga.UPDATE_EXISTING_ARTICLE].production == orch.PENDING_ACTIVATION
    assert m[ga.IMPROVE_SEARCH_SNIPPET].production == orch.PENDING_ACTIVATION
    assert m[ga.REVIEW_AFFILIATE_PLACEMENT].production == orch.DEFERRED
    assert m[ga.CREATE_GROWTH_POST].production == orch.PLAN_ONLY
    for action in (ga.CREATE_REGULAR_THREADS_POST, ga.CREATE_THREADS_ALTERNATIVE_ANGLE):
        assert "generation request" in m[action].handoff  # model は既存の流れだけが呼ぶ
        assert "proposal approval" in m[action].downstream_approval
    stage = orch.stage_of(candidate_status="approved", review_status="approved", converted=False,
                          effective_at=None, downstream_open=False)
    assert stage == orch.STAGE_APPROVED
    assert orch.next_step(ga.CREATE_GROWTH_POST, stage).startswith("plan only")
    handed = orch.stage_of(candidate_status="converted", review_status="approved",
                           converted=True, effective_at=None, downstream_open=True)
    assert "pending activation" in orch.next_step(ga.IMPROVE_SEARCH_SNIPPET, handed)
    assert orch.next_step(ga.WAIT_FOR_MORE_DATA, handed).startswith("informational")


# == 発見の候補 ==================================================================================
def _candidate(session, phrase, key, *, ads=None):
    row = ContentDiscoveryCandidate(
        phrase_key=key, phrase=phrase, status="tracked", duplicate_state="new",
        sources_json=[{"source": "gsc_query"}], evidence_json={"gsc_impressions": 4,
                                                               **({"google_ads": ads} if ads
                                                                  else {})},
        refresh_needs_json=[], first_seen_at=_NOW, last_seen_at=_NOW, seen_count=1,
        updated_at=_NOW)
    session.add(row)
    session.commit()
    return row


def _item(key, *, cluster="c1", gaps=(), cannibalization=()):
    return SimpleNamespace(keyword_id=None, discovery_key=key, cluster_id=cluster,
                           gaps_filled=tuple(gaps), content_type={"role": "comparison"},
                           monetization_role="support", cannibalization=tuple(cannibalization))


class _FakeIntelligence:
    def __init__(self, items):
        self.items = items

    def build(self, *, now):
        return SimpleNamespace(next_articles=self.items)


_ADS = {"returned": True, "observed_at": "2026-09-30T02:40:00+00:00",
        "avg_monthly_searches": 170, "search_volume_evidence": "observed",
        "search_demand": 0.42, "commercial_intent": 0.6, "market_evidence_state": "observed",
        "competition": "MEDIUM", "normalizers": {"search_demand": "v2",
                                                 "commercial_intent": "v2"},
        "source_reference": "google_ads:historical_metrics"}


def _counts(session):
    return tuple(session.scalar(select(func.count()).select_from(m))
                 for m in (Keyword, KeywordSignal, Article))


def test_discovery_plan_shows_at_most_five_and_writes_nothing(session) -> None:
    items = []
    for i in range(8):
        _candidate(session, f"語 {i}", f"語{i}")
        items.append(_item(f"語{i}", cluster=f"c{i % 3}", gaps=("pricing",) if i == 5 else ()))
    service = DiscoveryPromotionService(session, intelligence=_FakeIntelligence(items))
    before = _counts(session)
    plan = service.plan(now=_NOW, limit=50)
    assert len(plan["shown"]) == 5 and plan["eligible"] == 8
    assert plan["shown"][0]["phrase"] == "語 5"  # gap を埋める候補が先
    assert len({v["cluster_id"] for v in plan["shown"][:3]}) == 3  # まずクラスタごとに 1 つ
    assert "no approve-all" in plan["notes"][0]
    assert _counts(session) == before


def test_promotion_is_individual_fingerprinted_and_creates_only_a_keyword(session) -> None:
    row = _candidate(session, "rpa ライセンス", "rpaライセンス", ads=_ADS)
    service = DiscoveryPromotionService(
        session, intelligence=_FakeIntelligence([_item("rpaライセンス")]))
    fp = service.plan(now=_NOW)["shown"][0]["promotion_fingerprint"]
    with pytest.raises(DiscoveryPromotionError, match="fingerprint"):
        service.promote(row.id, expected_fingerprint="0" * 16, now=_NOW)
    assert _counts(session) == (0, 0, 0)
    result = service.promote(row.id, expected_fingerprint=fp, now=_NOW)
    assert result["external_calls"] == 0 and result["articles_created"] == 0
    assert _counts(session) == (1, 2, 0)  # Keyword 1 つと保存済みの値からの signal だけ
    signal = session.scalars(select(KeywordSignal).where(
        KeywordSignal.component == "search_demand")).one()
    assert signal.provider == "google_ads" and signal.normalized_value == pytest.approx(0.42)
    assert signal.raw_data["rederived_without_external_call"] is True
    session.refresh(row)
    assert row.status == "promoted" and row.evidence_json["promotion"]["fingerprint"] == fp
    with pytest.raises(DiscoveryPromotionError, match="promoted"):
        service.promote(row.id, expected_fingerprint=fp, now=_NOW)


def test_promotion_refuses_overlap_and_existing_keywords(session) -> None:
    overlap = _candidate(session, "crm 比較", "crm比較")
    dup = _candidate(session, "Zapier 使い方", "zapier使い方")
    session.add(Keyword(keyword="Zapier 使い方"))
    session.commit()
    hit = {"rule": "same_intent", "kind": "article", "ref": "article:3"}
    service = DiscoveryPromotionService(session, intelligence=_FakeIntelligence([
        _item("crm比較", cannibalization=(hit,)), _item("zapier使い方")]))
    plan = service.plan(now=_NOW)
    assert plan["blocked_by_cannibalization"] == 1
    assert [v["candidate_id"] for v in plan["shown"]] == [dup.id]
    views = {v["candidate_id"]: v for v in service._candidates(_NOW)}
    with pytest.raises(DiscoveryPromotionError, match="cannibalization"):
        service.promote(overlap.id, expected_fingerprint=views[overlap.id][
            "promotion_fingerprint"], now=_NOW)
    with pytest.raises(DiscoveryPromotionError, match="already has this phrase"):
        service.promote(dup.id, expected_fingerprint=views[dup.id]["promotion_fingerprint"],
                        now=_NOW)
    assert session.scalar(select(func.count()).select_from(Keyword)) == 1


# == 健康 (pure) ===================================================================================
def _policy():
    return sh.load_health_policy()


def _jst(h, m=0, day=1):
    from app.operations.policy import get_policy

    return datetime(2026, 10, day, h, m, tzinfo=get_policy().timezone)


def test_nightly_findings_missed_failed_stale_and_healthy() -> None:
    p = _policy()
    ok = {"run_key": "nightly:2026-10-01", "status": "succeeded"}
    assert sh.nightly_findings(p, now_local=_jst(9), schedule_time="03:30", registered=True,
                               last_run=ok, last_success_at=_jst(3, 31), eligible=40,
                               budget=80) == []
    old = {"run_key": "nightly:2026-09-30", "status": "succeeded"}
    missed = sh.nightly_findings(p, now_local=_jst(9), schedule_time="03:30", registered=True,
                                 last_run=old, last_success_at=_jst(3, 31, day=30 - 29),
                                 eligible=40, budget=80)
    assert "nightly_missed" in {f.check for f in missed}
    early = sh.nightly_findings(p, now_local=_jst(5), schedule_time="03:30", registered=True,
                                last_run=old, last_success_at=_jst(4) - timedelta(days=1),
                                eligible=40, budget=80)
    assert early == []  # 猶予の間は知らせない
    failed = {"run_key": "nightly:2026-10-01", "status": "failed", "failure_reason": "boom"}
    checks = {f.check: f for f in sh.nightly_findings(
        p, now_local=_jst(9), schedule_time="03:30", registered=True, last_run=failed,
        last_success_at=_jst(3) - timedelta(days=2), eligible=500, budget=80)}
    assert {"nightly_failed", "nightly_missed", "nightly_stale"} <= set(checks)
    assert checks["nightly_failed"].actionable
    assert not checks["nightly_candidate_explosion"].actionable  # 要約にだけ出す
    assert checks["nightly_failed"].fingerprint == sh.finding(
        p, "nightly_failed", "nightly", "x", "different text").fingerprint  # 日付を含まない


def test_worker_findings() -> None:
    p = _policy()
    fresh = {"heartbeat_at": _NOW - timedelta(minutes=5), "released_at": None}
    assert sh.worker_findings(p, now=_NOW, lock=fresh, log_errors=0) == []
    stale = {"heartbeat_at": _NOW - timedelta(hours=2), "released_at": None}
    found = sh.worker_findings(p, now=_NOW, lock=stale, log_errors=2)
    assert [f.check for f in found] == ["worker_stopped", "worker_log_errors"]
    assert found[0].severity == "error"


# == 健康 (service と警告) =======================================================================
class _Notifier:
    name = "fake"

    def __init__(self):
        self.sent = []

    def send(self, message):
        from app.operations.notifications import NotificationResult

        self.sent.append(message)
        return NotificationResult(provider=self.name, delivered=True)


def _service(session, tmp_path, *, schema=({"head"}, {"head"})):
    log = tmp_path / "threads-worker.log"
    log.write_text("", encoding="utf-8")
    return SystemHealthService(session, settings=dummy_google_ads_settings(), worker_log=log,
                               task_registered=lambda _name: True,
                               schema_heads=lambda: schema)


def _healthy_state(session, now):
    session.add(OperationsLock(lock_name="threads_worker", owner_label="threads-worker pid=1",
                               acquired_at=now - timedelta(hours=5),
                               heartbeat_at=now - timedelta(minutes=3)))
    session.add(NightlyAnalysisRun(
        run_key="nightly:2026-10-01", local_date=date(2026, 10, 1), status="succeeded",
        attempt_count=1, budget=80,
        started_at=now - timedelta(hours=5, minutes=30),
        finished_at=now - timedelta(hours=5, minutes=29), eligible_count=40,
        analyzed_count=40, skipped_count=0, deferred_count=0, refresh_required_count=0,
        next_article_count=5))
    session.commit()


def test_a_healthy_system_records_no_alert(session, tmp_path) -> None:
    _healthy_state(session, _NOW)
    service = _service(session, tmp_path)
    result = service.evaluate(now=_NOW)
    assert result["healthy"], result["actionable"]
    notifier = _Notifier()
    assert service.sync_alerts(now=_NOW, notifiers=[notifier])["recorded"] == 0
    assert session.scalar(select(func.count()).select_from(OperationsAlert)) == 0
    assert notifier.sent == []


def test_problems_alert_once_stay_quiet_and_resolve_when_cleared(session, tmp_path) -> None:
    _healthy_state(session, _NOW)
    lock = session.scalars(select(OperationsLock)).one()
    lock.heartbeat_at = _NOW - timedelta(hours=3)  # worker 停止
    # 夜の分析の行は消す (日が進むと実行漏れの警告が別に出る。それは上の pure なテストで見る)。
    session.delete(session.scalars(select(NightlyAnalysisRun)).one())
    session.commit()
    service = _service(session, tmp_path, schema=({"old"}, {"head"}))
    notifier = _Notifier()
    first = service.sync_alerts(now=_NOW, notifiers=[notifier])
    assert first["recorded"] == 2 and first["notified"] == 2
    rows = session.scalars(select(OperationsAlert)).all()
    assert {r.source for r in rows} == {sh.SOURCE}
    assert all(r.operations_run_id is None for r in rows)  # 日ごとの報告のメールに入れない
    # 翌日も同じ問題: 新しい警告も知らせも無い
    again = service.sync_alerts(now=_NOW + timedelta(days=1), notifiers=[notifier])
    assert again["notified"] == 0 and len(notifier.sent) == 2
    assert session.scalar(select(func.count()).select_from(OperationsAlert)) == 2
    # 人が承認した警告は、知らせ直しの間隔を過ぎても知らせない
    worker_alert = next(r for r in rows if r.evidence_json["check"] == "worker_stopped")
    worker_alert.status = "acknowledged"
    session.commit()
    later = service.sync_alerts(now=_NOW + timedelta(days=8), notifiers=[notifier])
    assert later["notified"] == 1  # schema の警告だけが 168 時間後に知らせ直す
    # 直った: 自動で解決
    lock.heartbeat_at = _NOW + timedelta(days=8, minutes=-2)
    session.commit()
    healed = _service(session, tmp_path).sync_alerts(now=_NOW + timedelta(days=8, minutes=1),
                                                     notifiers=[notifier])
    assert healed["resolved"] == 2
    assert {r.status for r in session.scalars(select(OperationsAlert))} == {"resolved"}


def test_health_resolution_never_touches_other_alert_sources(session, tmp_path) -> None:
    from app.operations.monitoring import AlertDraft
    from app.operations.policy import get_policy
    from app.services.operations_alert_service import OperationsAlertService

    _healthy_state(session, _NOW)
    OperationsAlertService(session, policy=get_policy(), notifiers=[]).record_and_notify(
        [AlertDraft(alert_type="DATA_STALE", severity="warning", source="gsc",
                    title="stale", summary="stale", fingerprint="other-source")], now=_NOW)
    _service(session, tmp_path).sync_alerts(now=_NOW, notifiers=[])
    assert session.scalars(select(OperationsAlert)).one().status == "open"


def test_monitoring_hook_is_opt_in_and_never_breaks_monitoring(session) -> None:
    from app.services.operations_monitoring_service import OperationsMonitoringService

    class _Broken:
        def sync_alerts(self, **_kwargs):
            raise RuntimeError("probe failed")

    service = OperationsMonitoringService(session, settings=None, notifiers=[],
                                          system_health=_Broken())
    assert "probe failed" in service._sync_system_health(now=_NOW, notifiers=[])["error"]
    plain = OperationsMonitoringService(session, settings=None, notifiers=[])
    assert plain._sync_system_health(now=_NOW, notifiers=[]) is None


def test_system_status_cli_is_read_only(session, tmp_path, capsys) -> None:
    from scripts import system_status

    _healthy_state(session, _NOW)

    def factory():
        from sqlalchemy.orm import sessionmaker

        return sessionmaker(bind=session.get_bind(), expire_on_commit=False)()

    before = session.scalar(select(func.count()).select_from(OperationsAlert))
    assert system_status.main(
        ["alerts"], session_factory=factory, settings=dummy_google_ads_settings(),
        health_factory=lambda s, settings: _service(s, tmp_path)) == 0
    out = capsys.readouterr().out
    assert "open alerts (0)" in out and "database writes = 0" in out
    assert session.scalar(select(func.count()).select_from(OperationsAlert)) == before


def test_growth_digest_sending_stays_disabled() -> None:
    import json
    from pathlib import Path

    policy = json.loads((Path(__file__).resolve().parents[2] / "app" / "config"
                         / "growth_action_policy.json").read_text(encoding="utf-8"))
    assert policy["digest"]["sending_enabled"] is False


def test_pricing_markers_match_whole_ascii_tokens_only() -> None:
    from app.content.content_types import PRICING, role_from_marker

    assert role_from_marker("Notion 料金 いくら") == (PRICING, "いくら")
    assert role_from_marker("Zapier pricing") == (PRICING, "pricing")
    assert role_from_marker("Make Plans 比較")[0] == PRICING
    assert role_from_marker("planner アプリ")[0] != PRICING  # "plan" は語の一部では当てない


def test_a_declared_pending_migration_is_info_not_an_alert(session, tmp_path) -> None:
    from app.project_state.local_state import PENDING_PRODUCTION_MIGRATIONS

    _healthy_state(session, _NOW)
    pending = next(iter(PENDING_PRODUCTION_MIGRATIONS))
    service = _service(session, tmp_path, schema=({"c1d0e233e180"}, {pending}))
    result = service.evaluate(now=_NOW)
    checks = {f["check"]: f for f in result["findings"]}
    assert checks["schema_pending_declared"]["actionable"] is False
    assert "schema_mismatch" not in checks and result["healthy"]
    assert result["summary"]["db"]["pending"] == [pending]
