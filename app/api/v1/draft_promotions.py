"""ArticleDraftPromotion の REST エンドポイント。

- ``POST .../draft-promotion-preview`` : read-only (hash + validator、DB write 0)
- ``POST .../draft-promotions``        : Human 採用アクション (3-hash guard、1 transaction)
- ``GET  .../draft-promotions``        : 一覧 (summary、本文なし)
- ``GET  .../draft-promotions/{id}``   : 1 件 (本文全文)

PATCH / DELETE なし。生成 run は変更しない。汎用 Article update で代替しない。
"""

from fastapi import APIRouter, status

from app.api.dependencies import ArticleDraftPromotionServiceDep
from app.article.schemas import (
    DraftPromotionCreateRequest,
    DraftPromotionCreateResponse,
    DraftPromotionPreviewRequest,
    DraftPromotionPreviewResponse,
    DraftPromotionRead,
    DraftPromotionSummaryRead,
)
from app.models import ArticleDraftPromotion

router = APIRouter(prefix="/articles", tags=["draft-promotion"])


def _summary(row: ArticleDraftPromotion) -> DraftPromotionSummaryRead:
    report = row.validation_report or {}
    return DraftPromotionSummaryRead(
        id=row.id,
        article_id=row.article_id,
        source_run_id=row.source_run_id,
        source_prompt_input_hash=row.source_prompt_input_hash,
        source_rendered_prompt_hash=row.source_rendered_prompt_hash,
        body_hash=row.body_hash,
        meta_hash=row.meta_hash,
        candidate_content_hash=row.candidate_content_hash,
        body_chars=len(row.body_markdown),
        meta_chars=len(row.meta_description),
        validation_overall=report.get("overall"),
        promotion_eligible=report.get("promotion_eligible"),
        idempotency_key=row.idempotency_key,
        promoted_at=row.promoted_at,
        created_at=row.created_at,
    )


@router.post(
    "/{article_id}/draft-promotion-preview",
    response_model=DraftPromotionPreviewResponse,
    status_code=status.HTTP_200_OK,
    summary="draft 採用候補の preview (read-only、hash + validator)",
)
def preview_draft_promotion(
    article_id: int,
    payload: DraftPromotionPreviewRequest,
    service: ArticleDraftPromotionServiceDep,
) -> DraftPromotionPreviewResponse:
    return service.preview(
        article_id,
        source_run_id=payload.source_run_id,
        body_markdown=payload.body_markdown,
        meta_description=payload.meta_description,
    )


@router.post(
    "/{article_id}/draft-promotions",
    response_model=DraftPromotionCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Human が draft 候補を採用する (3-hash guard、Article.body/meta 書き込み)",
)
def create_draft_promotion(
    article_id: int,
    payload: DraftPromotionCreateRequest,
    service: ArticleDraftPromotionServiceDep,
) -> DraftPromotionCreateResponse:
    return service.promote(
        article_id,
        source_run_id=payload.source_run_id,
        body_markdown=payload.body_markdown,
        meta_description=payload.meta_description,
        expected_body_hash=payload.expected_body_hash,
        expected_meta_hash=payload.expected_meta_hash,
        expected_candidate_content_hash=payload.expected_candidate_content_hash,
        idempotency_key=payload.idempotency_key,
        human_review_notes=payload.human_review_notes,
    )


@router.get(
    "/{article_id}/draft-promotions",
    response_model=list[DraftPromotionSummaryRead],
    status_code=status.HTTP_200_OK,
    summary="記事の draft 採用記録 一覧 (メタデータのみ)",
)
def list_draft_promotions(
    article_id: int, service: ArticleDraftPromotionServiceDep
) -> list[DraftPromotionSummaryRead]:
    return [_summary(r) for r in service.list_for_article(article_id)]


@router.get(
    "/{article_id}/draft-promotions/{promotion_id}",
    response_model=DraftPromotionRead,
    status_code=status.HTTP_200_OK,
    summary="draft 採用記録を 1 件取得する (本文全文)",
)
def get_draft_promotion(
    article_id: int, promotion_id: int, service: ArticleDraftPromotionServiceDep
) -> DraftPromotionRead:
    return service.to_read(service.get(article_id, promotion_id))
