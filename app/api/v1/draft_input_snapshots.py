"""DraftInputSnapshot の REST エンドポイント。

- ``GET  /articles/{id}/draft-input-preview``          : read-only preview
- ``POST /articles/{id}/draft-input-snapshots``        : freeze (drift guard 付き)
- ``GET  /articles/{id}/draft-input-snapshots``        : 一覧 (summary)
- ``GET  /articles/{id}/draft-input-snapshots/{sid}``  : 1 件 (payload 全文)

PATCH / DELETE は提供しない (immutable)。freeze は Article.status を変更しない。
"""

from fastapi import APIRouter, status

from app.api.dependencies import DraftInputSnapshotServiceDep
from app.article.schemas import (
    DraftInputFreezeRequest,
    DraftInputFreezeResponse,
    DraftInputPreviewRead,
    DraftInputSnapshotRead,
    DraftInputSnapshotSummaryRead,
)

router = APIRouter(prefix="/articles", tags=["draft-input-snapshots"])


@router.get(
    "/{article_id}/draft-input-preview",
    response_model=DraftInputPreviewRead,
    status_code=status.HTTP_200_OK,
    summary="draft 生成入力の preview を取得する (read-only)",
)
def preview_draft_input(
    article_id: int, service: DraftInputSnapshotServiceDep
) -> DraftInputPreviewRead:
    return service.preview(article_id)


@router.post(
    "/{article_id}/draft-input-snapshots",
    response_model=DraftInputFreezeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="draft 生成入力を凍結する (expected_content_hash で drift 検知)",
)
def freeze_draft_input(
    article_id: int,
    payload: DraftInputFreezeRequest,
    service: DraftInputSnapshotServiceDep,
) -> DraftInputFreezeResponse:
    return service.freeze(article_id, payload.expected_content_hash)


@router.get(
    "/{article_id}/draft-input-snapshots",
    response_model=list[DraftInputSnapshotSummaryRead],
    status_code=status.HTTP_200_OK,
    summary="記事の draft 入力 Snapshot 一覧 (メタデータのみ)",
)
def list_draft_input_snapshots(
    article_id: int, service: DraftInputSnapshotServiceDep
) -> list[DraftInputSnapshotSummaryRead]:
    return service.list_for_article(article_id)


@router.get(
    "/{article_id}/draft-input-snapshots/{snapshot_id}",
    response_model=DraftInputSnapshotRead,
    status_code=status.HTTP_200_OK,
    summary="draft 入力 Snapshot を 1 件取得する (payload 全文)",
)
def get_draft_input_snapshot(
    article_id: int, snapshot_id: int, service: DraftInputSnapshotServiceDep
) -> DraftInputSnapshotRead:
    return service.get(article_id, snapshot_id)
