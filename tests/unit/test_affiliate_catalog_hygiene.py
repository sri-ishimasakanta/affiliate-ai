"""app/affiliate/catalog_hygiene.py — 宣言的な match_terms 削除 (pure)。

catalog の正本は production DB で version 管理された source が無いため、削除を config に宣言して
PLAN / EXECUTE で適用する。ここでは config の検証・追跡 config の範囲 (scope) と不変条件を固定する。
"""

from __future__ import annotations

import copy

import pytest

from app.affiliate.catalog_hygiene import (
    DEFAULT_HYGIENE_PATH,
    STATUS_APPLIED,
    STATUS_DRIFT,
    STATUS_NOT_FOUND,
    STATUS_PENDING,
    HygieneConfigError,
    TermRemoval,
    evaluate_change,
    load_hygiene_config,
    parse_hygiene_config,
)
from app.keyword.affiliate_matching import ProgramFacts
from app.keyword.affiliate_tiers import (
    TIER_STRONG,
    default_tier_config,
    is_own_name_term,
    match_programs_tiered,
)

_SPEC = load_hygiene_config()

# C2.5.5 で削除を承認した term だけ (これ以外を外す変更は scope 外)。
_APPROVED_REMOVALS = {
    "Make": {"自動化", "業務効率化"},
    "n8n Cloud": {"自動化"},
    "Writesonic": {"生成AI"},
    "Descript": {"議事録", "文字起こし", "AI 文字起こし", "音声文字起こし"},
    "HubSpot": {"業務効率化"},
    "Pipedrive": {"業務効率化"},
    "ClickUp": {"業務効率化"},
    "monday.com": {"業務効率化"},
    "Todoist": {"業務効率化"},
    "Reclaim.ai": {"業務効率化"},
    "ActiveCampaign": {"CRM", "顧客管理"},
}

# 残すべき core terms (削除してはいけない)。
_MUST_KEEP = {
    "Make": {"Make", "業務自動化", "SaaS連携"},
    "n8n Cloud": {"n8n", "業務自動化"},
    "Writesonic": {
        "Writesonic",
        "AIライティング",
        "AI ライティング",
        "文章生成AI",
        "記事作成 AI",
        "AI 記事作成",
    },
    "Descript": {"Descript", "動画編集 AI", "ポッドキャスト 編集"},
    "ActiveCampaign": {"ActiveCampaign", "マーケティング自動化", "メール自動化", "MAツール"},
    "HubSpot": {"HubSpot", "CRM", "顧客管理", "マーケティング自動化"},
    "Pipedrive": {"Pipedrive", "CRM", "SFA", "営業支援", "顧客管理"},
    "ClickUp": {"ClickUp", "タスク管理", "プロジェクト管理"},
    "monday.com": {"monday.com", "monday", "プロジェクト管理", "タスク管理"},
    "Todoist": {"Todoist", "タスク管理"},
    "Reclaim.ai": {
        "Reclaim.ai",
        "Reclaim",
        "AI スケジュール",
        "スケジュール 自動化",
        "カレンダー 自動化",
    },
}


def _raw(**over):
    change = {
        "id": "c1",
        "program": "Acme",
        "provider": "direct",
        "before": ["Acme", "業務効率化", "タスク管理"],
        "remove": ["業務効率化"],
        "reason": "goal word",
        "evidence": "audit",
    }
    change.update(over)
    return {"version": 1, "changes": [change]}


# ================================================================ tracked config scope
def test_tracked_config_declares_exactly_the_approved_removals() -> None:
    assert len(_SPEC.changes) == 11 and _SPEC.removal_count == 16
    declared = {c.program: set(c.remove) for c in _SPEC.changes}
    assert declared == _APPROVED_REMOVALS


def test_tracked_config_keeps_every_core_and_strong_term() -> None:
    for change in _SPEC.changes:
        must = _MUST_KEEP[change.program]
        assert must <= set(change.after), (change.program, must - set(change.after))
        assert must <= set(change.before)


