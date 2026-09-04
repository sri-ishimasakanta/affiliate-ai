"""WordPress dry-run preview の入出力スキーマ。"""

from __future__ import annotations

from pydantic import BaseModel


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
