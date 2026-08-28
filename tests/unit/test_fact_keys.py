"""app/article/fact_keys.py の検証。"""

import pytest

from app.article.fact_keys import (
    FACT_VALUE_TYPE,
    LIST_FACT_KEYS,
    RECOMMENDED_FACT_KEYS,
    REQUIRED_FACT_KEYS,
    FactKey,
    ValueStatus,
    normalize_str_list,
)


def test_seventeen_persistent_fact_keys() -> None:
    assert len(list(FactKey)) == 17
    assert set(FACT_VALUE_TYPE) == set(FactKey)
    # derived timestamps は fact_key ではない
    assert "pricing_checked_at" not in {k.value for k in FactKey}
    assert "last_verified_at" not in {k.value for k in FactKey}


def test_value_status_members() -> None:
    assert {s.value for s in ValueStatus} == {"verified", "unknown", "not_applicable"}


def test_required_and_recommended_partition() -> None:
    assert set(REQUIRED_FACT_KEYS) | set(RECOMMENDED_FACT_KEYS) == set(FactKey)
    assert not set(REQUIRED_FACT_KEYS) & set(RECOMMENDED_FACT_KEYS)
    assert FactKey.PRICING_SUMMARY in REQUIRED_FACT_KEYS
    assert FactKey.FREE_PLAN_AVAILABLE in REQUIRED_FACT_KEYS
    assert FactKey.AI_FEATURES in RECOMMENDED_FACT_KEYS


def test_target_users_is_single_form_list() -> None:
    assert FACT_VALUE_TYPE[FactKey.TARGET_USERS] == "list[str]"
    assert FactKey.TARGET_USERS in LIST_FACT_KEYS


def test_normalize_str_list_trim_dedupe_dropempty_orderpreserve() -> None:
    out = normalize_str_list(["  b ", "a", "b", "", "  ", "ｃ"])
    assert out == ["b", "a", "c"]  # NFKC でfullwidth c -> c、順序保持、重複除去


def test_normalize_str_list_rejects_non_list_and_non_str() -> None:
    with pytest.raises(TypeError):
        normalize_str_list("not a list")
    with pytest.raises(TypeError):
        normalize_str_list(["ok", 3])


def test_normalize_str_list_does_not_casefold() -> None:
    assert normalize_str_list(["ChatGPT", "Make"]) == ["ChatGPT", "Make"]
