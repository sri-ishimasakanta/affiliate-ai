"""T6.3.3c: 人が許した同じ日のやり直し (2026-09-29 の 1 回だけの例外の仕組み)。偽の Luna だけ。

- 管理用 CLI の ``--allow-same-day-growth-retry <今日>`` だけが使える (worker は使わない)
- T6.3.3 の形の記録の日だけ。前の試み (2 回の呼び出し・似すぎ 0.543) は消さずに残す
- 前の呼び出しも 1 日の上限 4 回に数える (新しい呼び出しは残りの 2 回まで)
- 検査・承認・1 日 1 本・フォロワーの目標は変わらない
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from app.approval.review_snapshot import build_snapshot
from app.models import ThreadsPostProposal
from app.services.threads_growth_service import ThreadsGrowthService
from tests.integration.test_threads_growth_diversity import FailingLuna, _yesterday
from tests.integration.test_threads_growth_posts import (
    BODY_A,
    BODY_C,
    DAY,
    JST,
    MORNING,
    FakeThreads,
    GrowthLuna,
    _client,
    _growth,
    _growth_rows,
    _record,
)

LATER = MORNING + timedelta(hours=3)


def _call(ordinal: int, purpose: str, score: float) -> dict:
    audit = {"similarity": {"max_similarity": score, "threshold": 0.5, "blocked": True,
                            "blocked_by": "proposal #1",
                            "top": [{"ref": "proposal #1", "similarity": score}]}}  # fmt: skip
    return {"ordinal": ordinal, "purpose": purpose, "at": "2026-09-27T22:00:10+00:00",
            "result": "ok", "usage": {"input_tokens": 800},
            "validation": {"ok": False, "reason_ids": ["growth_duplicate"],
                           "reasons": "too similar to the recent growth post proposal #1",
                           "audit": audit}}  # fmt: skip


def _legacy_day(tmp_path) -> tuple[Path, bytes]:
    """本番の 2026-09-29 と同じ形の T6.3.3 の記録 (2 回呼んで、似すぎて提案なし)。"""

    legacy = {"request_id": f"growth-{DAY}", "content_kind": "account_growth",
              "brief": {"date_jst": DAY.isoformat(), "angle": "goal_progress"},
              "result": "rejected_by_validation", "model_calls": 2, "repair_calls": 1,
              "reason": "growth check failed: too similar to the recent growth post "
                        "proposal #1 (0.543)",
              "history": [_call(1, "initial", 0.541), _call(2, "repair", 0.543)]}  # fmt: skip
    path = tmp_path / "growth" / f"{DAY.isoformat()}.openai.json"
    path.write_text(json.dumps(legacy, ensure_ascii=False, indent=2), "utf-8")
    return path, path.read_bytes()


def _operator(session, tmp_path, fake, *, day=DAY, followers=None, collect=False):
    return ThreadsGrowthService(session, timezone=JST, client=_client(fake),
                                threads_service=FakeThreads(followers),
                                directory=tmp_path / "growth", collect_followers=collect,
                                same_day_retry=day)  # fmt: skip


def test_the_override_refuses_any_other_day(session, tmp_path) -> None:
    _yesterday(session, tmp_path)
    path, before = _legacy_day(tmp_path)
    fake = GrowthLuna([BODY_C])
    out = _operator(session, tmp_path, fake, day=DAY - timedelta(days=1)).maintain(
        now=LATER, execute=True)  # fmt: skip
    assert fake.calls == 0 and "refused" in out["reason"] and path.read_bytes() == before


def test_the_override_keeps_the_legacy_calls_and_uses_only_the_rest(session, tmp_path) -> None:
    _yesterday(session, tmp_path)  # proposal #1 (昨日)
    path, before = _legacy_day(tmp_path)
    original = json.loads(before)
    plan = _operator(session, tmp_path, GrowthLuna([])).plan(now=LATER)
    assert plan["due"] is True and plan["model_calls_today"] == 2
    fake = GrowthLuna([BODY_A, BODY_A, BODY_A])  # 昨日と同じ言い回し → 2 回とも似すぎ
    out = _operator(session, tmp_path, fake).maintain(now=LATER, execute=True)
    assert fake.calls == 2 and out["created"] is None  # 新しい呼び出しは残りの 2 回だけ
    assert out["outcome"] == "growth_generation_exhausted"
    record = _record(tmp_path)
    assert len(record["history"]) == 4 and record["model_calls"] == 4
    legacy_calls, new_calls = record["history"][:2], record["history"][2:]
    assert all(c["legacy"] for c in legacy_calls)
    assert [c["similarity"]["max"] for c in legacy_calls] == [0.541, 0.543]
    assert legacy_calls[1]["similarity"]["compared"] == "proposal #1"
    assert all(c["override"] == "human_authorized_same_day_retry" for c in new_calls)
    families = [c["strategy"]["family"] for c in new_calls]
    assert "goal_progress" not in families and len(set(families)) == 2
    assert new_calls[0]["purpose"] == "strategy_retry"
    assert "言い換えではなく" in new_calls[0]["retry_direction"]
    override = record["override"]
    assert override["type"] == "human_authorized_same_day_retry"
    assert (override["legacy_calls"], override["starting_remaining_budget"]) == (2, 2)
    assert record["legacy_record"] == original  # 前の試みはそのまま
    copy = tmp_path / "growth" / f"{DAY.isoformat()}.legacy-t633.openai.json"
    assert copy.read_bytes() == before
    # もう一度やり直しても、worker でも、5 回目は呼ばない。
    again = GrowthLuna([BODY_C])
    _operator(session, tmp_path, again).maintain(now=LATER + timedelta(hours=1), execute=True)
    _growth(session, tmp_path, again).maintain(now=LATER + timedelta(hours=2), execute=True)
    assert again.calls == 0 and len(_growth_rows(session)) == 1


def test_the_override_can_create_exactly_one_proposal_for_approval(session, tmp_path) -> None:
    _yesterday(session, tmp_path)
    _legacy_day(tmp_path)
    fake = GrowthLuna([BODY_C, BODY_C])
    out = _operator(session, tmp_path, fake).maintain(now=LATER, execute=True)
    assert fake.calls == 1 and out["created"]
    row = session.get(ThreadsPostProposal, out["created"])
    assert row.status == "awaiting_approval" and row.approved_at is None
    meta = row.learning_guidance_json["growth"]
    assert meta["model_call_index"] == 3 and meta["strategy"]["family"] != "goal_progress"
    snap = build_snapshot(subject_type="threads_post", subject=row, article=None)
    labels = (snap["post_kind_label"], snap["goal_label"], snap["link_mode"], snap["topic_label"])
    assert labels == ("Growth Post", "フォロワー100人", "なし", "インサイト祭り")
    assert "インサイト祭り" not in row.content_text
    assert _record(tmp_path)["outcome"] == "stored"
    later = GrowthLuna([BODY_A])
    _operator(session, tmp_path, later).maintain(now=LATER + timedelta(hours=1), execute=True)
    assert later.calls == 0 and len(_growth_rows(session)) == 2  # 昨日と今日の 1 本ずつ


def test_the_worker_never_continues_an_operator_retry(session, tmp_path) -> None:
    _yesterday(session, tmp_path)
    _legacy_day(tmp_path)
    _operator(session, tmp_path, FailingLuna([503, 503, 503])).maintain(now=LATER, execute=True)
    assert _record(tmp_path)["result"] == "paused"  # 一時的な失敗で止まった
    worker = GrowthLuna([BODY_C])
    out = _growth(session, tmp_path, worker).maintain(now=LATER + timedelta(hours=1),
                                                      execute=True)  # fmt: skip
    assert worker.calls == 0 and "human-authorized same-day retry" in out["reason"]


def test_the_override_never_resets_a_new_style_day(session, tmp_path) -> None:
    _yesterday(session, tmp_path)
    _growth(session, tmp_path, GrowthLuna([BODY_A] * 4)).maintain(now=MORNING, execute=True)
    assert len(_record(tmp_path)["history"]) == 4
    fake = GrowthLuna([BODY_C])
    out = _operator(session, tmp_path, fake).maintain(now=MORNING + timedelta(hours=1),
                                                      execute=True)  # fmt: skip
    assert fake.calls == 0 and "finished" in out["reason"]


def test_the_override_does_not_bypass_the_follower_target(session, tmp_path) -> None:
    _yesterday(session, tmp_path)
    path, before = _legacy_day(tmp_path)
    fake = GrowthLuna([BODY_C])
    out = _operator(session, tmp_path, fake, followers=100, collect=True).maintain(
        now=LATER, execute=True)  # fmt: skip
    assert fake.calls == 0 and out["follower_target_reached"] is True
    assert path.read_bytes() == before  # 記録にも触れない


def test_the_cli_override_needs_execute_and_a_date() -> None:
    from scripts import maintain_threads_growth_post as cli

    with pytest.raises(SystemExit):
        cli.main(["--allow-same-day-growth-retry", "2026-09-29"], session_factory=object,
                 settings=object())  # fmt: skip
    with pytest.raises(SystemExit):
        cli.main(["--execute", "--allow-same-day-growth-retry", "today"],
                 session_factory=object, settings=object())  # fmt: skip
