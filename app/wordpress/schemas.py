"""WordPress dry-run preview の入出力スキーマ。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


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
