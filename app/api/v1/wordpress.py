"""WordPress dry-run preview エンドポイント。

- ``POST .../wordpress-preview`` : read-only。WordPress へは通信しない。DB write 0。

PATCH / DELETE / publish なし。post_status は常に ``draft`` (§23)。
"""

from fastapi import APIRouter, status

from app.api.dependencies import (
    WordPressDraftRunServiceDep,
    WordPressPreviewServiceDep,
    WordPressPublicationRunServiceDep,
)
from app.models import WordPressDraftRun, WordPressPublicationRun
from app.wordpress.schemas import (
    WordPressDraftRequestPreviewRequest,
    WordPressDraftRequestPreviewResponse,
    WordPressDraftRunExecuteRequest,
    WordPressDraftRunExecuteResponse,
    WordPressDraftRunPrepareRequest,
    WordPressDraftRunPrepareResponse,
    WordPressDraftRunRead,
    WordPressDraftRunSummaryRead,
    WordPressPreviewResponse,
    WordPressPublicationRunPrepareRequest,
    WordPressPublicationRunPrepareResponse,
    WordPressPublicationRunRead,
    WordPressPublicationRunSummaryRead,
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


def _wp_run_summary(run: WordPressDraftRun) -> WordPressDraftRunSummaryRead:
    return WordPressDraftRunSummaryRead(
        id=run.id,
        article_id=run.article_id,
        source_promotion_id=run.source_promotion_id,
        status=run.status,
        target_base_url=run.target_base_url,
        method=run.method,
        endpoint_path=run.endpoint_path,
        payload_hash=run.payload_hash,
        request_identity_hash=run.request_identity_hash,
        target_request_identity_hash=run.target_request_identity_hash,
        renderer_version=run.renderer_version,
        rendered_content_hash=run.rendered_content_hash,
        canonical_body_hash=run.canonical_body_hash,
        canonical_meta_hash=run.canonical_meta_hash,
        idempotency_key=run.idempotency_key,
        wordpress_post_id=run.wordpress_post_id,
        wordpress_post_status=run.wordpress_post_status,
        wordpress_post_url=run.wordpress_post_url,
        error_message=run.error_message,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _wp_run_detail(run: WordPressDraftRun) -> WordPressDraftRunRead:
    import json as _json

    try:
        keys = list(_json.loads(run.payload_json).keys())
    except Exception:
        keys = []
    return WordPressDraftRunRead(
        **_wp_run_summary(run).model_dump(),
        payload_json=run.payload_json,
        payload_keys=keys,
        response_snapshot=run.response_snapshot,
    )


@router.post(
    "/{article_id}/wordpress-draft-runs/prepare",
    response_model=WordPressDraftRunPrepareResponse,
    status_code=status.HTTP_201_CREATED,
    summary="初回 WordPress draft run を prepare する (通信なし・Human gate)",
)
def prepare_wordpress_draft_run(
    article_id: int,
    payload: WordPressDraftRunPrepareRequest,
    service: WordPressDraftRunServiceDep,
) -> WordPressDraftRunPrepareResponse:
    return service.prepare(
        article_id,
        source_promotion_id=payload.source_promotion_id,
        expected_renderer_version=payload.expected_renderer_version,
        expected_rendered_content_hash=payload.expected_rendered_content_hash,
        expected_payload_hash=payload.expected_payload_hash,
        expected_request_identity_hash=payload.expected_request_identity_hash,
        idempotency_key=payload.idempotency_key,
    )


@router.get(
    "/{article_id}/wordpress-draft-runs",
    response_model=list[WordPressDraftRunSummaryRead],
    status_code=status.HTTP_200_OK,
    summary="記事の WordPress draft run 一覧 (メタデータのみ)",
)
def list_wordpress_draft_runs(
    article_id: int, service: WordPressDraftRunServiceDep
) -> list[WordPressDraftRunSummaryRead]:
    return [_wp_run_summary(r) for r in service.list_for_article(article_id)]


@router.get(
    "/{article_id}/wordpress-draft-runs/{run_id}",
    response_model=WordPressDraftRunRead,
    status_code=status.HTTP_200_OK,
    summary="WordPress draft run を 1 件取得する (payload 全文)",
)
def get_wordpress_draft_run(
    article_id: int, run_id: int, service: WordPressDraftRunServiceDep
) -> WordPressDraftRunRead:
    return _wp_run_detail(service.get(article_id, run_id))


@router.post(
    "/{article_id}/wordpress-draft-runs/{run_id}/execute",
    response_model=WordPressDraftRunExecuteResponse,
    status_code=status.HTTP_200_OK,
    summary="prepared run を実行し、WordPress へ厳密に 1 回だけ draft 作成 POST を送る",
)
def execute_wordpress_draft_run(
    article_id: int,
    run_id: int,
    payload: WordPressDraftRunExecuteRequest,
    service: WordPressDraftRunServiceDep,
) -> WordPressDraftRunExecuteResponse:
    return service.execute(
        article_id,
        run_id,
        expected_target_request_identity_hash=payload.expected_target_request_identity_hash,
    )


# --- WordPress publication runs (既存 draft の publish; このフェーズは prepare のみ) ---


def _wp_pubrun_summary(run: WordPressPublicationRun) -> WordPressPublicationRunSummaryRead:
    return WordPressPublicationRunSummaryRead(
        id=run.id,
        article_id=run.article_id,
        source_wordpress_draft_run_id=run.source_wordpress_draft_run_id,
        status=run.status,
        target_base_url=run.target_base_url,
        wordpress_post_id=run.wordpress_post_id,
        method=run.method,
        endpoint_path=run.endpoint_path,
        publish_payload_hash=run.publish_payload_hash,
        publication_request_identity_hash=run.publication_request_identity_hash,
        target_publication_request_identity_hash=(
            run.target_publication_request_identity_hash
        ),
        canonical_body_hash=run.canonical_body_hash,
        canonical_meta_hash=run.canonical_meta_hash,
        wordpress_raw_content_hash=run.wordpress_raw_content_hash,
        expected_pre_publish_status=run.expected_pre_publish_status,
        idempotency_key=run.idempotency_key,
        wordpress_post_status=run.wordpress_post_status,
        wordpress_post_url=run.wordpress_post_url,
        published_at_source=run.published_at_source,
        error_message=run.error_message,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _wp_pubrun_detail(run: WordPressPublicationRun) -> WordPressPublicationRunRead:
    return WordPressPublicationRunRead(
        **_wp_pubrun_summary(run).model_dump(),
        publish_payload_json=run.publish_payload_json,
        response_snapshot=run.response_snapshot,
    )


@router.post(
    "/{article_id}/wordpress-publication-runs",
    response_model=WordPressPublicationRunPrepareResponse,
    status_code=status.HTTP_201_CREATED,
    summary="既存 WordPress draft の publish run を prepare する (通信なし・Human gate)",
)
def prepare_wordpress_publication_run(
    article_id: int,
    payload: WordPressPublicationRunPrepareRequest,
    service: WordPressPublicationRunServiceDep,
) -> WordPressPublicationRunPrepareResponse:
    return service.prepare(
        article_id,
        source_wordpress_draft_run_id=payload.source_wordpress_draft_run_id,
        expected_wordpress_post_id=payload.expected_wordpress_post_id,
        expected_target_request_identity_hash=payload.expected_target_request_identity_hash,
        wordpress_raw_content_hash=payload.wordpress_raw_content_hash,
        idempotency_key=payload.idempotency_key,
    )


@router.get(
    "/{article_id}/wordpress-publication-runs",
    response_model=list[WordPressPublicationRunSummaryRead],
    status_code=status.HTTP_200_OK,
    summary="記事の WordPress publication run 一覧 (メタデータのみ)",
)
def list_wordpress_publication_runs(
    article_id: int, service: WordPressPublicationRunServiceDep
) -> list[WordPressPublicationRunSummaryRead]:
    return [_wp_pubrun_summary(r) for r in service.list_for_article(article_id)]


@router.get(
    "/{article_id}/wordpress-publication-runs/{run_id}",
    response_model=WordPressPublicationRunRead,
    status_code=status.HTTP_200_OK,
    summary="WordPress publication run を 1 件取得する (payload 全文)",
)
def get_wordpress_publication_run(
    article_id: int, run_id: int, service: WordPressPublicationRunServiceDep
) -> WordPressPublicationRunRead:
    return _wp_pubrun_detail(service.get(article_id, run_id))
