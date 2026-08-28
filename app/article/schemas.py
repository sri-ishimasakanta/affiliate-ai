"""Article の外部入出力用スキーマ。

SQLAlchemy モデルを直接 API 入出力に使わないための境界。
モデル属性 ``body`` / ``wordpress_post_id`` はここでは
``draft_content`` / ``wordpress_id`` として公開する (対応付けは Service 層で行う)。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.article.planning import ArticleType
from app.models.enums import ArticleStatus


class ArticleCreate(BaseModel):
    """記事新規登録の入力。"""

    keyword_id: int | None = None
    title: str = Field(min_length=1, max_length=512)
    slug: str = Field(min_length=1, max_length=255)


class ArticleUpdate(BaseModel):
    """記事部分更新の入力。

    未指定のフィールドは変更しない (``model_dump(exclude_unset=True)`` を利用)。
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=512)
    slug: str | None = Field(default=None, min_length=1, max_length=255)
    draft_content: str | None = None


class ArticleStatusUpdate(BaseModel):
    """status 変更専用の入力。

    Enum を用いるため、存在しない status 文字列は validation error (422) になる。
    """

    model_config = ConfigDict(extra="forbid")

    status: ArticleStatus


class ArticleRead(BaseModel):
    """記事の出力表現。"""

    id: int
    keyword_id: int | None
    title: str
    slug: str
    status: ArticleStatus
    draft_content: str | None
    published_url: str | None
    wordpress_id: int | None
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None


# --- Article <-> AffiliateProgram の関連 (中間モデル操作) ---------------------


class ArticleAffiliateProgramCreate(BaseModel):
    """記事に広告案件を紐付ける入力。"""

    model_config = ConfigDict(extra="forbid")

    affiliate_program_id: int
    is_primary: bool = False


class ArticleAffiliateProgramUpdate(BaseModel):
    """紐付けの更新入力 (V1 では primary フラグのみ)。"""

    model_config = ConfigDict(extra="forbid")

    is_primary: bool


class ArticleAffiliateProgramRead(BaseModel):
    """記事 × 広告案件の関連の出力。tracking_url 等は含めない。"""

    id: int
    article_id: int
    affiliate_program_id: int
    is_primary: bool
    created_at: datetime


# --- Article Plan (DB 非永続。keyword から都度導出する) ----------------------


class PlanReadiness(BaseModel):
    complete: bool
    present_components: list[str]
    missing_components: list[str]
    opportunity_score: float | None


class PlanSectionRead(BaseModel):
    level: str
    heading: str
    purpose: str
    required_elements: list[str]


class ComparisonAxisRead(BaseModel):
    axis: str
    data_availability: Literal["catalog", "future_research_required"]


class AffiliateCandidateRead(BaseModel):
    """記事内で紹介候補になり得る active program。tracking_url / credential は返さない。"""

    program_id: int
    name: str
    provider: str | None
    commission_type: str | None
    commission_value: float | None
    currency: str | None
    matched_terms: list[str]
    monetization_data_available: bool
    recommended_role: Literal[
        "primary_candidate", "secondary_candidate", "comparison_candidate"
    ]


class CannibalizationInfo(BaseModel):
    originality: float | None
    corpus_available: bool | None
    max_similarity: float | None
    most_similar_kind: str | None
    most_similar_keyword_id: int | None
    most_similar_keyword_text: str | None
    guidance: str
    acknowledgment_required: bool


class ArticlePlanDTO(BaseModel):
    """keyword から決定論的に導出する記事企画。DB へは保存しない。"""

    keyword_id: int
    keyword: str

    readiness: PlanReadiness

    working_title: str
    proposed_slug: str
    slug_available: bool

    article_type: ArticleType | None

    target_reader: str
    search_intent_summary: str

    primary_goal: str
    secondary_goals: list[str]

    outline: list[PlanSectionRead]
    comparison_axes: list[ComparisonAxisRead]

    affiliate_candidates: list[AffiliateCandidateRead]
    catalog_drift: bool
    catalog_snapshot_available: bool
    snapshot_program_ids: list[int]
    live_program_ids: list[int]

    cta_strategy: str
    cannibalization: CannibalizationInfo

    compliance_checklist: list[str]
    quality_guardrails: list[str]
    source_requirements: list[str]

    warnings: list[str]
    notes: str | None = None


class ArticlePlanApproveRequest(BaseModel):
    """企画承認 (atomic)。plan 自体は保存せず、承認結果のみ DB 化する。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=512)
    slug: str = Field(min_length=1, max_length=255)
    primary_affiliate_program_id: int | None = None
    secondary_affiliate_program_ids: list[int] = Field(default_factory=list)
    acknowledge_cannibalization: bool = False
    acknowledge_incomplete_plan: bool = False
    notes: str | None = Field(default=None, max_length=2000)
