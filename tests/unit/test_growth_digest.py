"""Growth Action のまとめの選び方 (C9-A、pure)。

pin する契約:

- 対象は いま動ける (active + actionable_now) で、同じ状態 (同じ指紋) でまだ知らせていないもの。
  止める理由がある・既存の仕事が担う・情報・レビュー中・承認済み・却下・変換済み・置き換え・
  観測されなくなったものは入らない。
- 5 件まで。足りなければ少ないまま (埋めない)。
- 並べ方は C9 の成分の順。選んだ理由・次より前に来た理由を成分の言葉で出す。1 つの点数は無い。
- 多様さ: 種類ごとに一番よいものを先に。候補が少なければ同じ種類が複数でもよい。
- 証拠が変われば通知の指紋が変わる (もう一度知らせてよい)。本文は 1 件ずつのレビューだけ。
"""

from __future__ import annotations

import pytest

from app.growth import digest as gd
from app.growth.inbox import ACTIONABLE_NOW, BLOCKED, COVERED, INFORMATIONAL

_RANK = {"insufficient_data": 0, "structural": 1, "hypothesis": 2, "preliminary": 3}


def _entry(i, action="review_internal_links", *, evidence="structural", potential=1,
           status="active", availability=ACTIONABLE_NOW, fp=None):  # fmt: skip
    levels = {0: "unknown", 1: "low", 2: "medium", 3: "high"}
    return {
        "id": i, "opportunity_key": f"{action}:article:article:{i}", "revision": 1,
        "action_type": action, "subject_id": f"article:{i}", "article_id": i,
        "keyword_id": None, "evidence_state": evidence, "rationale": "r", "blockers": [],
        "candidate_fingerprint": fp or f"fp{i}", "recommendation": {},
        "status": status, "availability": availability, "availability_reasons": ["why"],
        "evidence": [{"reason": "because"}],
        "priority": {"evidence_strength": {"rank": _RANK[evidence], "level": evidence},
                     "potential_opportunity": {"rank": potential, "level": levels[potential]},
                     "monetization_relevance": {"rank": 2, "level": "medium"},
                     "effort": {"rank": 1, "level": "low"},
                     "recency_urgency": {"rank": 1, "level": "low"}},
    }  # fmt: skip


@pytest.mark.parametrize(("status", "availability", "reason"), [
    ("observed", BLOCKED, BLOCKED),
    ("observed", COVERED, COVERED),
    ("observed", INFORMATIONAL, INFORMATIONAL),
    ("observed", "not_observed", "not_observed"),
    ("pending_review", ACTIONABLE_NOW, gd.EXCLUDED_NOT_ACTIVE),
    ("approved", ACTIONABLE_NOW, gd.EXCLUDED_NOT_ACTIVE),
    ("rejected", ACTIONABLE_NOW, gd.EXCLUDED_NOT_ACTIVE),
    ("converted", ACTIONABLE_NOW, gd.EXCLUDED_NOT_ACTIVE),
    ("superseded", ACTIONABLE_NOW, gd.EXCLUDED_NOT_ACTIVE),
    ("dismissed", ACTIONABLE_NOW, gd.EXCLUDED_NOT_ACTIVE),
])  # fmt: skip
def test_ineligible_candidates_are_excluded_with_a_reason(status, availability, reason) -> None:
    plan = gd.select([_entry(1, status=status, availability=availability)])
    assert plan["selected"] == []
    assert plan["excluded"][0]["reason"] == reason


def test_already_notified_same_state_is_excluded_but_new_evidence_is_not() -> None:
    plan = gd.select([_entry(1, fp="same")], notified={"same": 7})
    assert plan["selected"] == [] and plan["excluded"][0]["reason"] == gd.EXCLUDED_ALREADY_NOTIFIED
    assert "delivery #7" in plan["excluded"][0]["detail"]
    again = gd.select([_entry(1, fp="changed")], notified={"same": 7})
    assert [s["id"] for s in again["selected"]] == [1]
    assert gd.notification_fingerprint(_entry(1, fp="same")) != gd.notification_fingerprint(
        _entry(1, fp="changed"))  # fmt: skip


def test_at_most_five_and_fewer_are_not_padded() -> None:
    many = [_entry(i) for i in range(1, 30)]
    assert len(gd.select(many)["selected"]) == 5
    few = gd.select([_entry(1), _entry(2)])
    assert len(few["selected"]) == 2 and few["counts"]["selected"] == 2


def test_diversity_takes_the_best_of_each_type_first() -> None:
    links = [_entry(i, potential=3) for i in range(1, 10)]
    other = [_entry(50, "create_new_article", potential=1),
             _entry(60, "review_affiliate_placement", evidence="hypothesis")]
    plan = gd.select(links + other)
    types = [s["action_type"] for s in plan["selected"]]
    assert set(types) == {"review_internal_links", "create_new_article",
                          "review_affiliate_placement"}
    assert types[0] == "review_affiliate_placement"  # 行動の証拠が先
    adjusted = [s for s in plan["selected"] if s["selection_reason"].startswith("diversity")]
    assert [s["id"] for s in adjusted] == [50]  # 全体の上位 5 件に入らないが種類の一番
    assert plan["diversity"]["adjusted"] == 1


def test_a_single_type_may_fill_the_digest_when_nothing_else_is_eligible() -> None:
    plan = gd.select([_entry(i) for i in range(1, 8)])
    assert [s["action_type"] for s in plan["selected"]] == ["review_internal_links"] * 5


def test_the_ordering_is_explained_without_a_score() -> None:
    plan = gd.select([_entry(1, potential=1), _entry(2, evidence="hypothesis"),
                      _entry(3, potential=3)])  # fmt: skip
    first, second, third = plan["selected"]
    assert first["id"] == 2 and "stronger evidence" in first["why_before_next"]
    assert second["id"] == 3 and "higher opportunity" in second["why_before_next"]
    assert third["why_before_next"] is None
    assert plan["score"] is None and "score" not in first
    assert plan["ordering"][0] == "stronger evidence"


def test_selection_is_deterministic() -> None:
    entries = [_entry(i, potential=i % 3 + 1) for i in range(1, 12)]
    assert gd.select(entries) == gd.select(list(reversed(entries)))
    assert gd.select(entries)["digest_identity"] == gd.select(entries)["digest_identity"]


def test_the_body_is_individual_review_only() -> None:
    plan = gd.select([_entry(1), _entry(2, "create_new_article")])
    body = gd.render_text(plan, tz_label="Asia/Tokyo")
    assert body.count("--fingerprint") == 2  # 1 件ずつ
    assert "一括の承認はありません" in body
    assert "approve-all" not in body and "approve all" not in body.lower()
