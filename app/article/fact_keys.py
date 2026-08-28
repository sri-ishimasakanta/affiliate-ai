"""ArticleFact の V1 fact key と value status の定義 (pure)。

- persistent fact key は 17 種類。`pricing_checked_at` / `last_verified_at` は
  **fact key ではなく FactPack 側で導出**する。
- `list[str]` fact は正規化 (trim / 空要素除外 / 重複除去 / 順序保持)。内容は casefold しない。
"""

from __future__ import annotations

import unicodedata
from enum import StrEnum


class FactKey(StrEnum):
    OFFICIAL_PRODUCT_NAME = "official_product_name"
    OFFICIAL_URL = "official_url"
    CATEGORY = "category"
    PRIMARY_USE_CASES = "primary_use_cases"
    TARGET_USERS = "target_users"
    PRICING_SUMMARY = "pricing_summary"
    FREE_PLAN_AVAILABLE = "free_plan_available"
    FREE_TRIAL_AVAILABLE = "free_trial_available"
    KEY_FEATURES = "key_features"
    AUTOMATION_CAPABILITIES = "automation_capabilities"
    AI_FEATURES = "ai_features"
    INTEGRATIONS = "integrations"
    JAPANESE_LANGUAGE_SUPPORT = "japanese_language_support"
    JAPAN_BUSINESS_SUPPORT = "japan_business_support"
    BUSINESS_PLAN_AVAILABLE = "business_plan_available"
    SECURITY_OR_ENTERPRISE_NOTES = "security_or_enterprise_notes"
    LIMITATIONS = "limitations"


class ValueStatus(StrEnum):
    VERIFIED = "verified"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


# verified 時の fact_value の型 ("str" / "bool" / "list[str]")。
FACT_VALUE_TYPE: dict[FactKey, str] = {
    FactKey.OFFICIAL_PRODUCT_NAME: "str",
    FactKey.OFFICIAL_URL: "str",
    FactKey.CATEGORY: "str",
    FactKey.PRIMARY_USE_CASES: "list[str]",
    FactKey.TARGET_USERS: "list[str]",  # V1 は list[str] に固定
    FactKey.PRICING_SUMMARY: "str",
    FactKey.FREE_PLAN_AVAILABLE: "bool",
    FactKey.FREE_TRIAL_AVAILABLE: "bool",
    FactKey.KEY_FEATURES: "list[str]",
    FactKey.AUTOMATION_CAPABILITIES: "list[str]",
    FactKey.AI_FEATURES: "list[str]",
    FactKey.INTEGRATIONS: "list[str]",
    FactKey.JAPANESE_LANGUAGE_SUPPORT: "bool",
    FactKey.JAPAN_BUSINESS_SUPPORT: "bool",
    FactKey.BUSINESS_PLAN_AVAILABLE: "bool",
    FactKey.SECURITY_OR_ENTERPRISE_NOTES: "str",
    FactKey.LIMITATIONS: "list[str]",
}

LIST_FACT_KEYS: frozenset[FactKey] = frozenset(
    k for k, t in FACT_VALUE_TYPE.items() if t == "list[str]"
)

# readiness gate の required fact (Phase 3B-1 §34)。
REQUIRED_FACT_KEYS: tuple[FactKey, ...] = (
    FactKey.OFFICIAL_PRODUCT_NAME,
    FactKey.OFFICIAL_URL,
    FactKey.PRIMARY_USE_CASES,
    FactKey.KEY_FEATURES,
    FactKey.PRICING_SUMMARY,
    FactKey.FREE_PLAN_AVAILABLE,
)

# 不足しても drafting 可能 (FactPack.warnings へ)。
RECOMMENDED_FACT_KEYS: tuple[FactKey, ...] = tuple(
    k for k in FactKey if k not in REQUIRED_FACT_KEYS
)

# list fact の最小件数 (readiness で使用)。
MIN_LIST_LEN: dict[FactKey, int] = {
    FactKey.PRIMARY_USE_CASES: 1,
    FactKey.KEY_FEATURES: 2,
}


def normalize_str_list(value: object) -> list[str]:
    """list[str] fact を正規化する: NFKC → trim → 空除外 → 重複除去 (順序保持)。

    内容そのものは casefold しない。重複判定は NFKC + trim 済み文字列で行う。
    """

    if not isinstance(value, list):
        raise TypeError("expected a list of strings")
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise TypeError("list items must be strings")
        norm = unicodedata.normalize("NFKC", item).strip()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out
