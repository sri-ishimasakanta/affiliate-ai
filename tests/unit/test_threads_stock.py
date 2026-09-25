"""投稿案の在庫の保守の計画 (T6、pure)。

pin する契約:

- 使える在庫 = prepared + requested + approved_unpublished + scheduled。held / expired /
  stale は数えない。下限・上限は T4.2 の目安 (3 / 15) で、ノルマではない。
- 足りなければ足りない分だけ。1 回の上限は 3 (ポリシーでも 3 を超えられない)。
- 答えを待つ依頼があれば新しく出さない。
- 同じ記事を掘り続けない・どの記事も永久に後回しにしない・トピックを散らす。
- 切り口はこの回の中で重ならず、少ないものから。T5.5 の参考は同数のときだけ。
- リンク付きは多くても 1 回に 1 本、割合が低いときだけ。
- 計画は何も変えない。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.social.threads.guidance import Preference, ThreadsGenerationGuidance
from app.social.threads.policy import get_operations_policy, load_operations_policy
from app.social.threads.stock import (
    STATE_APPROVED_UNPUBLISHED,
    STATE_EXPIRED,
    STATE_HELD,
    STATE_PREPARED,
    STATE_PUBLISHED,
    STATE_REQUESTED,
    STATE_SCHEDULED,
    STATE_STALE,
    ArticleFact,
    ProposalFact,
    StockFacts,
    plan_stock,
    request_id,
)
from app.social.threads.style import POST_ANGLES

_NOW = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)
_POLICY = get_operations_policy()


def _article(aid, topic=None, *, last_used=None, has_url=True, recent=()):
    return ArticleFact(
        article_id=aid,
        title=f"記事{aid}",
        topic=topic if topic is not None else f"topic{aid}",
        has_url=has_url,
        last_used_at=last_used,
        recent_angles=frozenset(recent),
    )


def _proposal(pid, aid, *, state=STATE_PREPARED, angle="insight", link="none", topic=None, age=1):
    return ProposalFact(
        proposal_id=pid,
        article_id=aid,
        angle=angle,
        link_mode=link,
        state=state,
        created_at=_NOW - timedelta(days=age),
        topic=topic if topic is not None else f"topic{aid}",
    )


def _facts(proposals=(), articles=None, *, recent=(), pending=0):
    return StockFacts(
        now=_NOW,
        proposals=tuple(proposals),
        articles=tuple(articles if articles is not None else [_article(i) for i in range(1, 8)]),
        recent_publications=tuple(recent),
        pending_generation_requests=pending,
    )


def _plan(*args, guidance=None, policy=_POLICY, **kwargs):
    return plan_stock(_facts(*args, **kwargs), policy, guidance=guidance)


# == stock ======================================================================
def test_the_targets_are_the_t42_advisory_stock() -> None:
    assert (_POLICY.stock_target_low, _POLICY.stock_target_high) == (3, 15)
    assert _POLICY.max_new_proposals_per_cycle == 3
    assert _plan().as_dict()["advisory"] is True


def test_empty_stock_needs_generation_bounded_to_three() -> None:
    plan = _plan()
    assert plan.needs_generation is True
    assert plan.usable == 0
    assert plan.requested_count == 3
    assert "usable stock 0 is below the advisory floor 3" in plan.reasons
    assert len({r.article_id for r in plan.requests}) == 3  # 1 記事 1 本
    assert len({r.angle for r in plan.requests}) == 3


def test_healthy_stock_does_not_generate() -> None:
    proposals = [
        _proposal(1, 1, angle="insight"),
        _proposal(2, 2, state=STATE_REQUESTED, angle="question"),
        _proposal(3, 3, state=STATE_APPROVED_UNPUBLISHED, angle="comparison"),
        _proposal(4, 4, state=STATE_SCHEDULED, angle="beginner_tip"),
    ]
    plan = _plan(proposals)
    assert plan.usable == 4
    assert plan.needs_generation is False
    assert plan.requested_count == 0
    assert any("within the advisory range 3-15" in r for r in plan.reasons)
    assert "1 approval request(s) are outstanding" in plan.reasons


def test_every_state_is_counted_and_only_usable_states_count_as_stock() -> None:
    proposals = [
        _proposal(1, 1),
        _proposal(2, 2, state=STATE_HELD),
        _proposal(3, 3, state=STATE_EXPIRED),
        _proposal(4, 4, state=STATE_STALE),
        _proposal(5, 5, state=STATE_HELD),
        _proposal(6, 1, state=STATE_PUBLISHED),
    ]
    plan = _plan(proposals)
    assert plan.counts[STATE_PREPARED] == 1
    assert plan.counts[STATE_HELD] == 2
    assert plan.counts[STATE_EXPIRED] == 1
    assert plan.counts[STATE_STALE] == 1
    assert plan.counts[STATE_PUBLISHED] == 1
    assert plan.usable == 1
    assert plan.requested_count == 2  # 3 - 1


def test_at_or_above_the_ceiling_nothing_is_generated() -> None:
    proposals = [_proposal(i, i, angle=POST_ANGLES[i % 5]) for i in range(1, 16)]
    articles = [_article(i) for i in range(1, 20)]
    plan = _plan(proposals, articles)
    assert plan.usable == 15
    assert plan.needs_generation is False
    assert "usable stock 15 is at or above the advisory ceiling 15" in plan.reasons


def test_a_topic_monoculture_inside_the_range_asks_for_one_more() -> None:
    proposals = [_proposal(i, 1, angle=POST_ANGLES[i]) for i in range(4)]
    plan = _plan(proposals)
    assert plan.needs_generation is True
    assert plan.requested_count == 1
    assert any("only one article/topic is represented" in r for r in plan.reasons)
    assert plan.requests[0].article_id != 1


def test_the_per_cycle_bound_cannot_exceed_three(tmp_path: Path) -> None:
    document = dict(get_operations_policy().raw)
    document["proposal_stock"] = {**document["proposal_stock"], "max_new_proposals_per_cycle": 4}
    path = tmp_path / "p.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="max_new_proposals_per_cycle"):
        load_operations_policy(path)
    document["proposal_stock"]["max_new_proposals_per_cycle"] = 1
    path.write_text(json.dumps(document), encoding="utf-8")
    assert _plan(policy=load_operations_policy(path)).requested_count == 1


def test_a_pending_generation_request_blocks_new_requests() -> None:
    plan = _plan(pending=2)
    assert plan.needs_generation is True
    assert plan.requested_count == 0
    assert "still waiting for output" in plan.blocked_by[0]


# == source selection ===========================================================
def test_articles_with_usable_stock_are_not_mined_again() -> None:
    plan = _plan([_proposal(1, 1)], [_article(1), _article(2), _article(3)])
    assert 1 not in {r.article_id for r in plan.requests}
    assert plan.skipped_articles["already has usable stock"] == 1


def test_never_used_articles_come_first_and_recent_ones_last() -> None:
    articles = [
        _article(1, last_used=_NOW - timedelta(days=1)),  # クールダウン中
        _article(2, last_used=_NOW - timedelta(days=30)),
        _article(3),
        _article(4),
    ]
    order = [r.article_id for r in _plan(articles=articles).requests]
    assert order == [3, 4, 2]


def test_rotation_never_starves_an_article() -> None:
    articles = {i: _article(i) for i in range(1, 8)}
    chosen: list[int] = []
    now = _NOW
    for _cycle in range(3):
        plan = plan_stock(
            StockFacts(now=now, proposals=(), articles=tuple(articles.values())), _POLICY
        )
        for request in plan.requests:
            chosen.append(request.article_id)
            articles[request.article_id] = _article(request.article_id, last_used=now)
        now += timedelta(days=1)
    assert sorted(set(chosen)) == list(range(1, 8))  # 7 記事すべてが順に選ばれる
    assert len(set(chosen[:7])) == 7  # 最初の一巡で重複しない


def test_a_cycle_spreads_across_topics_first() -> None:
    articles = [_article(1, "ai"), _article(2, "ai"), _article(3, "make"), _article(4, "seo")]
    topics = [r.topic for r in _plan(articles=articles).requests]
    assert sorted(topics) == ["ai", "make", "seo"]


# == angle / link diversity =====================================================
def test_angles_are_distinct_and_the_least_represented_come_first() -> None:
    proposals = [
        _proposal(10, 10, angle="insight"),
        _proposal(11, 11, angle="comparison"),
    ]
    articles = [_article(i) for i in range(1, 12)]
    plan = _plan(proposals, articles, recent=[("question", "none")])
    angles = [r.angle for r in plan.requests]
    assert len(set(angles)) == len(angles)
    # 使える在庫 2 → 足りないのは 1 本。在庫にも最近にも無い切り口から (表の順)。
    assert angles == ["common_mistake"]


def test_an_article_does_not_repeat_its_recent_angle() -> None:
    articles = [_article(1, recent=("insight", "common_mistake"))]
    plan = _plan(articles=articles)
    assert plan.requests[0].angle not in ("insight", "common_mistake")


def _prefer(dimension, value, direction="prefer"):
    return ThreadsGenerationGuidance(
        as_of=_NOW.isoformat(),
        as_of_local="",
        source_schema="threads-learning/1",
        source_policy_version="t5.0",
        evidence_status="sufficient_evidence",
        preferences=(Preference(dimension=dimension, value=value, direction=direction),),
    )


def test_a_weak_angle_preference_is_only_a_tie_break_and_cannot_collapse_the_batch() -> None:
    neutral = [r.angle for r in _plan().requests]
    weak = [r.angle for r in _plan(guidance=_prefer("angle", "beginner_tip")).requests]
    assert weak[0] == "beginner_tip"  # 同数のときだけ前へ
    assert len(set(weak)) == 3 and len(set(neutral)) == 3
    # 既に多い切り口は、参考があっても選ばれない (多様性が学習より強い)。
    proposals = [_proposal(10 + i, 10 + i, angle="beginner_tip") for i in range(2)]
    articles = [_article(i) for i in range(1, 14)]
    plan = _plan(proposals, articles, guidance=_prefer("angle", "beginner_tip"))
    assert "beginner_tip" not in {r.angle for r in plan.requests}


def test_neutral_guidance_changes_nothing() -> None:
    neutral = ThreadsGenerationGuidance(
        as_of=_NOW.isoformat(),
        as_of_local="",
        source_schema="threads-learning/1",
        source_policy_version="t5.0",
        evidence_status="insufficient_sample",
    )
    assert _plan(guidance=neutral).as_dict() == _plan().as_dict()


def test_at_most_one_article_link_per_cycle_and_only_while_links_are_rare() -> None:
    plan = _plan()
    assert [r.link_mode for r in plan.requests].count("article") == 1
    linky = [("insight", "article"), ("question", "article"), ("comparison", "none")]
    assert {r.link_mode for r in _plan(recent=linky).requests} == {"none"}
    no_url = [_article(i, has_url=False) for i in range(1, 5)]
    assert {r.link_mode for r in _plan(articles=no_url).requests} == {"none"}


# == review / determinism ========================================================
def test_old_or_unhealthy_stock_is_reported_for_re_review_only() -> None:
    proposals = (
        _proposal(1, 1, age=20),
        _proposal(2, 2, state=STATE_HELD),
        _proposal(3, 3, state=STATE_EXPIRED),
        _proposal(4, 4, age=2),
    )
    plan = _plan(proposals)
    reviewed = {r["proposal_id"]: r["reason"] for r in plan.re_review}
    assert set(reviewed) == {1, 2, 3}
    assert "not expired" in reviewed[1]


def test_the_plan_and_request_ids_are_deterministic() -> None:
    assert _plan().as_dict() == _plan().as_dict()
    kwargs = dict(article_id=1, angle="insight", link_mode="none", fingerprint="f" * 64, as_of=_NOW)
    assert request_id(**kwargs) == request_id(**kwargs)
    assert request_id(**kwargs) != request_id(**{**kwargs, "angle": "question"})
