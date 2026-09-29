"""記事 (と切り口) を指定した生成の依頼を、在庫の保守の計画が使う規則 (C9-B、pure)。

pin する契約:

- 指定の依頼が無ければ、計画は今までと同じ。
- 使うのは、在庫の規則で **もともと生成するとき** だけ。1 回の上限 (3)・1 記事 1 本・答え待ちの
  依頼の抑止・トピックの散らし・切り口の重なりの規則はそのまま (依頼の数は増えない)。
- 指定の記事に使える在庫がある・休みの期間・固定した切り口が最近使われた、なら使わずに待つ。
- 別の切り口の依頼は、固定した切り口で依頼する。通常の依頼は、在庫の規則で切り口を選ぶ。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from app.social.threads.stock import TargetedFact, plan_stock
from tests.unit.test_threads_stock import _NOW, _POLICY, _article, _facts, _proposal


def _plan(targeted=(), *args, **kwargs):
    return plan_stock(replace(_facts(*args, **kwargs), targeted=tuple(targeted)), _POLICY)


def test_without_targeted_requests_the_plan_is_unchanged() -> None:
    assert _plan().as_dict() == plan_stock(_facts(), _POLICY).as_dict()


def test_a_targeted_article_replaces_one_fairness_pick_within_the_cap() -> None:
    baseline = [r.article_id for r in _plan().requests]
    plan = _plan([TargetedFact(request_id=9, article_id=7)])
    ids = [r.article_id for r in plan.requests]
    assert 7 not in baseline and ids[0] == 7
    assert plan.requested_count == len(baseline) == 3  # 数は増えない
    first = plan.requests[0]
    assert first.targeted_request_id == 9
    assert any("targeted by growth handoff request #9" in r for r in first.reasons)
    assert all(r.targeted_request_id is None for r in plan.requests[1:])
    assert first.as_dict()["targeted_request_id"] == 9


def test_a_frozen_alternative_angle_is_requested_as_frozen() -> None:
    plan = _plan([TargetedFact(request_id=3, article_id=5, angle="question",
                               lane="alternative_angle")])  # fmt: skip
    first = plan.requests[0]
    assert (first.article_id, first.angle) == (5, "question")
    assert len({r.angle for r in plan.requests}) == len(plan.requests)  # 重ならない


def test_healthy_stock_does_not_generate_for_a_targeted_request() -> None:
    proposals = [_proposal(1, 1, angle="insight"), _proposal(2, 2, angle="question"),
                 _proposal(3, 3, angle="comparison"), _proposal(4, 4, angle="beginner_tip")]
    plan = _plan([TargetedFact(request_id=1, article_id=6)], proposals)
    assert plan.needs_generation is False and plan.requests == ()


def test_a_pending_generation_request_still_blocks() -> None:
    plan = _plan([TargetedFact(request_id=1, article_id=6)], pending=1)
    assert plan.requests == ()


def test_a_targeted_article_with_usable_stock_waits() -> None:
    plan = _plan([TargetedFact(request_id=1, article_id=1)], [_proposal(1, 1)])
    assert 1 not in {r.article_id for r in plan.requests}
    assert plan.skipped_articles["targeted request waiting: article already has usable stock"] == 1


def test_a_targeted_article_in_cooldown_waits() -> None:
    articles = [_article(1, last_used=_NOW - timedelta(days=1)), _article(2), _article(3),
                _article(4)]
    plan = _plan([TargetedFact(request_id=1, article_id=1)], articles=articles)
    assert plan.requests[0].targeted_request_id is None
    assert plan.skipped_articles["targeted request waiting: article in cooldown"] == 1


def test_a_recently_used_frozen_angle_waits() -> None:
    articles = [_article(1, recent=("question",)), _article(2), _article(3), _article(4)]
    plan = _plan([TargetedFact(request_id=1, article_id=1, angle="question",
                               lane="alternative_angle")], articles=articles)  # fmt: skip
    assert all(r.targeted_request_id is None for r in plan.requests)
    assert plan.skipped_articles[
        "targeted request waiting: the frozen angle was used meanwhile"] == 1  # fmt: skip


def test_an_unknown_article_waits() -> None:
    plan = _plan([TargetedFact(request_id=1, article_id=99)])
    assert all(r.targeted_request_id is None for r in plan.requests)
    assert plan.skipped_articles["targeted request waiting: article not eligible"] == 1
