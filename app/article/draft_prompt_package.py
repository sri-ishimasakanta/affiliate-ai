"""DraftPromptPackage builder — DraftInputSnapshot payload から **model が見てよい
最小・安全・決定論的入力** を組み立てる (pure)。

入力は次の 3 つだけ (§23):
    - ``DraftInputSnapshot.payload`` (frozen)
    - snapshot id / content_hash
    - 検証済み :class:`EditorialOverridesV1`

live な ArticleFact / Source / AffiliateProgram / ArticlePlan / FactPack へは
一切アクセスしない。commission / provider / tracking_url / planning_role / Snapshot
audit / opportunity_score / internal warnings 等は **構造的に読まず**、生成後に
:func:`assert_no_forbidden_keys` で禁止キーの不在を検証する (§26/§27)。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.article.draft_input_canonical import canonical_datetime
from app.exceptions import DraftGenerationNotReadyError
from app.models.draft_generation_run import (
    PROMPT_BUILDER_VERSION,
    PROMPT_PACKAGE_VERSION,
    PROMPT_TEMPLATE_VERSION,
)

# PromptPackage に **絶対に現れてはいけない** dict キー (§26/§27)。
# 値の中に一般語として出るのは別 validator (commission leakage) が扱う。
FORBIDDEN_PACKAGE_KEYS: frozenset[str] = frozenset(
    {
        "commission_type",
        "commission_value",
        "commission",
        "provider",
        "network",
        "tracking_url",
        "landing_page_url",
        "planning_role",
        "payout",
        "cookie_duration",
        "conversion_economics",
        "opportunity_score",
        "api_key",
        "apikey",
        "token",
        "secret",
        "password",
        "credential",
        "authorization",
        "bearer",
    }
)

_DEFAULT_UNKNOWN_PHRASING = (
    "公式情報では確認できませんでした（存在/非存在・対応/非対応のいずれも断定しない）"
)
_DO_NOT_ASSERT_SUFFIX = "：この項目は Human editorial ruling により断定禁止"

_PRIMARY_MEANING = (
    "CTA / 紹介導線上の primary candidate。事実比較・弱点・目的別の適否は "
    "usable_facts に基づいて公平に書くこと。primary であることを理由に最高評価を"
    "強制しない。"
)

_PRICING_INSTRUCTION = (
    "料金は各 tool の pricing_summary fact の記載範囲のみを使う。Hub / 製品別、"
    "アドオン別料金、期間限定価格などの区別を pricing_summary の文言どおりに保ち、"
    "その範囲外の数値を作らない。すべての料金に as_of_label と公式 source_url を併記する。"
)

# audit.excluded_from_snapshot は「Snapshot にあったが PromptPackage へ持ち込まなかった
# もの」の説明。禁止トークンそのものを列挙すると downstream の素朴な substring scanner
# を誤爆させるため、散文で記述する。
_EXCLUDED_FROM_SNAPSHOT = (
    "affiliate 収益条件 (報酬タイプ・報酬額) / affiliate ネットワーク名 / "
    "計画段階の候補ロール / tracking・landing URL / Snapshot audit ブロック / "
    "機会スコア / 内部 readiness warning / Source タイトル — は PromptPackage へ"
    "持ち込んでいない。"
)


class AxisRulingV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    axis: str
    action: Literal["SOFTEN", "REMOVE", "KEEP"]
    instruction: str


class JapaneseSupportRulingV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verified_true: list[str] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)
    not_researched: list[str] = Field(default_factory=list)
    rule: str


class EditorialOverridesV1(BaseModel):
    """Snapshot freeze 後に Human が確定した編集判断 (§10/§11)。extra="forbid"。"""

    model_config = ConfigDict(extra="forbid")

    primary: str
    comparison_set_size: int
    axis_rulings: list[AxisRulingV1] = Field(default_factory=list)
    japanese_support_ruling: JapaneseSupportRulingV1 | None = None
    do_not_assert: list[str] = Field(default_factory=list)
    commission_to_llm: bool = False


def _fact_key_order(payload: dict) -> list[str]:
    return list(payload.get("policy", {}).get("fact_key_order", []))


def _pricing_as_of_label(payload: dict) -> str:
    """全 tool の pricing_summary cell の最古 checked_at から「YYYY年M月時点」。"""

    dates: list[str] = []
    for tool in payload.get("tools", []):
        for cell in tool.get("cells", []):
            if cell.get("fact_key") == "pricing_summary" and cell.get("checked_at"):
                dates.append(cell["checked_at"])
    if not dates:
        return "調査時点"
    oldest = min(dates)  # canonical ISO 文字列なので辞書順 = 時系列順
    dt = datetime.fromisoformat(oldest)
    return f"{dt.year}年{dt.month}月時点"


def _tool_entry(tool: dict, do_not_assert: set[str]) -> dict:
    subject = tool["subject_ref"]
    usable_facts: list[dict] = []
    unknown_fact_keys: list[dict] = []
    not_researched_fact_keys: list[str] = []
    for cell in tool["cells"]:
        state = cell["state"]
        if state == "verified":
            entry = {
                "fact_key": cell["fact_key"],
                "value": cell["value"],
                "checked_at": cell["checked_at"],
            }
            src = cell.get("source")
            if src is not None:
                entry["source"] = {
                    "source_type": src["source_type"],
                    "source_url": src["source_url"],
                }
            usable_facts.append(entry)
        elif state == "unknown":
            phrasing = _DEFAULT_UNKNOWN_PHRASING
            if f"{subject}/{cell['fact_key']}" in do_not_assert:
                phrasing += _DO_NOT_ASSERT_SUFFIX
            unknown_fact_keys.append(
                {"fact_key": cell["fact_key"], "allowed_phrasing": phrasing}
            )
        elif state == "not_researched":
            not_researched_fact_keys.append(cell["fact_key"])
    return {
        "subject_ref": subject,
        "is_primary": bool(tool["is_primary"]),
        "usable_facts": usable_facts,
        "forbidden_fact_keys": list(tool["do_not_claim"]),
        "unknown_fact_keys": unknown_fact_keys,
        "not_researched_fact_keys": not_researched_fact_keys,
    }


def build_prompt_package(
    *,
    snapshot_payload: dict,
    snapshot_id: int,
    snapshot_content_hash: str,
    overrides: EditorialOverridesV1,
    now: datetime,
) -> dict:
    """frozen Snapshot payload + Human overrides から DraftPromptPackage を組む。"""

    p = snapshot_payload
    do_not_assert = set(overrides.do_not_assert)

    tools = [_tool_entry(t, do_not_assert) for t in p["tools"]]

    package: dict = {
        "prompt_package_version": PROMPT_PACKAGE_VERSION,
        "prompt_builder_version": PROMPT_BUILDER_VERSION,
        "template_version": PROMPT_TEMPLATE_VERSION,
        "snapshot_id": snapshot_id,
        "snapshot_content_hash": snapshot_content_hash,
        "article": {
            "id": p["article"]["id"],
            "title": p["article"]["title"],
            "slug": p["article"]["slug"],
            "keyword": p["keyword"]["text"],
        },
        "plan": {
            "article_type": p["plan"]["article_type"],
            "target_reader": p["plan"]["target_reader"],
            "search_intent_summary": p["plan"]["search_intent_summary"],
            "primary_goal": p["plan"]["primary_goal"],
            "secondary_goals": list(p["plan"]["secondary_goals"]),
            "outline": p["plan"]["outline"],
            "comparison_axes": p["plan"]["comparison_axes"],
            "cta_strategy": p["plan"]["cta_strategy"],
            "cannibalization_guidance": p["plan"]["cannibalization_guidance"],
            "cannibalization_acknowledgment_required": p["plan"][
                "cannibalization_acknowledgment_required"
            ],
            "compliance_checklist": list(p["plan"]["compliance_checklist"]),
            "quality_guardrails": list(p["plan"]["quality_guardrails"]),
            "source_requirements": list(p["plan"]["source_requirements"]),
        },
        "primary": {
            "subject_ref": overrides.primary,
            "meaning": _PRIMARY_MEANING,
        },
        "comparison_tools": tools,
        "fact_key_order": _fact_key_order(p),
        "editorial_overrides": overrides.model_dump(mode="json"),
        "pricing_notice_policy": {
            "as_of_label": _pricing_as_of_label(p),
            "instruction": _PRICING_INSTRUCTION,
        },
        "audit": {
            "built_at": canonical_datetime(now),
            "source_of_truth": (
                f"draft_input_snapshot #{snapshot_id} "
                f"(content_hash {snapshot_content_hash})"
            ),
            "excluded_note": _EXCLUDED_FROM_SNAPSHOT,
        },
    }

    assert_no_forbidden_keys(package)
    return package


def assert_no_forbidden_keys(package: object, *, path: str = "") -> None:
    """PromptPackage を再帰探索し、禁止 dict キーが 1 つでもあれば例外。

    キー名ベースの検査 (値中の一般語は誤検出しない, §27)。
    """

    if isinstance(package, dict):
        for key, value in package.items():
            if key.lower() in FORBIDDEN_PACKAGE_KEYS:
                raise DraftGenerationNotReadyError(
                    f"forbidden key {key!r} present in prompt package at {path or '<root>'}"
                )
            assert_no_forbidden_keys(value, path=f"{path}.{key}" if path else key)
    elif isinstance(package, list):
        for i, item in enumerate(package):
            assert_no_forbidden_keys(item, path=f"{path}[{i}]")
