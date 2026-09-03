"""Article の外部入出力用スキーマ。

SQLAlchemy モデルを直接 API 入出力に使わないための境界。
モデル属性 ``body`` / ``wordpress_post_id`` はここでは
``draft_content`` / ``wordpress_id`` として公開する (対応付けは Service 層で行う)。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.article.draft_prompt_package import EditorialOverridesV1
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


# --- Source (公式ページの観測記録。immutable) --------------------------------

SourceType = Literal[
    "official_product",
    "official_pricing",
    "official_docs",
    "official_help",
    "official_announcement",
    "secondary",
]

OFFICIAL_SOURCE_TYPES: frozenset[str] = frozenset(
    {
        "official_product",
        "official_pricing",
        "official_docs",
        "official_help",
        "official_announcement",
    }
)


class SourceCreate(BaseModel):
    """公式ページの観測記録を登録する入力。URL safety は Service で検証する。"""

    model_config = ConfigDict(extra="forbid")

    source_type: SourceType
    source_url: str = Field(min_length=1, max_length=1024)
    title: str | None = Field(default=None, max_length=512)
    checked_at: datetime


class SourceRead(BaseModel):
    id: int
    article_id: int
    source_type: str
    source_url: str | None
    title: str | None
    checked_at: datetime | None
    created_at: datetime


# --- ArticleFact (immutable 履歴) ------------------------------------------


class ArticleFactCreate(BaseModel):
    """1 tool・1 fact_key の観測結果。`update` はせず、新しい行を append する。"""

    model_config = ConfigDict(extra="forbid")

    subject_ref: str = Field(min_length=1, max_length=200)
    affiliate_program_id: int | None = None
    fact_key: str
    fact_value: object | None = None
    value_status: Literal["verified", "unknown", "not_applicable"]
    unknown_reason: str | None = Field(default=None, max_length=500)
    source_id: int | None = None
    checked_at: datetime


class ArticleFactRead(BaseModel):
    id: int
    article_id: int
    subject_ref: str
    affiliate_program_id: int | None
    fact_key: str
    fact_value: object | None
    value_status: str
    unknown_reason: str | None
    source_id: int | None
    checked_at: datetime
    created_at: datetime


# --- FactPack (read-time 導出。DB 非永続) --------------------------------


class FactEntry(BaseModel):
    fact_key: str
    value: object | None
    value_status: str
    source_id: int | None
    source_url: str | None
    checked_at: datetime
    unknown_reason: str | None
    fresh: bool


class ToolFacts(BaseModel):
    subject_ref: str
    affiliate_program_id: int | None
    facts: list[FactEntry]
    usable_claims: list[str]
    do_not_claim: list[str]
    pricing_checked_at: datetime | None
    last_verified_at: datetime | None


class MissingFact(BaseModel):
    subject_ref: str
    fact_key: str
    reason: Literal["not_researched", "unknown", "not_applicable"]


class StaleFact(BaseModel):
    subject_ref: str
    fact_key: str
    checked_at: datetime
    max_age_days: int


class FreshnessReport(BaseModel):
    within_policy: bool
    stale_facts: list[StaleFact]
    stalest_pricing_checked_at: datetime | None


class ToolReadiness(BaseModel):
    subject_ref: str
    ok: bool
    missing_required: list[str]
    stale_required: list[str]


class FactPackReadiness(BaseModel):
    drafting_allowed: bool
    per_tool: list[ToolReadiness]
    blocking_reasons: list[str]


class FactPackPlanMetadata(BaseModel):
    article_type: str | None
    target_reader: str
    search_intent_summary: str
    outline_headings: list[str]
    comparison_axes: list[str]
    cta_strategy: str
    cannibalization_guidance: str


class FactPackAffiliateCandidate(BaseModel):
    program_id: int
    name: str
    provider: str | None
    recommended_role: str
    commission_type: str | None
    commission_value: float | None


class SourceCoverage(BaseModel):
    source_count: int
    by_type: dict[str, int]
    tools_with_official_pricing: int
    tools_total: int


class FactPackDTO(BaseModel):
    article: ArticleRead
    keyword_id: int | None
    keyword: str | None
    plan_metadata: FactPackPlanMetadata | None
    affiliate_candidates: list[FactPackAffiliateCandidate]
    tool_facts: list[ToolFacts]
    source_coverage: SourceCoverage
    missing_facts: list[MissingFact]
    freshness: FreshnessReport
    readiness: FactPackReadiness
    warnings: list[str]


# --- DraftInputSnapshot (LLM draft 生成入力の凍結 artifact) -------------------


class DraftInputGateStatus(BaseModel):
    can_freeze: bool
    failed_gates: list[str]


class DraftInputPreviewRead(BaseModel):
    """read-only preview。DB write なし。"""

    article_id: int
    snapshot_version: str
    builder_version: str
    content_hash: str
    payload: dict
    readiness: dict
    gate_status: DraftInputGateStatus


class DraftInputFreezeRequest(BaseModel):
    """freeze 入力。preview で人が見た content_hash を必須で渡す (drift guard)。"""

    expected_content_hash: str = Field(min_length=64, max_length=64)


class DraftInputSnapshotSummaryRead(BaseModel):
    """一覧用のメタデータ (payload 全文は含めない)。"""

    id: int
    article_id: int
    snapshot_version: str
    builder_version: str
    plan_snapshot_origin: str
    content_hash: str
    primary_affiliate_program_id: int | None
    comparison_program_ids: list[int]
    drafting_allowed_at_freeze: bool
    frozen_at: datetime
    created_at: datetime


class DraftInputSnapshotRead(DraftInputSnapshotSummaryRead):
    """detail (payload 全文を含む)。"""

    payload: dict


class DraftInputFreezeResponse(BaseModel):
    snapshot: DraftInputSnapshotRead
    already_frozen: bool


# --- DraftGenerationRun / DraftPromptPackage --------------------------------


class GenerationParametersV1(BaseModel):
    """LLM 実行パラメータ。既知の安全キーのみ (secret 禁止, §57)。"""

    model_config = ConfigDict(extra="forbid")

    temperature: float | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    top_p: float | None = None
    seed: int | None = None
    stop: list[str] | None = None


class DraftGenerationPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: int
    editorial_overrides: EditorialOverridesV1


class DraftGenerationPreviewRead(BaseModel):
    article_id: int
    snapshot_id: int
    prompt_package_version: str
    prompt_builder_version: str
    template_version: str
    prompt_input_hash: str
    rendered_prompt_hash: str
    prompt_package: dict
    rendered_prompt: str
    validation_summary: dict
    estimated_size: dict


class DraftGenerationRunPrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: int
    expected_prompt_hash: str = Field(min_length=64, max_length=64)
    expected_rendered_prompt_hash: str = Field(min_length=64, max_length=64)
    execution_mode: str
    provider: str | None = None
    model: str | None = None
    generation_parameters: GenerationParametersV1 | None = None
    editorial_overrides: EditorialOverridesV1
    idempotency_key: str | None = Field(default=None, max_length=64)


class DraftGenerationRunSummaryRead(BaseModel):
    id: int
    article_id: int
    snapshot_id: int
    snapshot_content_hash: str
    status: str
    execution_mode: str
    provider: str | None
    model: str | None
    prompt_template_version: str
    prompt_builder_version: str
    prompt_input_hash: str
    rendered_prompt_hash: str
    idempotency_key: str | None
    validation_overall: str | None
    promotion_eligible: bool | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DraftGenerationRunRead(DraftGenerationRunSummaryRead):
    prompt_package: dict
    rendered_prompt: str
    editorial_overrides: dict
    generation_parameters: dict | None
    raw_output: str | None
    parsed_body: str | None
    parsed_meta_description: str | None
    generation_notes: list[str] | None
    validation_report: dict | None
    token_usage: dict | None
    error_message: str | None


class DraftGenerationPrepareResponse(BaseModel):
    run: DraftGenerationRunSummaryRead
    already_prepared: bool


class DraftGenerationExecuteResponse(BaseModel):
    run: DraftGenerationRunSummaryRead
    next_action: str
    rendered_prompt: str | None = None


class DraftGenerationSubmitResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_output: str


class DraftGenerationSubmitResultResponse(BaseModel):
    run: DraftGenerationRunRead
