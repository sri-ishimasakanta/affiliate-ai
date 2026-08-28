"""Keyword の外部入出力用スキーマ。

SQLAlchemy モデルを直接 API 入出力に使わないための境界。
モデル属性 ``intent`` はここでは ``search_intent`` として公開する
(対応付けは Service 層で行う)。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import KeywordSignalComponent, KeywordStatus


class KeywordCreate(BaseModel):
    """キーワード新規登録の入力。"""

    keyword: str = Field(min_length=1, max_length=255)
    search_intent: str | None = Field(default=None, max_length=50)
    category: str | None = Field(default=None, max_length=100)


class KeywordUpdate(BaseModel):
    """キーワード部分更新の入力。

    未指定のフィールドは変更しない (``model_dump(exclude_unset=True)`` を利用)。
    """

    model_config = ConfigDict(extra="forbid")

    search_intent: str | None = Field(default=None, max_length=50)
    category: str | None = Field(default=None, max_length=100)


class KeywordStatusUpdate(BaseModel):
    """status 変更専用の入力。

    Enum を用いるため、存在しない status 文字列は validation error (422) になる。
    """

    model_config = ConfigDict(extra="forbid")

    status: KeywordStatus


class KeywordRead(BaseModel):
    """キーワードの出力表現。"""

    id: int
    keyword: str
    search_intent: str | None
    category: str | None
    status: KeywordStatus
    opportunity_score: float | None
    created_at: datetime
    updated_at: datetime


class KeywordScoreCreate(BaseModel):
    """Opportunity Score 計算の入力。

    各コンポーネントは 0〜100。``total_score`` と ``score_version`` は
    クライアントから受け付けない (``extra="forbid"`` で 422 になる)。
    """

    model_config = ConfigDict(extra="forbid")

    search_demand: float = Field(ge=0, le=100)
    commercial_intent: float = Field(ge=0, le=100)
    affiliate_opportunity: float = Field(ge=0, le=100)
    competition_ease: float = Field(ge=0, le=100)
    trend: float = Field(ge=0, le=100)
    originality: float = Field(ge=0, le=100)
    site_relevance: float = Field(ge=0, le=100)
    input_source: str = Field(default="manual", min_length=1, max_length=50)


class KeywordScoreRead(BaseModel):
    """Opportunity Score 履歴レコードの出力表現 (immutable)。"""

    id: int
    keyword_id: int
    search_demand: float
    commercial_intent: float
    affiliate_opportunity: float
    competition_ease: float
    trend: float
    originality: float
    site_relevance: float
    total_score: float
    score_version: str
    input_source: str
    created_at: datetime


class KeywordSignalCreate(BaseModel):
    """component 値の根拠となる Signal の入力 (immutable 履歴として保存)。

    ``normalized_value`` は 0〜100 の正規化済み値。正規化は行わない。
    """

    model_config = ConfigDict(extra="forbid")

    component: KeywordSignalComponent
    normalized_value: float = Field(ge=0, le=100)
    provider: str = Field(min_length=1, max_length=50)
    raw_data: dict[str, Any] | list[Any] | None = None
    source_reference: str | None = None
    observed_at: datetime
    period_start: datetime | None = None
    period_end: datetime | None = None


class CompetitionEaseManualCreate(BaseModel):
    """competition_ease の手動 evidence 入力 (Organic SEO Keyword Difficulty)。

    ``keyword_difficulty`` は 0 (easy) 〜 100 (hard) の Organic SEO Keyword Difficulty。
    Google Ads の competition / competition_index は入力しない。
    ``source_reference`` に credential / API key / account ID を入れない。
    """

    model_config = ConfigDict(extra="forbid")

    keyword_difficulty: float = Field(ge=0, le=100)
    source_name: str = Field(min_length=1, max_length=100)
    source_reference: str | None = Field(default=None, max_length=500)
    observed_at: datetime | None = None

    @field_validator("keyword_difficulty", mode="before")
    @classmethod
    def _reject_bool(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("keyword_difficulty must be a number, not a boolean")
        return value

    @field_validator("source_name")
    @classmethod
    def _strip_source_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("source_name must not be blank")
        return stripped

    @field_validator("source_reference")
    @classmethod
    def _strip_source_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class KeywordSignalRead(BaseModel):
    """Signal 履歴レコードの出力表現 (immutable)。"""

    id: int
    keyword_id: int
    component: KeywordSignalComponent
    normalized_value: float
    provider: str
    raw_data: dict[str, Any] | list[Any] | None
    source_reference: str | None
    observed_at: datetime
    period_start: datetime | None
    period_end: datetime | None
    created_at: datetime
