"""WordPress dry-run preview の read-only オーケストレーション。

DB write 0 / WordPress call 0 / LLM 0。Human 承認済み canonical ``Article.body`` を
変更せず、deterministic HTML へ render し、pre-publication validator を通し、
WordPress へ渡す予定の値 (すべて ``post_status="draft"``) を返すだけ。
"""

from __future__ import annotations

from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.article.draft_promotion_canonical import compute_text_hash
from app.config.settings import get_settings
from app.exceptions import EntityNotFoundError, RenderedCandidateChangedError
from app.repositories.article_draft_promotion_repository import (
    ArticleDraftPromotionRepository,
)
from app.repositories.article_repository import ArticleRepository
from app.wordpress.draft_request import (
    ENDPOINT_PATH,
    METHOD,
    V1_POST_STATUS,
    build_wordpress_draft_request,
)
from app.wordpress.publication_validator import validate_wordpress_publication_preview
from app.wordpress.renderer import render_wordpress_html
from app.wordpress.schemas import (
    WordPressDraftRequestPreviewResponse,
    WordPressPreviewResponse,
)

# promotion / source run が取れない場合の fallback (validator は promotion_exists で fail)。
_FALLBACK_TOOLS = [
    "Make", "HubSpot", "ClickUp", "monday.com", "Pipedrive", "Reclaim.ai", "Todoist",
]
_FALLBACK_DOMAINS = {
    "www.make.com", "www.hubspot.com", "clickup.com", "monday.com",
    "www.pipedrive.com", "reclaim.ai", "todoist.com",
}

_V1_POST_STATUS = "draft"


