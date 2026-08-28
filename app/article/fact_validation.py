"""ArticleFact の値・状態・型の検証 (pure)。

single 作成 (`ArticleFactService`) と bulk import (`ArticleFactImportService`) で
**同一のルール**を使うための共有ロジック。DB / FastAPI 非依存。
Source の存在・所有権チェックは呼び出し側 (Service) の責務。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.article.fact_keys import (
    FACT_VALUE_TYPE,
    FactKey,
    ValueStatus,
    normalize_str_list,
)
from app.article.schemas import OFFICIAL_SOURCE_TYPES
from app.exceptions import FactValidationError


@dataclass(frozen=True)
class ValidatedFact:
    fact_key: FactKey
    value_status: ValueStatus
    fact_value: object | None
    unknown_reason: str | None


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise FactValidationError(message)


def _coerce_value(fact_key: FactKey, value: object) -> object:
    expected = FACT_VALUE_TYPE[fact_key]
    if expected == "bool":
        _require(isinstance(value, bool), f"{fact_key}: value must be a boolean")
        return value
    if expected == "str":
        _require(
            isinstance(value, str) and value.strip() != "",
            f"{fact_key}: value must be a non-empty string",
        )
        return value.strip()
    if expected == "list[str]":
        try:
            normalized = normalize_str_list(value)
        except TypeError as exc:
            raise FactValidationError(f"{fact_key}: {exc}") from exc
        _require(len(normalized) >= 1, f"{fact_key}: list must not be empty")
        return normalized
    raise FactValidationError(f"{fact_key}: unsupported value type {expected}")  # pragma: no cover


def validate_fact(
    *,
    fact_key: str,
    value_status: str,
    fact_value: object | None,
    unknown_reason: str | None,
    source_type: str | None,
    source_present: bool,
    checked_at: datetime,
    now: datetime,
) -> ValidatedFact:
    """正規化済み ``ValidatedFact`` を返す。不正なら :class:`FactValidationError`。"""

    try:
        key = FactKey(fact_key)
    except ValueError as exc:
        raise FactValidationError(f"unknown fact_key: {fact_key!r}") from exc
    try:
        status = ValueStatus(value_status)
    except ValueError as exc:
        raise FactValidationError(f"unknown value_status: {value_status!r}") from exc

    if checked_at.tzinfo is None:
        raise FactValidationError("checked_at must be timezone-aware")
    if checked_at > now:
        raise FactValidationError("checked_at must not be in the future")

    reason = (unknown_reason or "").strip() or None

    if status is ValueStatus.VERIFIED:
        _require(fact_value is not None, f"{key}: verified fact requires fact_value")
        _require(source_present, f"{key}: verified fact requires a source")
        _require(
            source_type in OFFICIAL_SOURCE_TYPES,
            f"{key}: verified fact requires an official_* source (got {source_type!r})",
        )
        _require(reason is None, f"{key}: verified fact must not have unknown_reason")
        coerced = _coerce_value(key, fact_value)
        return ValidatedFact(key, status, coerced, None)

    if status is ValueStatus.UNKNOWN:
        _require(fact_value is None, f"{key}: unknown fact must not carry a value")
        _require(source_present, f"{key}: unknown fact requires a source (proof it was checked)")
        _require(
            source_type in OFFICIAL_SOURCE_TYPES,
            f"{key}: unknown fact requires an official_* source",
        )
        _require(reason is not None, f"{key}: unknown fact requires a non-blank unknown_reason")
        return ValidatedFact(key, status, None, reason)

    # not_applicable
    _require(fact_value is None, f"{key}: not_applicable fact must not carry a value")
    _require(
        reason is not None,
        f"{key}: not_applicable fact requires a non-blank explanation (unknown_reason)",
    )
    return ValidatedFact(key, status, None, reason)
