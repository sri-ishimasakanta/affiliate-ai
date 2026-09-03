"""app/article/draft_output_validators.py の pure テスト (heuristic シグナル)。"""

from __future__ import annotations

from app.article.draft_output_contract import ParsedDraft
from app.article.draft_output_validators import validate_draft_output

_TOOLS = ["Make", "HubSpot", "ClickUp", "monday.com", "Pipedrive", "Reclaim.ai", "Todoist"]


def _package(primary="Make", with_limitations=True) -> dict:
    tools = []
    for t in _TOOLS:
        usable = [{"fact_key": "pricing_summary", "value": "x"}]
        if with_limitations and t == primary:
            usable.append({"fact_key": "limitations", "value": "有料は…"})
        tools.append({"subject_ref": t, "is_primary": t == primary, "usable_facts": usable})
    return {
        "primary": {"subject_ref": primary},
        "comparison_tools": tools,
        "plan": {"outline": [{"level": "H2", "heading": f"H{i}"} for i in range(6)]},
    }


def _body_ok() -> str:
    tools_line = " / ".join(_TOOLS)
    return (
        "本記事は広告（アフィリエイト）を含みます。\n\n"
        "## 業務効率化ツールとは\n" + "解説。" * 200 + "\n\n"
        "## 選び方\n比較軸を示します。\n\n"
        f"## おすすめ業務効率化ツール比較\n{tools_line} を比較します。"
        "料金は2026年8月時点。\n\n"
        "## 目的別おすすめ\n用途に応じて選びます。\n\n"
        "## 導入時の注意点\nMake には注意点もあります。請求書払いは各社で異なり未確認。\n\n"
        "## よくある質問\nQ&A。\n\n"
        "## まとめ\n結論。"
    )


_DEFAULT_META = "業務効率化ツールを目的別に比較して選び方を解説する記事です。"


def _parsed(body: str, meta: str = _DEFAULT_META) -> ParsedDraft:
    return ParsedDraft(meta_description=meta, body_markdown=body)


def test_clean_body_passes_or_warns_only() -> None:
    r = validate_draft_output(parsed=_parsed(_body_ok()), package=_package())
    assert r["overall"] in {"pass", "warn"}
    assert r["promotion_eligible"] is True


def test_h1_in_body_is_fail() -> None:
    body = "# タイトル\n\n" + _body_ok()
    r = validate_draft_output(parsed=_parsed(body), package=_package())
    ids = {c["id"]: c["level"] for c in r["checks"]}
    assert ids["body_no_h1"] == "fail"
    assert r["promotion_eligible"] is False


def test_missing_tool_is_fail() -> None:
    body = _body_ok().replace("Todoist", "")
    r = validate_draft_output(parsed=_parsed(body), package=_package())
    ids = {c["id"]: c["level"] for c in r["checks"]}
    assert ids["all_tools_present"] == "fail"


def test_missing_pr_disclosure_is_fail() -> None:
    body = _body_ok().replace("本記事は広告（アフィリエイト）を含みます。", "はじめに")
    r = validate_draft_output(parsed=_parsed(body), package=_package())
    ids = {c["id"]: c["level"] for c in r["checks"]}
    assert ids["pr_disclosure"] == "fail"


def test_make_japanese_assertion_is_fail() -> None:
    body = _body_ok() + "\n\nMake は日本語対応しています。"
    r = validate_draft_output(parsed=_parsed(body), package=_package())
    ids = {c["id"]: c["level"] for c in r["checks"]}
    assert ids["claim_make_japanese"] == "fail"


def test_make_japanese_hedged_is_pass() -> None:
    body = _body_ok() + "\n\nMake の日本語対応は公式情報では確認できませんでした。"
    r = validate_draft_output(parsed=_parsed(body), package=_package())
    ids = {c["id"]: c["level"] for c in r["checks"]}
    assert ids["claim_make_japanese"] == "pass"


def test_commission_leakage_affiliate_context_is_fail() -> None:
    body = _body_ok() + "\n\nMake は報酬率が35%と高いのでおすすめです。"
    r = validate_draft_output(parsed=_parsed(body), package=_package())
    ids = {c["id"]: c["level"] for c in r["checks"]}
    assert ids["commission_leakage"] == "fail"


def test_product_percentage_is_not_commission_leakage() -> None:
    body = _body_ok() + "\n\n年払いにすると30%オフになります（2026年8月時点）。"
    r = validate_draft_output(parsed=_parsed(body), package=_package())
    ids = {c["id"]: c["level"] for c in r["checks"]}
    assert ids["commission_leakage"] == "pass"


def test_fairness_all_seven_compared_but_not_all_recommended_is_ok() -> None:
    body = _body_ok()  # 目的別おすすめは一般的表現のみ = 一部推薦でも OK
    r = validate_draft_output(parsed=_parsed(body), package=_package())
    assert r["promotion_eligible"] is True


def test_unqualified_superlative_about_primary_is_warn() -> None:
    body = _body_ok() + "\n\nMake は最も優れている総合ツールです。"
    r = validate_draft_output(parsed=_parsed(body), package=_package())
    ids = {c["id"]: c["level"] for c in r["checks"]}
    assert ids["fairness_primary_superlative"] == "warn"
    # warn だけなら promotion_eligible は True でよい
    assert r["promotion_eligible"] is True
