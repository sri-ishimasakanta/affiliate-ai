"""app/article/draft_prompt_package.py の pure テスト。

合成 Snapshot payload を使い、builder が Snapshot のみを source とし、
禁止キー (commission 等) を持ち込まず、unknown/not_researched を保持することを検証。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.article.draft_prompt_canonical import compute_prompt_input_hash
from app.article.draft_prompt_package import (
    EditorialOverridesV1,
    assert_no_forbidden_keys,
    build_prompt_package,
)
from app.exceptions import DraftGenerationNotReadyError

NOW = datetime(2026, 9, 1, tzinfo=UTC)
_FACT_KEYS = [
    "official_product_name", "official_url", "category", "primary_use_cases",
    "target_users", "pricing_summary", "free_plan_available", "free_trial_available",
    "key_features", "automation_capabilities", "ai_features", "integrations",
    "japanese_language_support", "japan_business_support", "business_plan_available",
    "security_or_enterprise_notes", "limitations",
]


def _cell(fact_key: str, state: str) -> dict:
    if state == "verified":
        return {
            "fact_key": fact_key,
            "state": "verified",
            "fact_id": 1,
            "affiliate_program_id": 1,
            "value": f"{fact_key}-value",
            "unknown_reason": None,
            "checked_at": "2026-08-28T05:12:00+00:00",
            "fresh": True,
            "claim_allowed": True,
            "source": {
                "source_id": 2,
                "source_type": "official_pricing",
                "source_url": "https://example.com/pricing",
                "source_title": "Pricing",
                "source_checked_at": "2026-08-28T05:12:00+00:00",
            },
        }
    return {
        "fact_key": fact_key,
        "state": state,
        "fact_id": None,
        "affiliate_program_id": None,
        "value": None,
        "unknown_reason": "checked but not conclusive" if state == "unknown" else None,
        "checked_at": None,
        "fresh": None,
        "claim_allowed": False,
        "source": None,
    }


def _tool(subject: str, is_primary: bool) -> dict:
    # 15 verified + 1 unknown (ai_features) + 1 not_researched (japan_business_support)
    cells = []
    for fk in _FACT_KEYS:
        if fk == "ai_features":
            cells.append(_cell(fk, "unknown"))
        elif fk == "japan_business_support":
            cells.append(_cell(fk, "not_researched"))
        else:
            cells.append(_cell(fk, "verified"))
    usable = [c["fact_key"] for c in cells if c["state"] == "verified"]
    do_not = [c["fact_key"] for c in cells if c["state"] != "verified"]
    return {
        "subject_ref": subject,
        "is_primary": is_primary,
        "cells": cells,
        "usable_claims": usable,
        "do_not_claim": do_not,
        "readiness": {"ok": True, "missing_required": [], "stale_required": []},
    }


def _payload() -> dict:
    return {
        "snapshot_version": "draft_input_v1",
        "article": {"id": 1, "keyword_id": 21, "title": "T", "slug": "s"},
        "keyword": {"id": 21, "text": "業務効率化 ツール おすすめ", "category": None},
        "plan": {
            "plan_snapshot_origin": "current_derived__human_confirmed_at_freeze",
            "article_type": "recommendation_roundup",
            "target_reader": "reader",
            "search_intent_summary": "intent",
            "primary_goal": "goal",
            "secondary_goals": ["g2"],
            "outline": [
                {"level": "H2", "heading": "比較", "purpose": "p", "required_elements": []}
            ],
            "comparison_axes": [
                {"axis": "用途・解決できる課題",
                 "data_availability": "future_research_required"},
                {"axis": "料金（月額 / 年額）",
                 "data_availability": "future_research_required"},
                {"axis": "対象ユーザー規模（個人 / 中小 / 法人）",
                 "data_availability": "future_research_required"},
                {"axis": "自動化範囲",
                 "data_availability": "future_research_required"},
                {"axis": "日本語対応・日本法人",
                 "data_availability": "future_research_required"},
                {"axis": "法人契約・請求書払い",
                 "data_availability": "future_research_required"},
                {"axis": "カテゴリ（カタログ分類）", "data_availability": "catalog"},
                {"axis": "提携 ASP / 収益条件（内部判断用・記事非掲載）",
                 "data_availability": "catalog"},
            ],
            "cta_strategy": "cta",
            "cannibalization_guidance": "guide",
            "cannibalization_acknowledgment_required": True,
            "compliance_checklist": ["c1"],
            "quality_guardrails": ["q1"],
            "source_requirements": ["r1"],
        },
        "comparison_set": [
            {
                "article_affiliate_program_id": 1, "affiliate_program_id": 1,
                "program_name": "Make", "provider": "direct", "program_status": "active",
                "commission_type": "percentage", "commission_value": "35.0000",
                "currency": None, "is_primary": True, "planning_role": "primary_candidate",
            },
            {
                "article_affiliate_program_id": 2, "affiliate_program_id": 5,
                "program_name": "HubSpot", "provider": "Impact", "program_status": "active",
                "commission_type": "percentage", "commission_value": "30.0000",
                "currency": None, "is_primary": False, "planning_role": "primary_candidate",
            },
        ],
        "selection": {
            "primary_affiliate_program_id": 1,
            "primary_article_affiliate_program_id": 1,
            "primary_subject_ref": "Make",
            "comparison_program_ids": [1, 5],
            "authority": "human_confirmed_article_affiliate_program.is_primary",
        },
        "tools": [_tool("Make", True), _tool("HubSpot", False)],
        "sources": [
            {"id": 2, "article_id": 1, "source_type": "official_pricing",
             "source_url": "https://example.com/pricing", "title": "Pricing",
             "checked_at": "2026-08-28T05:12:00+00:00"},
        ],
        "policy": {
            "fact_key_order": _FACT_KEYS,
            "required_fact_keys": [], "recommended_fact_keys": [],
            "freshness_policy": {},
        },
        "readiness": {"drafting_allowed": True, "blocking_reasons": [], "warnings": ["w"]},
        "audit": {"built_at": "x", "opportunity_score": 68.81},
    }


def _overrides(**kw) -> EditorialOverridesV1:
    base = dict(
        primary="Make",
        comparison_set_size=2,
        axis_rulings=[
            {"axis": "法人契約・請求書払い", "action": "SOFTEN", "instruction": "SOFTEN する"}
        ],
        japanese_support_ruling={
            "verified_true": ["monday.com"], "unknown": ["Make"],
            "not_researched": ["HubSpot"], "rule": "verified true のみ断定可",
        },
        do_not_assert=["Make/ai_features", "HubSpot/ai_features"],
        commission_to_llm=False,
    )
    base.update(kw)
    return EditorialOverridesV1(**base)


def _build(payload=None, overrides=None, now=NOW):
    return build_prompt_package(
        snapshot_payload=payload or _payload(),
        snapshot_id=1,
        snapshot_content_hash="a" * 64,
        overrides=overrides or _overrides(),
        now=now,
    )


def _iter_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _iter_keys(v)
    elif isinstance(obj, list):
        for it in obj:
            yield from _iter_keys(it)


def test_package_has_no_forbidden_keys() -> None:
    pkg = _build()
    keys = set(_iter_keys(pkg))
    assert "commission_type" not in keys
    assert "commission_value" not in keys
    assert "provider" not in keys
    assert "network" not in keys
    assert "planning_role" not in keys
    assert "tracking_url" not in keys
    assert "landing_page_url" not in keys
    # explicit assert helper も通る
    assert_no_forbidden_keys(pkg)


def test_forbidden_key_injection_raises() -> None:
    pkg = _build()
    pkg["comparison_tools"][0]["commission_value"] = "35.0000"
    with pytest.raises(DraftGenerationNotReadyError):
        assert_no_forbidden_keys(pkg)


def test_usable_unknown_not_researched_split() -> None:
    pkg = _build()
    for tool in pkg["comparison_tools"]:
        assert len(tool["usable_facts"]) == 15
        assert [u["fact_key"] for u in tool["unknown_fact_keys"]] == ["ai_features"]
        assert tool["not_researched_fact_keys"] == ["japan_business_support"]
        assert set(tool["forbidden_fact_keys"]) == {
            "ai_features", "japan_business_support"
        }


def test_unknown_carries_allowed_phrasing_and_do_not_assert_flag() -> None:
    pkg = _build()
    make = next(t for t in pkg["comparison_tools"] if t["subject_ref"] == "Make")
    entry = make["unknown_fact_keys"][0]
    assert "確認できませんでした" in entry["allowed_phrasing"]
    assert "断定禁止" in entry["allowed_phrasing"]  # do_not_assert に入れているため


def test_article_and_editorial_overrides_carried() -> None:
    pkg = _build()
    assert pkg["article"]["title"] == "T"
    assert pkg["article"]["keyword"] == "業務効率化 ツール おすすめ"
    ov = pkg["editorial_overrides"]
    assert ov["commission_to_llm"] is False
    assert ov["axis_rulings"][0]["action"] == "SOFTEN"
    assert ov["japanese_support_ruling"]["verified_true"] == ["monday.com"]
    assert pkg["primary"]["subject_ref"] == "Make"


def test_hash_deterministic_across_built_at() -> None:
    h1 = compute_prompt_input_hash(_build(now=NOW))
    h2 = compute_prompt_input_hash(
        _build(now=datetime(2027, 5, 5, tzinfo=UTC))
    )
    assert h1 == h2


def test_hash_changes_on_override_change() -> None:
    h1 = compute_prompt_input_hash(_build())
    h2 = compute_prompt_input_hash(_build(overrides=_overrides(commission_to_llm=True)))
    assert h1 != h2


def test_live_snapshot_payload_is_only_source() -> None:
    # payload の tools を書き換えれば package も変わる = Snapshot が唯一の source
    p = _payload()
    h1 = compute_prompt_input_hash(_build(payload=p))
    p2 = _payload()
    p2["tools"][0]["cells"][0]["value"] = "CHANGED"
    h2 = compute_prompt_input_hash(_build(payload=p2))
    assert h1 != h2


def test_pricing_as_of_label_derived() -> None:
    pkg = _build()
    assert pkg["pricing_notice_policy"]["as_of_label"] == "2026年8月時点"


# --- Phase 3C-4C.1: LLM-visible comparison-axis projection -------------------

_HIDDEN_AXIS_LABELS = {
    "提携 ASP / 収益条件（内部判断用・記事非掲載）",
    "法人契約・請求書払い",
    "日本語対応・日本法人",
}


def test_builder_version_is_v2() -> None:
    assert _build()["prompt_builder_version"] == "draft_prompt_builder_v2"


def test_internal_comparison_axes_projected_out() -> None:
    pkg = _build()
    labels = {a["axis"] for a in pkg["plan"]["comparison_axes"]}
    assert labels.isdisjoint(_HIDDEN_AXIS_LABELS)
    # affiliate economics axis は 0 件
    assert not any(
        ("ASP" in a["axis"] or "収益条件" in a["axis"] or "提携" in a["axis"])
        for a in pkg["plan"]["comparison_axes"]
    )


def test_projection_keeps_legitimate_axes_and_order() -> None:
    pkg = _build()
    labels = [a["axis"] for a in pkg["plan"]["comparison_axes"]]
    assert labels == [
        "用途・解決できる課題",
        "料金（月額 / 年額）",
        "対象ユーザー規模（個人 / 中小 / 法人）",  # 「法人」を含むが除外しない
        "自動化範囲",
        "カテゴリ（カタログ分類）",
    ]


def test_projection_does_not_touch_fact_boundary() -> None:
    """日本語対応の generic 軸は消えるが、tool ごとの JLS Fact state は保持される。"""
    pkg = _build()
    for tool in pkg["comparison_tools"]:
        # _payload() の _tool(): 全 fact verified, ai_features=unknown,
        # japan_business_support=not_researched
        assert "japanese_language_support" in [
            u["fact_key"] for u in tool["usable_facts"]
        ]


def test_sensitive_axis_leak_raises() -> None:
    """exact リストに無い affiliate-economics 風軸は hard-fail する。"""
    p = _payload()
    p["plan"]["comparison_axes"].append(
        {"axis": "ASP 別の想定収益条件", "data_availability": "catalog"}
    )
    with pytest.raises(DraftGenerationNotReadyError):
        _build(payload=p)


def test_projection_changes_prompt_input_hash() -> None:
    """hidden 軸を含む payload では projection が hash を変える。

    projection 後の hash は、最初から hidden 軸を除いた payload の hash と一致する
    (projection は決定論的で idempotent)。
    """
    h_projected = compute_prompt_input_hash(_build(payload=_payload()))

    p_pre_filtered = _payload()
    p_pre_filtered["plan"]["comparison_axes"] = [
        a
        for a in p_pre_filtered["plan"]["comparison_axes"]
        if a["axis"] not in _HIDDEN_AXIS_LABELS
    ]
    assert compute_prompt_input_hash(_build(payload=p_pre_filtered)) == h_projected

    # hidden 軸を強制的に残した package と比べれば hash は必ず異なる
    pkg_with_hidden = _build(payload=_payload())
    pkg_with_hidden["plan"]["comparison_axes"] = _payload()["plan"]["comparison_axes"]
    assert compute_prompt_input_hash(pkg_with_hidden) != h_projected


def test_human_override_wording_preserved_verbatim() -> None:
    instruction = (
        "比較表でツール別の yes/no 判定を作らない。「導入時の注意点」で、"
        "「請求書払い・日本法人対応は各社で異なり、本記事では未確認です。"
        "導入前に各社の公式情報を確認してください。」程度の限定表現に留める。"
        "未確認情報を補完・推測しない。"
    )
    rule = (
        "monday.com と Todoist のみ「日本語対応」と断定してよい。Make は"
        "「公式情報では確認できず」まで。HubSpot・ClickUp・Pipedrive・Reclaim.ai は"
        "「本記事では未確認」まで。unknown / not_researched を false・非対応・なしへ"
        "変換しない。"
    )
    ov = _overrides(
        axis_rulings=[
            {"axis": "法人契約・請求書払い", "action": "SOFTEN", "instruction": instruction}
        ],
        japanese_support_ruling={
            "verified_true": ["monday.com", "Todoist"],
            "unknown": ["Make"],
            "not_researched": ["HubSpot", "ClickUp", "Pipedrive", "Reclaim.ai"],
            "rule": rule,
        },
    )
    pkg = _build(overrides=ov)
    stored = pkg["editorial_overrides"]
    assert stored["axis_rulings"][0]["instruction"] == instruction
    assert stored["japanese_support_ruling"]["rule"] == rule
