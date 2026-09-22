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


class ArticleMonetizationModeUpdate(BaseModel):
    """C2.5.8: content monetization mode の **明示** 変更 (editorial intent)。

    affiliate link を足す / 外すことでは mode は変わらない。この専用操作だけが変更できる。
    """

    model_config = ConfigDict(extra="forbid")

    monetization_mode: Literal["affiliate", "supporting"]


class ArticleMonetizationModeRead(BaseModel):
    """article の実効 mode とその出どころ、および現在の affiliate primary の状態。"""

    article_id: int
    # 保存されている明示値 (legacy 行は None)
    monetization_mode: Literal["affiliate", "supporting"] | None
    # 実効 mode: 明示値、無ければ primary link からの legacy fallback
    effective_monetization_mode: Literal["affiliate", "supporting"]
    monetization_mode_source: Literal["explicit", "legacy_derived"]
    primary_affiliate_program_id: int | None
    affiliate_program_link_count: int
    # affiliate mode なのに primary が無い等、mode と link 状態の不整合 (mode は変えない)
    mode_requirement_violations: list[str] = Field(default_factory=list)


class ArticleStatusUpdate(BaseModel):
    """status 変更専用の入力。

    Enum を用いるため、存在しない status 文字列は validation error (422) になる。
    """

    model_config = ConfigDict(extra="forbid")

    status: ArticleStatus


class ArticlePublicationApprovalRequest(BaseModel):
    """WordPress draft の Human 目視レビュー後の publication approval (review -> approved)。

    任意の status への変更は許さない (このエンドポイントは review -> approved 専用)。
    """

    model_config = ConfigDict(extra="forbid")

    expected_wordpress_post_id: int
    expected_target_request_identity_hash: str = Field(min_length=64, max_length=64)


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
    # C2.5.8: 保存されている content monetization mode (編集上の意図)。
    # None = C2.5.8 より前に承認された legacy 行 (backfill しない)。実効 mode / 出どころ /
    # 要件違反は GET /articles/{id}/monetization-mode が返す。
    monetization_mode: Literal["affiliate", "supporting"] | None = None


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
    # C2.5.4 (追加): brand tier。strong = 自身の名前 / 明示 alias、weak = generic な term だけ。
    match_tier: Literal["strong", "weak"] | None = None
    strong_terms: list[str] = Field(default_factory=list)
    weak_terms: list[str] = Field(default_factory=list)
    tier_reason: str | None = None
    tier_ambiguity: str | None = None
    # C2.5.7 (追加): brand tier とは独立した fit (core / loose / unreviewed。strong だけで match
    # した場合は None)。primary_eligible = strong、または weak かつ core。新規承認の primary は
    # primary_eligible な candidate だけ (loose / unreviewed は文脈・secondary 用に見えるだけ)。
    fit: Literal["core", "loose", "unreviewed"] | None = None
    fit_reason: str | None = None
    core_terms: list[str] = Field(default_factory=list)
    loose_terms: list[str] = Field(default_factory=list)
    unreviewed_terms: list[str] = Field(default_factory=list)
    scoring_eligible: bool | None = None
    primary_eligible: bool | None = None


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
    # C2.5.4 (報告専用・追加): candidates の tier 集計。drift 判定には使わない。
    strong_candidate_count: int | None = None
    weak_candidate_count: int | None = None
    no_strong_affiliate_candidate: bool | None = None
    # C2.5.7 (追加): weak の内訳 (fit) と、新規承認で primary にできる candidate の数
    core_weak_candidate_count: int | None = None
    loose_weak_candidate_count: int | None = None
    unreviewed_weak_candidate_count: int | None = None
    primary_eligible_candidate_count: int | None = None
    no_primary_eligible_candidate: bool | None = None
    # C2.5.8 (追加): monetization mode。「作ってよいか」と「affiliate primary があるか」を分ける
    # C3: 記事タイプ。article_type は推論の結果 (推奨)、既存 article があればその実効値も出す
    recommended_article_type: ArticleType | None = None
    article_type_recommendation_marker: str | None = None
    existing_article_type: ArticleType | None = None
    existing_article_type_source: Literal["explicit", "inferred"] | None = None
    recommended_monetization_mode: Literal["affiliate", "supporting"] | None = None
    monetization_recommendation_reason: str | None = None
    # この keyword の既存 (live) article の実効 mode。未承認なら None
    monetization_mode: Literal["affiliate", "supporting"] | None = None
    # explicit = articles.monetization_mode の明示値 /
    # legacy_derived = 明示値が無く primary link から導出
    monetization_mode_source: Literal["explicit", "legacy_derived"] | None = None
    # affiliate_ready: primary_eligible な candidate がある (affiliate mode を選べる)
    affiliate_ready: bool | None = None
    # supporting_ready: supporting mode の要件 (affiliate 以外) を満たす。比較型で比較対象の候補が
    # 0 件なら False (比較対象は内容の要件で、affiliate とは別)
    supporting_ready: bool | None = None
    supporting_blockers: list[str] = Field(default_factory=list)
    # mode に関わらない、affiliate 以外の blocker (既存 article / 記事タイプ未確定 / template 無し)
    production_blockers: list[str] = Field(default_factory=list)
    # 承認時に acknowledge が必要な項目 (blocker ではない)
    acknowledgements_required: list[str] = Field(default_factory=list)
    # alias だけで strong になった program (legacy の candidates には入らない)
    alias_only_strong_programs: list[str] = Field(default_factory=list)

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
    # C2.5.8: affiliate = primary 必須 (primary_eligible) / supporting = primary 無し。
    # 省略時は request から決める: primary があれば affiliate、無ければ supporting (後方互換)
    monetization_mode: Literal["affiliate", "supporting"] | None = None
    # C3: 人が確定する記事タイプ。省略時は keyword からの推論を採用する (後方互換)。
    # 推論できない keyword (料金 / 無料 / ブランド名など) はここで明示する。
    article_type: ArticleType | None = None
    # C3: affiliate 案件に裏付けられない比較対象 (app/config/content_subjects.json の key)
    content_subject_keys: list[str] = Field(default_factory=list)
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
    # C2.5.8 (追加): 実効 mode。supporting の affiliate 無しは設計どおり (error でない)
    monetization_mode: Literal["affiliate", "supporting"] | None = None
    monetization_mode_source: Literal["explicit", "legacy_derived"] | None = None
    # present = primary の affiliate がある / absent_by_design = supporting (affiliate 収益化なし)
    affiliate_monetization: Literal["present", "absent_by_design"] | None = None
    # 記事タイプが比較対象 (subject) を必要とするか (affiliate とは別の内容要件)
    comparison_subjects_required: bool | None = None
    comparison_subject_count: int | None = None
    # C3 (追加): 比較対象の内訳。affiliate link に依存しない編集 subject を含む
    comparison_subject_names: list[str] = Field(default_factory=list)
    # persisted = 承認時に固定した選択 / legacy_affiliate_links = link からの fallback
    comparison_subject_source: Literal["persisted", "legacy_affiliate_links"] | None = None
    non_affiliate_subject_count: int | None = None


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
    # C2.5.8 (追加): 実効 mode。明示値のときだけ payload / content_hash にも入る
    monetization_mode: Literal["affiliate", "supporting"] | None = None
    monetization_mode_source: Literal["explicit", "legacy_derived"] | None = None


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


