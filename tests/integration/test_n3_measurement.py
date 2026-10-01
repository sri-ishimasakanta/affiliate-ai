"""N3: note の実測の記録と報告 (2026-10-01)。手元の DB だけ。外に問い合わせない。

pin する契約:

- 測る記事は登録で決める。bootstrap (台帳に無い記事) は ``external-n<12>`` の参照で測れるが、
  登録した記事だけ。台帳の記事 (``draft-``) は今までどおり台帳に要る。承認は作らない。
- 観測の時刻は UTC にそろえて保存し、同じ時刻は別のオフセットで書いても 1 つ。
- 1 回の観測をまとめて記録できる。既定は PLAN。1 つでも拒まれれば何も書かない。
- 報告は観測した点だけ: 累計の前の観測との差・公開からの日数・7 日ごとの観測の予定。
  観測の無い記事は「最初の観測待ち」(0 を入れない)。勝ち負け・因果を出さない。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.models import ManualMetricEntry
from app.n_track import metrics as mm
from app.services.manual_metrics_service import ManualMetricsService, load_targets
from app.services.note_ledger_service import NoteLedgerService
from tests.integration.test_note_ledger import POLICY, _drafts

JST = timezone(timedelta(hours=9))
BASE = datetime(2026, 10, 2, 21, 0, tzinfo=JST)


def _service(session, tmp_path):
    root, _path, draft = _drafts(tmp_path, publish=True)
    NoteLedgerService(session, policy=POLICY).sync_from_drafts(root, execute=True,
                                                               now=BASE)  # fmt: skip
    targets = {"cadence_days": 7, "checkpoints": 4, "window_days": 3, "channels": ["note"],
               "pieces": [
                   {"ref": "external-nd9c4fb635aad", "role": "bootstrap",
                    "url": "https://note.com/u/n/nd9c4fb635aad",
                    "note_published_at": "2026-09-30T11:49:04+09:00", "in_ledger": False},
                   {"ref": draft.id, "role": "controlled-1", "url": "https://note.com/u/n/p1",
                    "note_published_at": "2026-09-30T21:09:01+09:00", "in_ledger": True}]}
    return ManualMetricsService(session, targets=targets), draft


def _entry(ref, metric, value, at, kind="note_piece"):
    return mm.MetricInput(subject_kind=kind, subject_ref=ref, metric=metric, value=value,
                          observed_at=at, source_description="note dashboard (all time)",
                          entered_by="human")  # fmt: skip


def _count(session) -> int:
    return session.scalar(select(func.count()).select_from(ManualMetricEntry))


def test_the_repository_targets_keep_bootstrap_and_controlled_apart() -> None:
    targets = load_targets()
    roles = {p["role"]: p for p in targets["pieces"]}
    assert set(roles) == {"bootstrap", "controlled-1", "controlled-2", "controlled-3", "paid-1"}
    assert roles["paid-1"]["url"] == "https://note.com/ai_growth_jp/n/nfcb70e910346"
    assert roles["bootstrap"]["in_ledger"] is False
    assert roles["bootstrap"]["ref"] == "external-nd9c4fb635aad"
    assert all(p["in_ledger"] for r, p in roles.items() if r != "bootstrap")
    assert roles["controlled-3"]["url"] == "https://note.com/ai_growth_jp/n/n53dfa4d2e2c3"
    assert (targets["cadence_days"], targets["checkpoints"]) == (7, 4)


def test_only_a_registered_external_piece_can_be_measured(session, tmp_path) -> None:
    service, _draft = _service(session, tmp_path)
    assert service.record(_entry("external-nd9c4fb635aad", "views", 30, BASE),
                          execute=True)["recorded"]  # fmt: skip
    with pytest.raises(mm.MetricError, match="not a registered"):
        service.record(_entry("external-n000000000000", "views", 1, BASE))
    with pytest.raises(mm.MetricError, match="not in the ledger"):
        service.record(_entry("draft-ffffffffff", "views", 1, BASE))


def test_times_are_stored_as_utc_and_the_same_instant_is_one_entry(session, tmp_path) -> None:
    service, draft = _service(session, tmp_path)
    service.record(_entry(draft.id, "views", 12, BASE), execute=True)
    row = session.scalars(select(ManualMetricEntry)).one()
    assert row.observed_at.replace(tzinfo=None) == datetime(2026, 10, 2, 12, 0)  # UTC
    same = service.record(_entry(draft.id, "views", 12, BASE.astimezone(UTC)), execute=True)
    assert same == {"recorded": False, "reason": "already recorded", "id": row.id}
    assert service.active_rows()[0]["observed_at"] == BASE  # 読み戻しは同じ時刻


def test_a_snapshot_is_planned_first_and_all_or_nothing(session, tmp_path) -> None:
    service, draft = _service(session, tmp_path)
    good = [_entry(draft.id, "views", 12, BASE), _entry(draft.id, "likes", 3, BASE),
            _entry("note", "followers", 41, BASE, kind="channel")]
    plan = service.record_many(good)
    assert plan["planned"] == 3 and _count(session) == 0
    with pytest.raises(mm.MetricError):
        service.record_many([*good, _entry(draft.id, "views", -1, BASE)], execute=True)
    assert _count(session) == 0  # 1 つでも拒まれれば何も書かない
    assert service.record_many(good, execute=True)["recorded"] == 3


def test_the_report_shows_deltas_days_and_waits_without_zeros(session, tmp_path) -> None:
    service, draft = _service(session, tmp_path)
    empty = service.report(now=BASE)
    assert empty["plan"]["state"] == "waiting_for_baseline"
    assert all(p["metrics"]["views"] == "waiting for the first observation"
               for p in empty["pieces"])
    week = BASE + timedelta(days=7)
    for at, views, followers in ((BASE, 12, 41), (week, 20, 44)):
        service.record_many([_entry(draft.id, "views", views, at),
                             _entry("note", "followers", followers, at, kind="channel")],
                            execute=True)  # fmt: skip
    report = service.report(now=week + timedelta(hours=1))
    controlled = next(p for p in report["pieces"] if p["role"] == "controlled-1")
    first, second = controlled["metrics"]["views"]
    assert "delta" not in first and second["delta"] == 8 and second["days_since_previous"] == 7
    assert first["days_since_publication"] == 2.0
    bootstrap = next(p for p in report["pieces"] if p["role"] == "bootstrap")
    assert bootstrap["metrics"]["views"] == "waiting for the first observation"
    assert report["channels"]["note"]["followers"][1]["delta"] == 3
    states = [c["state"] for c in report["plan"]["checkpoints"]]
    assert states == ["observed", "observed", "upcoming", "upcoming", "upcoming"]
    assert report["summaries"]["views"]["small_sample"] is True
    assert "no estimate" in report["reading"]


def test_a_running_total_that_goes_down_is_flagged() -> None:
    rows = [{"id": i, "subject_kind": "note_piece", "subject_ref": "draft-aaaaaa",
             "metric": "views", "value": v, "period_start": None, "period_end": None,
             "observed_at": BASE + timedelta(days=7 * i)} for i, v in enumerate((10, 8))]
    series = mm.timeline(rows, kind="note_piece", ref="draft-aaaaaa", metric="views")
    assert series[1]["delta"] == -2 and "went down" in series[1]["warning"]


def test_checkpoints_mark_missed_and_due_windows() -> None:
    plan = mm.checkpoints([BASE], now=BASE + timedelta(days=15))
    states = {c["label"]: c["state"] for c in plan["checkpoints"]}
    assert states == {"baseline": "observed", "week 1": "missed", "week 2": "due",
                      "week 3": "upcoming", "week 4": "upcoming"}
    assert plan["checkpoints"][4]["target"] == "2026-10-30"


def test_the_cli_snapshot_and_report(session, tmp_path, capsys) -> None:
    from scripts.record_manual_metric import main

    _service(session, tmp_path)

    class _Factory:
        def __call__(self):
            return self

        def __enter__(self):
            return session

        def __exit__(self, *exc):
            return False

    args = ["record-snapshot", "--observed-at", "2026-10-02T21:00:00+09:00",
            "--source", "note dashboard (all time)", "--by", "human",
            "--entry", "external-nd9c4fb635aad:views=30", "--entry", "note:followers=41"]
    assert main(args, session_factory=_Factory(), now=BASE) == 0
    assert '"planned": 2' in capsys.readouterr().out and _count(session) == 0
    assert main([*args, "--execute"], session_factory=_Factory(), now=BASE) == 0
    capsys.readouterr()
    assert _count(session) == 2
    assert main(["record-snapshot", "--observed-at", "2026-10-02T21:00:00+09:00", "--source",
                 "note dashboard", "--by", "human", "--entry", "bad-entry"],
                session_factory=_Factory(), now=BASE) == 2
    assert "REF:METRIC=VALUE" in capsys.readouterr().out
    assert main(["report"], session_factory=_Factory(), now=BASE) == 0
    assert '"baseline"' in capsys.readouterr().out