def test_no_tracked_removal_is_a_strong_term() -> None:
    aliases = default_tier_config()
    for change in _SPEC.changes:
        for term in change.remove:
            assert not is_own_name_term(change.program, term), (change.program, term)
            assert term not in aliases.aliases_for(change.program)


def test_removals_never_change_strong_matches_for_brand_and_alias_queries() -> None:
    def facts(terms_by_program: dict[str, tuple[str, ...]]) -> list[ProgramFacts]:
        return [
            ProgramFacts(i, name, c.provider, None, None, None, None, terms_by_program[name])
            for i, (name, c) in enumerate({c.program: c for c in _SPEC.changes}.items(), start=1)
        ]

    before = facts({c.program: c.before for c in _SPEC.changes})
    after = facts({c.program: c.after for c in _SPEC.changes})
    queries = [
        "make 料金",
        "make.com",
        "n8n",
        "writesonic 料金",
        "descript とは",
        "hubspot 使い方",
        "ハブスポット とは",
        "pipedrive",
        "clickup 料金",
        "monday com",
        "todoist",
        "reclaim ai",
        "activecampaign 比較",
    ]
    for q in queries:
        strong_b = {m.name for m in match_programs_tiered(q, before) if m.tier == TIER_STRONG}
        strong_a = {m.name for m in match_programs_tiered(q, after) if m.tier == TIER_STRONG}
        assert strong_a == strong_b and strong_b, q


def test_after_hygiene_generic_terms_stop_matching_but_category_terms_stay() -> None:
    def matched(keyword: str, use_after: bool) -> set[str]:
        programs = [
            ProgramFacts(
                i, c.program, c.provider, None, None, None, None, c.after if use_after else c.before
            )
            for i, c in enumerate(_SPEC.changes, start=1)
        ]
        return {m.name for m in match_programs_tiered(keyword, programs) if m.legacy_matched}

    assert len(matched("業務効率化 ツール おすすめ", False)) == 7  # 現状: この 1 語だけで 7 program
    assert matched("業務効率化 ツール おすすめ", True) == set()
    assert "Descript" in matched("議事録 作成 ツール", False)
    assert "Descript" not in matched("議事録 作成 ツール", True)
    assert matched("rpa 自動化", True) == set()
    assert "Writesonic" not in matched("生成AI とは", True)
    assert "ActiveCampaign" not in matched("crm おすすめ", True)
    # core / category terms は残る
    assert "Make" in matched("業務自動化 ツール", True) and "n8n Cloud" in matched(
        "業務自動化 ツール", True
    )
    assert "Descript" in matched("ポッドキャスト 編集 ツール", True)
    assert "Writesonic" in matched("ai ライティング おすすめ", True)
    assert "ActiveCampaign" in matched("マーケティング自動化 ツール", True)
    assert {"HubSpot", "Pipedrive"} <= matched("crm おすすめ", True)


def test_default_config_path_is_the_tracked_version_controlled_file() -> None:
    assert DEFAULT_HYGIENE_PATH.name == "affiliate_catalog_hygiene.json"
    assert DEFAULT_HYGIENE_PATH.is_file()


# ================================================================ TermRemoval / evaluate
def test_after_is_derived_from_before_and_keeps_order() -> None:
    change = parse_hygiene_config(_raw()).changes[0]
    assert change.after == ("Acme", "タスク管理")
    assert isinstance(change, TermRemoval)


def test_evaluate_statuses_are_exact() -> None:
    change = parse_hygiene_config(_raw()).changes[0]
    assert evaluate_change(change, ["Acme", "業務効率化", "タスク管理"]) == STATUS_PENDING
    assert evaluate_change(change, ["Acme", "タスク管理"]) == STATUS_APPLIED  # 冪等
    assert evaluate_change(change, None) == STATUS_NOT_FOUND
    for drifted in (
        ["Acme", "業務効率化", "タスク管理", "新語"],  # 追加された
        ["Acme", "タスク管理", "業務効率化"],  # 順序が違う
        ["Acme", "業務効率化 ", "タスク管理"],  # 表記 (空白) が違う
        ["acme", "業務効率化", "タスク管理"],  # 大文字小文字が違う
        [],
        ["Acme"],
    ):
        assert evaluate_change(change, drifted) == STATUS_DRIFT, drifted