# --- ArticleDraftPromotion (Human 承認 draft の採用記録) ---------------------


class DraftPromotionPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_run_id: int
    body_markdown: str = Field(min_length=1)
    meta_description: str = Field(min_length=1, max_length=400)


class DraftPromotionGates(BaseModel):
    article_exists: bool
    article_status_ok: bool
    article_body_empty: bool
    article_meta_empty: bool
    source_run_exists: bool
    source_run_belongs_to_article: bool
    source_run_succeeded: bool
    source_run_prompt_hash_ok: bool
    source_run_rendered_hash_ok: bool
    candidate_parses: bool
    candidate_validation_pass: bool
    candidate_promotion_eligible: bool


class DraftPromotionPreviewResponse(BaseModel):
    article_id: int
    source_run_id: int
    body_hash: str
    meta_hash: str
    candidate_content_hash: str
    body_chars: int
    meta_chars: int
    validation_report: dict
    source_run_status: str
    source_prompt_input_hash: str
    source_rendered_prompt_hash: str
    article_status: str
    can_promote: bool
    gates: DraftPromotionGates


class DraftPromotionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_run_id: int
    body_markdown: str = Field(min_length=1)
    meta_description: str = Field(min_length=1, max_length=400)
    expected_body_hash: str = Field(min_length=64, max_length=64)
    expected_meta_hash: str = Field(min_length=64, max_length=64)
    expected_candidate_content_hash: str = Field(min_length=64, max_length=64)
    idempotency_key: str | None = Field(default=None, max_length=64)
    human_review_notes: list[str] | None = None


class DraftPromotionSummaryRead(BaseModel):
    id: int
    article_id: int
    source_run_id: int
    source_prompt_input_hash: str
    source_rendered_prompt_hash: str
    body_hash: str
    meta_hash: str
    candidate_content_hash: str
    body_chars: int
    meta_chars: int
    validation_overall: str | None
    promotion_eligible: bool | None
    idempotency_key: str | None
    promoted_at: datetime
    created_at: datetime


class DraftPromotionRead(DraftPromotionSummaryRead):
    body_markdown: str
    meta_description: str
    validation_report: dict
    human_review_notes: list[str] | None


class DraftPromotionCreateResponse(BaseModel):
    promotion: DraftPromotionRead
    article_status: str
    already_promoted: bool