class WordPressPreviewService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._articles = ArticleRepository(session)
        self._promotions = ArticleDraftPromotionRepository(session)

    def preview(self, article_id: int) -> WordPressPreviewResponse:
        article = self._articles.get_by_id(article_id)
        if article is None:
            raise EntityNotFoundError("Article", article_id)

        promotions = self._promotions.list_by_article(article_id)
        promotion = promotions[0] if promotions else None

        expected_tools, allowed_domains = self._package_meta(promotion)

        rendered = render_wordpress_html(article.body or "")

        canonical_body_hash = compute_text_hash(article.body or "")
        canonical_meta_hash = compute_text_hash(article.meta_description or "")

        slug_is_ascii = (article.slug or "").isascii()
        slug_warning = (
            None
            if slug_is_ascii
            else (
                "slug が非 ASCII (日本語)。WordPress URL では percent-encode される。"
                "初回 WordPress draft 作成前に Human が最終決定すること。"
                "(publication fail ではない)"
            )
        )

        affiliate_substitutions: list[dict] = []  # V1: tracking_url が無いため 0
        internal_link_substitutions: list[dict] = []  # V1: sibling 記事なし

        report = validate_wordpress_publication_preview(
            article_status=str(article.status),
            article_title=article.title or "",
            article_slug=article.slug or "",
            article_body=article.body or "",
            article_meta_description=article.meta_description or "",
            article_published_url=article.published_url,
            article_wordpress_post_id=article.wordpress_post_id,
            article_published_at=article.published_at,
            promotion=promotion,
            rendered_html=rendered.html,
            rendered_h1_count=rendered.h1_count,
            rendered_h2_count=rendered.h2_count,
            rendered_h3_count=rendered.h3_count,
            rendered_table_count=rendered.table_count,
            rendered_external_links=rendered.external_links,
            expected_tool_names=expected_tools,
            allowed_external_domains=allowed_domains,
            affiliate_substitution_count=len(affiliate_substitutions),
            internal_link_substitution_count=len(internal_link_substitutions),
        )

        return WordPressPreviewResponse(
            article_id=article_id,
            source_promotion_id=(promotion.id if promotion is not None else None),
            target_title=article.title or "",
            target_slug=article.slug or "",
            slug_is_ascii=slug_is_ascii,
            slug_review_warning=slug_warning,
            target_post_status=_V1_POST_STATUS,  # 常に draft (§23)
            canonical_body_hash=canonical_body_hash,
            canonical_meta_hash=canonical_meta_hash,
            renderer_version=rendered.renderer_version,
            rendered_content=rendered.html,
            rendered_content_hash=rendered.html_hash,
            rendered_content_chars=len(rendered.html),
            rendered_h1_count=rendered.h1_count,
            rendered_h2_count=rendered.h2_count,
            rendered_h3_count=rendered.h3_count,
            rendered_table_count=rendered.table_count,
            rendered_image_count=rendered.image_count,
            wp_excerpt=article.meta_description or "",
            seo_meta_integration_supported=False,
            wordpress_configured=get_settings().wordpress_configured,
            affiliate_substitutions=affiliate_substitutions,
            internal_link_substitutions=internal_link_substitutions,
            external_links=list(rendered.external_links),
            external_link_domains=sorted(
                {urlparse(u).netloc for u in rendered.external_links}
            ),
            featured_image=None,
            inline_image_count=rendered.image_count,
            image_blocker=False,
            validation_report=report,
            publishable=report["publishable"],
        )

    # -- draft-create request preview (read-only, no network) --------
    def draft_request_preview(
        self,
        article_id: int,
        *,
        expected_renderer_version: str | None = None,
        expected_rendered_content_hash: str | None = None,
    ) -> WordPressDraftRequestPreviewResponse:
        pv = self.preview(article_id)  # 承認済み publication preview path を再利用

        if (
            expected_renderer_version is not None
            and expected_renderer_version != pv.renderer_version
        ):
            raise RenderedCandidateChangedError(
                "expected_renderer_version",
                expected_renderer_version,
                pv.renderer_version,
            )
        if (
            expected_rendered_content_hash is not None
            and expected_rendered_content_hash != pv.rendered_content_hash
        ):
            raise RenderedCandidateChangedError(
                "expected_rendered_content_hash",
                expected_rendered_content_hash,
                pv.rendered_content_hash,
            )

        blocking = [
            c["id"]
            for c in pv.validation_report["checks"]
            if c["level"] == "fail"
        ]
        # sendable な request package は全 gate pass 時のみ組む (§15)。
        sendable = (
            pv.publishable
            and pv.target_post_status == V1_POST_STATUS
            and pv.source_promotion_id is not None
            and not pv.affiliate_substitutions
            and not pv.internal_link_substitutions
        )

        payload = None
        payload_keys = ["title", "content", "excerpt", "slug", "status"]
        payload_hash = None
        request_identity_hash = None
        if sendable:
            req = build_wordpress_draft_request(
                article_id=article_id,
                source_promotion_id=pv.source_promotion_id,
                title=pv.target_title,
                content=pv.rendered_content,
                excerpt=pv.wp_excerpt,
                slug=pv.target_slug,
                canonical_body_hash=pv.canonical_body_hash,
                canonical_meta_hash=pv.canonical_meta_hash,
                renderer_version=pv.renderer_version,
                rendered_content_hash=pv.rendered_content_hash,
            )
            payload = req.payload_dict()
            payload_keys = list(payload.keys())
            payload_hash = req.payload_hash
            request_identity_hash = req.request_identity_hash

        return WordPressDraftRequestPreviewResponse(
            article_id=article_id,
            source_promotion_id=pv.source_promotion_id,
            method=METHOD,
            endpoint_path=ENDPOINT_PATH,
            target_post_status=V1_POST_STATUS,
            title=pv.target_title,
            slug=pv.target_slug,
            excerpt=pv.wp_excerpt,
            excerpt_chars=len(pv.wp_excerpt),
            rendered_content=pv.rendered_content,
            rendered_content_chars=pv.rendered_content_chars,
            canonical_body_hash=pv.canonical_body_hash,
            canonical_meta_hash=pv.canonical_meta_hash,
            renderer_version=pv.renderer_version,
            rendered_content_hash=pv.rendered_content_hash,
            payload=payload,
            payload_keys=payload_keys,
            payload_hash=payload_hash,
            request_identity_hash=request_identity_hash,
            publication_validation_report=pv.validation_report,
            publishable=pv.publishable,
            blocking_reasons=blocking,
            wordpress_configured=get_settings().wordpress_configured,
        )

    @staticmethod
    def _package_meta(promotion) -> tuple[list[str], set[str]]:
        run = getattr(promotion, "source_run", None) if promotion is not None else None
        pkg = getattr(run, "prompt_package", None) if run is not None else None
        if not pkg:
            return list(_FALLBACK_TOOLS), set(_FALLBACK_DOMAINS)
        tools = pkg.get("comparison_tools", [])
        names = [t["subject_ref"] for t in tools]
        domains: set[str] = set()
        for t in tools:
            for f in t.get("usable_facts", []):
                if f.get("fact_key") == "official_url" and f.get("value"):
                    domains.add(urlparse(f["value"]).netloc)
                src = f.get("source") or {}
                if src.get("source_url"):
                    domains.add(urlparse(src["source_url"]).netloc)
        return names, domains
