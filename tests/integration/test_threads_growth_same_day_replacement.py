"""人が却下したその日の Growth Post を、人が許して 1 回だけ差し替える (2026-10-01)。偽の Luna だけ。

- 管理用 CLI の ``--allow-same-day-growth-retry <今日>`` だけが使える (worker は使わない)
- 差し替えられるのは **人が却下した** その日の提案だけ。承認待ち・承認済みは差し替えない
- 却下された提案とその記録は変えない。前の記録は写しを残し、前の呼び出しも上限 4 回に数える
- 新しい提案も承認待ち (承認・公開はしない)。その日 1 回だけ (差し替えも却下されたら、もうしない)
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.models import ThreadsPostProposal
from app.services.threads_growth_service import ThreadsGrowthService
from tests.integration.test_threads_growth_diversity import _yesterday
from tests.integration.test_threads_growth_posts import (
    BODY_C,
    DAY,
    JST,
    MORNING,
    FakeThreads,
    GrowthLuna,
    _client,
    _growth,
    _growth_rows,
    _no_side_effects,
    _record,
)

LATER = MORNING + timedelta(hours=3)
BODY_P = (
    "インサイト祭りに参加します🙌 まずはフォロワー100人が目標です。AI自動化・Web運用を実際に"
    "試しながら発信しています。同じように目標に向かって発信している方と、ぜひつながりたいです。"
    "フォローもコメントも歓迎です。気になった方の投稿は、こちらからも見に行きます！"
)


def _operator(session, tmp_path, fake, *, family="participation"):
    return ThreadsGrowthService(session, timezone=JST, client=_client(fake),
                                threads_service=FakeThreads(None),
                                directory=tmp_path / "growth", same_day_retry=DAY,
                                growth_family=family)  # fmt: skip


def _today(session, tmp_path, *, reject=True) -> ThreadsPostProposal:
    """昨日の 1 本 + 今朝の worker の 1 本 (人が却下した形にする)。"""

    _yesterday(session, tmp_path)
    out = _growth(session, tmp_path, GrowthLuna([BODY_C])).maintain(now=MORNING, execute=True)
    row = session.get(ThreadsPostProposal, out["created"])
    if reject:
        row.status = "rejected"
        row.status_reason = "decided on mobile (rejected)"
        session.commit()
    return row


def _snapshot(row: ThreadsPostProposal) -> tuple:
    return (row.status, row.status_reason, row.content_text, row.approved_at,
            row.learning_guidance_json, row.not_before, row.expires_at)  # fmt: skip


def test_the_worker_never_replaces_a_rejected_proposal(session, tmp_path) -> None:
    rejected = _today(session, tmp_path)
    worker = GrowthLuna([BODY_P])
    out = _growth(session, tmp_path, worker).maintain(now=LATER, execute=True)
    assert worker.calls == 0 and f"already exists (proposal {rejected.id})" in out["reason"]


def test_the_operator_replaces_a_rejected_proposal_once(session, tmp_path) -> None:
    rejected = _today(session, tmp_path)
    before = _snapshot(rejected)
    original = (tmp_path / "growth" / f"{DAY.isoformat()}.openai.json").read_bytes()
    plan = _operator(session, tmp_path, GrowthLuna([])).plan(now=LATER)
    assert plan["due"] is True
    assert plan["same_day_retry"]["replaces_rejected_proposal"] == rejected.id
    fake = GrowthLuna([BODY_P])
    out = _operator(session, tmp_path, fake).maintain(now=LATER, execute=True)
    assert fake.calls == 1 and out["created"] and out["created"] != rejected.id
    row = session.get(ThreadsPostProposal, out["created"])
    assert row.status == "awaiting_approval" and row.approved_at is None
    meta = row.learning_guidance_json["growth"]
    assert meta["replaces_rejected_proposal"] == rejected.id
    assert meta["strategy"]["family"] == "participation"  # 人が選んだ書き方
    assert meta["model_call_index"] == 2  # 前の 1 回も数える
    session.refresh(rejected)
    assert _snapshot(rejected) == before  # 却下された提案とその記録は変えない
    record = _record(tmp_path)
    assert record["outcome"] == "stored" and record["proposal_id"] == row.id
    override = record["override"]
    assert override["mode"] == "replace_human_rejected"
    assert override["replaced_proposal"] == rejected.id
    assert (override["previous_calls"], override["starting_remaining_budget"]) == (1, 3)
    assert override["previous_proposal_id"] == rejected.id
    first, second = record["history"]
    assert first["superseded"] == {"reason": "human_rejected", "proposal_id": rejected.id}
    assert first["next_action"]["retry_reason"] == "human_rejected"
    assert second["override"] == "human_authorized_same_day_retry"
    assert "人が却下した" in second["retry_direction"]
    copy = tmp_path / "growth" / f"{DAY.isoformat()}.replaced-{rejected.id}.openai.json"
    assert copy.read_bytes() == original  # 前の記録はそのままの写し
    _no_side_effects(session)
    # もう作らない: やり直しをもう一度でも、worker でも。
    again = GrowthLuna([BODY_C])
    out2 = _operator(session, tmp_path, again).maintain(now=LATER + timedelta(hours=1),
                                                        execute=True)  # fmt: skip
    _growth(session, tmp_path, again).maintain(now=LATER + timedelta(hours=2), execute=True)
    assert again.calls == 0 and f"already exists (proposal {row.id})" in out2["reason"]
    assert len(_growth_rows(session)) == 3  # 昨日 + 却下 + 差し替え


def test_a_rejected_replacement_is_not_replaced_again(session, tmp_path) -> None:
    _today(session, tmp_path)
    out = _operator(session, tmp_path, GrowthLuna([BODY_P])).maintain(now=LATER, execute=True)
    row = session.get(ThreadsPostProposal, out["created"])
    row.status = "rejected"
    session.commit()
    again = GrowthLuna([BODY_C])
    out2 = _operator(session, tmp_path, again).maintain(now=LATER + timedelta(hours=1),
                                                        execute=True)  # fmt: skip
    assert again.calls == 0 and "already exists" in out2["reason"]
    assert len(_growth_rows(session)) == 3


@pytest.mark.parametrize("status", ["awaiting_approval", "approved"])
def test_the_operator_never_replaces_a_live_proposal(session, tmp_path, status) -> None:
    row = _today(session, tmp_path, reject=False)
    row.status = status
    session.commit()
    fake = GrowthLuna([BODY_P])
    out = _operator(session, tmp_path, fake).maintain(now=LATER, execute=True)
    assert fake.calls == 0 and f"already exists (proposal {row.id})" in out["reason"]
    assert out["same_day_retry"]["replaces_rejected_proposal"] is None


def test_an_unusable_family_falls_back_to_the_normal_order(session, tmp_path) -> None:
    _today(session, tmp_path)
    fake = GrowthLuna([BODY_P])
    out = _operator(session, tmp_path, fake, family="lesson_learned").maintain(
        now=LATER, execute=True)  # fmt: skip
    meta = session.get(ThreadsPostProposal, out["created"]).learning_guidance_json["growth"]
    assert meta["strategy"]["family"] != "lesson_learned"


def test_the_cli_family_needs_the_same_day_retry() -> None:
    from scripts import maintain_threads_growth_post as cli

    with pytest.raises(SystemExit):
        cli.main(["--execute", "--growth-family", "participation"], session_factory=object,
                 settings=object())  # fmt: skip
