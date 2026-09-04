"""WordPress dry-run preview エンドポイント。

- ``POST .../wordpress-preview`` : read-only。WordPress へは通信しない。DB write 0。

PATCH / DELETE / publish なし。post_status は常に ``draft`` (§23)。
"""

from fastapi import APIRouter, status

from app.api.dependencies import WordPressPreviewServiceDep
from app.wordpress.schemas import (
    WordPressDraftRequestPreviewRequest,
    WordPressDraftRequestPreviewResponse,
    WordPressPreviewResponse,
)

router = APIRouter(prefix="/articles", tags=["wordpress"])


@router.post(
    "/{article_id}/wordpress-preview",
    response_model=WordPressPreviewResponse,
    status_code=status.HTTP_200_OK,
    summary="WordPress 投稿の dry-run preview (read-only、WordPress 通信なし)",
)
def wordpress_preview(
    article_id: int,
    service: WordPressPreviewServiceDep,
) -> WordPressPreviewResponse:
    return service.preview(article_id)


@router.post(
    "/{article_id}/wordpress-draft-request-preview",
    response_model=WordPressDraftRequestPreviewResponse,
    status_code=status.HTTP_200_OK,
    summary="WordPress draft-create request の dry-run preview (read-only、通信なし)",
)
def wordpress_draft_request_preview(
    article_id: int,
    payload: WordPressDraftRequestPreviewRequest,
    service: WordPressPreviewServiceDep,
) -> WordPressDraftRequestPreviewResponse:
    return service.draft_request_preview(
        article_id,
        expected_renderer_version=payload.expected_renderer_version,
        expected_rendered_content_hash=payload.expected_rendered_content_hash,
    )
