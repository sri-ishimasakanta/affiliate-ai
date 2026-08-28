"""AffiliateProgram の外部入出力用スキーマ。

SQLAlchemy モデルを直接 API 入出力に使わないための境界。
スキーマのフィールド名はモデル属性名と一致させている (対応付けの変換は不要)。

正規化・検証:
- ``name``: strip、空白のみは拒否
- ``currency``: strip → uppercase、ISO 4217 の 3 文字英字のみ許可 (nullable)
- ``match_terms``: 各要素 strip、空要素の除去、重複除去、入力順維持 (nullable)
- ``commission_value``: None または 0 以上
- ``commission_type``: 既存データ互換のため自由文字列 (新規入力では ``fixed`` /
  ``percentage`` を推奨)
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import AffiliateProgramStatus

_CURRENCY_RE = re.compile(r"[A-Z]{3}")
_MATCH_TERM_MAX_LEN = 255


def _require_non_blank(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be blank")
    return stripped


def _normalize_currency(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().upper()
    if not _CURRENCY_RE.fullmatch(normalized):
        raise ValueError("currency must be a 3-letter alphabetic code (e.g. JPY)")
    return normalized


def _normalize_match_terms(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    seen: set[str] = set()
    result: list[str] = []
    for raw in value:
        term = raw.strip()
        if not term or term in seen:
            continue
        if len(term) > _MATCH_TERM_MAX_LEN:
            raise ValueError(
                f"match_terms entries must be at most {_MATCH_TERM_MAX_LEN} characters"
            )
        seen.add(term)
        result.append(term)
    return result


class AffiliateProgramCreate(BaseModel):
    """アフィリエイト案件の新規登録の入力。"""

    name: str = Field(min_length=1, max_length=255)
    provider: str | None = Field(default=None, max_length=100)
    category: str | None = Field(default=None, max_length=100)
    commission_type: str | None = Field(default=None, max_length=50)
    commission_value: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, max_length=8)
    landing_page_url: str | None = Field(default=None, max_length=1024)
    tracking_url: str | None = Field(default=None, max_length=1024)
    notes: str | None = None
    match_terms: list[str] | None = None
    status: AffiliateProgramStatus = AffiliateProgramStatus.ACTIVE

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _require_non_blank(value)

    @field_validator("currency")
    @classmethod
    def _validate_currency(cls, value: str | None) -> str | None:
        return _normalize_currency(value)

    @field_validator("match_terms")
    @classmethod
    def _validate_match_terms(cls, value: list[str] | None) -> list[str] | None:
        return _normalize_match_terms(value)


class AffiliateProgramUpdate(BaseModel):
    """アフィリエイト案件の部分更新の入力。

    未指定のフィールドは変更しない (``model_dump(exclude_unset=True)`` を利用)。
    明示的に ``null`` を送ると当該フィールドをクリアする。
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    provider: str | None = Field(default=None, max_length=100)
    category: str | None = Field(default=None, max_length=100)
    commission_type: str | None = Field(default=None, max_length=50)
    commission_value: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, max_length=8)
    landing_page_url: str | None = Field(default=None, max_length=1024)
    tracking_url: str | None = Field(default=None, max_length=1024)
    notes: str | None = None
    match_terms: list[str] | None = None
    status: AffiliateProgramStatus | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str | None:
        return None if value is None else _require_non_blank(value)

    @field_validator("currency")
    @classmethod
    def _validate_currency(cls, value: str | None) -> str | None:
        return _normalize_currency(value)

    @field_validator("match_terms")
    @classmethod
    def _validate_match_terms(cls, value: list[str] | None) -> list[str] | None:
        return _normalize_match_terms(value)


class AffiliateProgramRead(BaseModel):
    """アフィリエイト案件の出力表現。"""

    id: int
    name: str
    provider: str | None
    category: str | None
    commission_type: str | None
    commission_value: float | None
    currency: str | None
    landing_page_url: str | None
    tracking_url: str | None
    notes: str | None
    match_terms: list[str]
    status: AffiliateProgramStatus
    created_at: datetime
    updated_at: datetime
