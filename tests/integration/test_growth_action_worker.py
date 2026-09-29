"""C9 の評価を worker に載せる準備 (既定は無効)。

pin する契約:

- 既定は無効 (本番ではまだ動かさない)。方針で有効にしたときだけ登録される。
- heartbeat ごとに重い評価をしない: 軽い点検は 1 時間ごと、重い評価は 24 時間ごとか、
  点検の印が変わったとき (最短間隔より早めない)。
- 書くのは C9 の履歴の表だけ。外 (Threads・WordPress・メール) に触れない。
- 履歴の表が無い DB では、何もせずに理由を返す。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text

from app.models import Article
from app.models.growth_action import GrowthActionCandidate
from app.services.threads_worker_service import ThreadsWorkerService
from app.social.threads.service import ThreadsService
from app.social.threads.worker import SUBSYSTEM_GROWTH_OPPORTUNITY
from tests.integration.test_threads_worker_service import (
    _committed_policy,
    _ExplodingClient,
    _factory,
    _Settings,
)

_NOW = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)


class _Settings2(_Settings):
    search_console_property_uri = "sc-domain:bizfluxlab.com"
    search_console_credentials_file = None
    ga4_property_id = None


def _policy(**section):
    base = _committed_policy()
    worker = dict(base.raw.get("worker") or {})
    subsystems = dict(worker.get("subsystems") or {})
    if section:
        subsystems[SUBSYSTEM_GROWTH_OPPORTUNITY] = section
    worker["subsystems"] = subsystems
    return replace(base, raw={**base.raw, "worker": worker})


def _service(session, **section):
    settings = _Settings2()
    return ThreadsWorkerService(_factory(session), settings=settings,
                                threads_service=ThreadsService(settings,
                                                               client=_ExplodingClient()),
                                policy=_policy(**section))  # fmt: skip


def _article(session):
    session.add(Article(id=1, title="a", slug="a", body="本文", status="published",
                        published_url="https://bizfluxlab.com/a/",
                        published_at=_NOW - timedelta(days=3), monetization_mode="supporting"))
    session.commit()


def _other_tables(session) -> dict:
    names = [r[0] for r in session.execute(text(
        "select name from sqlite_master where type='table'"))]  # fmt: skip
    return {n: session.execute(text(f'select count(*) from "{n}"')).scalar() for n in names
            if not n.startswith("growth_action_")}


def test_the_subsystem_is_disabled_by_default(session) -> None:
    state = _service(session).build_schedule(_NOW).state(SUBSYSTEM_GROWTH_OPPORTUNITY)
    assert (state.enabled, state.next_run_at) == (False, None)
    assert "disabled by policy" in state.disabled_reason
    enabled = _service(session, enabled=True).build_schedule(_NOW)
    assert enabled.state(SUBSYSTEM_GROWTH_OPPORTUNITY).next_run_at == _NOW


def test_heavy_evaluation_runs_on_the_interval_and_writes_only_history(session) -> None:
    _article(session)
    service = _service(session, enabled=True)
    handler = service.handlers()[SUBSYSTEM_GROWTH_OPPORTUNITY]
    before = _other_tables(session)
    first = handler(_NOW)
    assert first.summary["evaluated"] is True and first.summary["trigger"] == "interval"
    assert first.next_run_at == _NOW + timedelta(minutes=60)  # 次は軽い点検
    assert session.scalar(select(func.count()).select_from(GrowthActionCandidate)) > 0
    assert _other_tables(session) == before
    assert first.summary["external_writes"] == 0
    # 5 分後の heartbeat・1 時間後の点検では重い評価をしない (印が同じ)。
    for later in (timedelta(minutes=5), timedelta(hours=1), timedelta(hours=23)):
        result = handler(_NOW + later)
        assert result.summary["evaluated"] is False
        assert result.summary["reason"] == "signature unchanged"
    assert handler(_NOW + timedelta(hours=24)).summary["evaluated"] is True


def test_a_signature_change_pulls_the_evaluation_forward_after_the_minimum(session) -> None:
    _article(session)
    service = _service(session, enabled=True)
    handler = service.handlers()[SUBSYSTEM_GROWTH_OPPORTUNITY]
    handler(_NOW)
    service._growth_signature = "changed-before"  # 取り込み・新しい行で印が変わったのと同じ
    early = handler(_NOW + timedelta(hours=2))
    assert early.summary["evaluated"] is False
    assert "waiting for the minimum interval" in early.summary["reason"]
    later = handler(_NOW + timedelta(hours=7))
    assert later.summary["evaluated"] is True and later.summary["trigger"] == "signature_changed"


def test_missing_history_tables_do_nothing(session, monkeypatch) -> None:
    from app.services import growth_action_service as gas

    monkeypatch.setattr(gas, "history_tables_ready", lambda _s: False)
    result = _service(session, enabled=True).handlers()[SUBSYSTEM_GROWTH_OPPORTUNITY](_NOW)
    assert result.summary["evaluated"] is False and "74bfaf6c9c9f" in result.summary["reason"]
    assert result.next_run_at == _NOW + timedelta(minutes=1440)
