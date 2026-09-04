"""WordPress dry-run preview の入出力スキーマ。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class WordPressPreviewResponse(BaseModel):
    article_id: int
    source_promotion_id: int | None

    target_title: str
    target_slug: str
    slug_is_ascii: bool
    slug_review_warning: str | None
    target_post_status: str  # V1 は常に "draft"

    canonical_body_hash: str
    canonical_meta_hash: str

    renderer_version: str
    rendered_content: str
    rendered_content_hash: str
    rendered_content_chars: int

    rendered_h1_count: int
    rendered_h2_count: int
    rendered_h3_count: int
    rendered_table_count: int
    rendered_image_count: int

    wp_excerpt: str
    seo_meta_integration_supported: bool
    wordpress_configured: bool

    affiliate_substitutions: list[dict]
    internal_link_substitutions: list[dict]
    external_links: list[str]
    external_link_domains: list[str]

    featured_image: str | None
    inline_image_count: int
    image_blocker: bool

    validation_report: dict
    publishable: bool


class WordPressDraftRequestPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Human が HTML を承認した時点の renderer identity。指定時は現在の render 結果と
    # 一致しなければ 409 (rendered_candidate_changed)。
    expected_renderer_version: str | None = None
    expected_rendered_content_hash: str | None = None


class WordPressDraftRequestPreviewResponse(BaseModel):
    article_id: int
    source_promotion_id: int | None

    method: str
    endpoint_path: str
    target_post_status: str

    title: str
    slug: str
    excerpt: str
    excerpt_chars: int
    rendered_content: str
    rendered_content_chars: int

    canonical_body_hash: str
    canonical_meta_hash: str
    renderer_version: str
    rendered_content_hash: str

    payload: dict | None
    payload_keys: list[str]
    payload_hash: str | None
    request_identity_hash: str | None

    publication_validation_report: dict
    publishable: bool
    blocking_reasons: list[str]
    wordpress_configured: bool


# --- WordPressDraftRun (初回 draft 作成の実行記録) -------------------------


class WordPressDraftRunPrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_promotion_id: int
    expected_renderer_version: str = Field(min_length=1)
    expected_rendered_content_hash: str = Field(min_length=64, max_length=64)
    expected_payload_hash: str = Field(min_length=64, max_length=64)
    expected_request_identity_hash: str = Field(min_length=64, max_length=64)
    idempotency_key: str | None = Field(default=None, max_length=128)
    # target_base_url は信頼できるローカル設定 (Settings.wordpress_base_url) からのみ。
    # credential は受け付けない (extra="forbid")。


class WordPressDraftRunPrepareResponse(BaseModel):
    run_id: int
    status: str
    already_prepared: bool

    article_id: int
    source_promotion_id: int

    target_base_url: str
    method: str
    endpoint_path: str

    payload_hash: str
    request_identity_hash: str
    target_request_identity_hash: str

    canonical_body_hash: str
    canonical_meta_hash: str
    renderer_version: str
    rendered_content_hash: str

    created_at: datetime
    wordpress_configured: bool


class WordPressDraftRunSummaryRead(BaseModel):
    id: int
    article_id: int
    source_promotion_id: int
    status: str

    target_base_url: str
    method: str
    endpoint_path: str

    payload_hash: str
    request_identity_hash: str
    target_request_identity_hash: str
    renderer_version: str
    rendered_content_hash: str
    canonical_body_hash: str
    canonical_meta_hash: str

    idempotency_key: str | None
    wordpress_post_id: str | None
    wordpress_post_status: str | None
    wordpress_post_url: str | None
    error_message: str | None

    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class WordPressDraftRunRead(WordPressDraftRunSummaryRead):
    payload_json: str
    payload_keys: list[str]
    response_snapshot: dict | None


# --- WordPressDraftRun execute (実 WordPress への draft 作成) --------------


class WordPressDraftRunExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # prepare 時に Human が承認した target_request_identity_hash と一致しなければ拒否。
    expected_target_request_identity_hash: str = Field(min_length=64, max_length=64)


class WordPressDraftRunExecuteResponse(BaseModel):
    run_id: int
    status: str  # 成功時は常に "succeeded" (失敗は例外 -> HTTP エラーで返る)
    article_id: int

    target_base_url: str
    target_request_identity_hash: str

    wordpress_post_id: str
    wordpress_post_status: str
    wordpress_post_url: str | None

    started_at: datetime
    finished_at: datetime