# ================================================================ config validation
@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ([], "must be a JSON object"),
        ({"version": 2, "changes": []}, "version must be 1"),
        ({"version": 1, "bogus": 1, "changes": []}, "unknown top-level"),
        ({"version": 1, "changes": []}, "non-empty list"),
        ({"version": 1, "changes": ["x"]}, "must be an object"),
        (_raw(bogus=1), "unknown keys"),
        (_raw(id=""), "id must be a non-empty string"),
        (_raw(program=""), "program must be a non-empty string"),
        (_raw(provider=""), "provider must be a non-empty string or null"),
        (_raw(before=[]), "before must be a non-empty list"),
        (_raw(before=["Acme", "Acme"]), "unique"),
        (_raw(remove=[]), "remove must be a non-empty list"),
        (_raw(remove=["業務効率化", "業務効率化"]), "unique"),
        (_raw(remove=["存在しない語"]), "not in before"),
        (_raw(before=["Acme", "業務効率化"], remove=["Acme", "業務効率化"]), "keep at least one"),
        (_raw(remove=["Acme"]), "cannot remove strong terms"),
        (
            _raw(program="HubSpot", before=["HubSpot", "ハブスポット"], remove=["ハブスポット"]),
            "strong terms",
        ),
        (_raw(reason=""), "reason and evidence"),
        (_raw(evidence=" "), "reason and evidence"),
    ],
)
def test_config_validation(raw, fragment: str) -> None:
    with pytest.raises(HygieneConfigError) as exc:
        parse_hygiene_config(raw)
    assert fragment in str(exc.value)


def test_config_rejects_missing_keys_and_duplicate_programs_and_ids() -> None:
    raw = _raw()
    del raw["changes"][0]["evidence"]
    with pytest.raises(HygieneConfigError, match="missing keys"):
        parse_hygiene_config(raw)
    dup = _raw()
    dup["changes"].append(copy.deepcopy(dup["changes"][0]))
    with pytest.raises(HygieneConfigError) as exc:
        parse_hygiene_config(dup)
    assert "duplicate change id" in str(exc.value) and "more than one change" in str(exc.value)


def test_config_cannot_express_additions_or_rewrites() -> None:
    # remove しか無い: 新しい term を足す / 書き換えるキーは unknown key として拒否される
    for key in ("add", "after", "replace", "set"):
        with pytest.raises(HygieneConfigError, match="unknown keys"):
            parse_hygiene_config(_raw(**{key: ["x"]}))


def test_config_keeps_terms_exactly_without_trimming() -> None:
    change = parse_hygiene_config(
        _raw(before=["Acme", " 業務効率化", "タスク管理"], remove=[" 業務効率化"])
    ).changes[0]
    assert change.remove == (" 業務効率化",)  # 空白も含めて完全一致の対象
    assert evaluate_change(change, ["Acme", "業務効率化", "タスク管理"]) == STATUS_DRIFT


def test_load_errors(tmp_path) -> None:
    with pytest.raises(HygieneConfigError, match="not found"):
        load_hygiene_config(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    with pytest.raises(HygieneConfigError, match="not readable JSON"):
        load_hygiene_config(bad)


def test_module_is_pure_no_db_network_or_llm_imports() -> None:
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "app" / "affiliate" / "catalog_hygiene.py"
    ).read_text(encoding="utf-8")
    for banned in (
        "sqlalchemy",
        "httpx",
        "requests",
        "google",
        "openai",
        "anthropic",
        "socket",
        "fastapi",
    ):
        assert f"import {banned}" not in source and f"from {banned}" not in source, banned
