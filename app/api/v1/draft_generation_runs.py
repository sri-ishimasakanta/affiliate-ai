"""DraftGenerationRun / DraftPromptPackage の REST エンドポイント。

- ``POST .../draft-generation-preview``                : read-only prompt preview
- ``POST .../draft-generation-runs``                   : prepare (LLM 呼ばない)
- ``POST .../draft-generation-runs/{id}/execute``      : prepared→running (manual は外部 call なし)
- ``POST .../draft-generation-runs/{id}/submit-result``: manual 出力を parse/validate
- ``GET  .../draft-generation-runs``                   : 一覧 (summary)
- ``GET  .../draft-generation-runs/{id}``              : 1 件 (prompt/output 全文)

PATCH / DELETE なし。生成成功は Article.body 採用ではない (promotion は別 phase)。
"""

from fastapi import APIRouter, status

from app.api.dependencies import (
    DraftGenerationRunServiceDep,
    DraftPromptPreviewServiceDep,
)
from app.article.schemas import (
    DraftGenerationExecuteResponse,
    DraftGenerationPrepareResponse,
    DraftGenerationPreviewRead,
    DraftGenerationPreviewRequest,
    DraftGenerationRunPrepareRequest,
    DraftGenerationRunRead,
    DraftGenerationRunSummaryRead,
    DraftGenerationSubmitResultRequest,
    DraftGenerationSubmitResultResponse,
)
from app.models import DraftGenerationRun

router = APIRouter(prefix="/articles", tags=["draft-generation"])


def _summary(run: DraftGenerationRun) -> DraftGenerationRunSummaryRead:
    report = run.validation_report or {}
    return DraftGenerationRunSummaryRead(
        id=run.id,
        article_id=run.article_id,
        snapshot_id=run.snapshot_id,
        snapshot_content_hash=run.snapshot_content_hash,
        status=run.status,
        execution_mode=run.execution_mode,
        provider=run.provider,
        model=run.model,
        prompt_template_version=run.prompt_template_version,
        prompt_builder_version=run.prompt_builder_version,
        prompt_input_hash=run.prompt_input_hash,
        rendered_prompt_hash=run.rendered_prompt_hash,
        idempotency_key=run.idempotency_key,
        validation_overall=report.get("overall"),
        promotion_eligible=report.get("promotion_eligible"),
        started_at=run.started_at,
        finished_at=run.finished_at,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _detail(run: DraftGenerationRun) -> DraftGenerationRunRead:
    return DraftGenerationRunRead(
        **_summary(run).model_dump(),
        prompt_package=run.prompt_package,
        rendered_prompt=run.rendered_prompt,
        editorial_overrides=run.editorial_overrides,
        generation_parameters=run.generation_parameters,
        raw_output=run.raw_output,
        parsed_body=run.parsed_body,
        parsed_meta_description=run.parsed_meta_description,
        generation_notes=run.generation_notes,
        validation_report=run.validation_report,
        token_usage=run.token_usage,
        error_message=run.error_message,
    )


@router.post(
    "/{article_id}/draft-generation-preview",
    response_model=DraftGenerationPreviewRead,
    status_code=status.HTTP_200_OK,
    summary="draft 生成 prompt の preview (read-only、LLM 呼ばない)",
)
def preview_draft_generation(
    article_id: int,
    payload: DraftGenerationPreviewRequest,
    service: DraftPromptPreviewServiceDep,
) -> DraftGenerationPreviewRead:
    out = service.preview(
        article_id,
        snapshot_id=payload.snapshot_id,
        overrides=payload.editorial_overrides,
    )
    return DraftGenerationPreviewRead(**out)


@router.post(
    "/{article_id}/draft-generation-runs",
    response_model=DraftGenerationPrepareResponse,
    status_code=status.HTTP_201_CREATED,
    summary="draft 生成 run を prepare する (2-hash drift guard、LLM 呼ばない)",
)
def prepare_draft_generation_run(
    article_id: int,
    payload: DraftGenerationRunPrepareRequest,
    service: DraftGenerationRunServiceDep,
) -> DraftGenerationPrepareResponse:
    run, already = service.prepare(
        article_id,
        snapshot_id=payload.snapshot_id,
        expected_prompt_hash=payload.expected_prompt_hash,
        expected_rendered_prompt_hash=payload.expected_rendered_prompt_hash,
        execution_mode=payload.execution_mode,
        editorial_overrides=payload.editorial_overrides,
        provider=payload.provider,
        model=payload.model,
        generation_parameters=(
            payload.generation_parameters.model_dump(exclude_none=True)
            if payload.generation_parameters is not None
            else None
        ),
        idempotency_key=payload.idempotency_key,
    )
    return DraftGenerationPrepareResponse(run=_summary(run), already_prepared=already)


@router.post(
    "/{article_id}/draft-generation-runs/{run_id}/execute",
    response_model=DraftGenerationExecuteResponse,
    status_code=status.HTTP_200_OK,
    summary="run を running へ (manual は外部 call なし・rendered_prompt を返す)",
)
def execute_draft_generation_run(
    article_id: int,
    run_id: int,
    service: DraftGenerationRunServiceDep,
) -> DraftGenerationExecuteResponse:
    run, next_action, rendered = service.execute(article_id, run_id)
    return DraftGenerationExecuteResponse(
        run=_summary(run), next_action=next_action, rendered_prompt=rendered
    )


@router.post(
    "/{article_id}/draft-generation-runs/{run_id}/submit-result",
    response_model=DraftGenerationSubmitResultResponse,
    status_code=status.HTTP_200_OK,
    summary="manual 生成結果を提出し parse/validate する (Article.body は変更しない)",
)
def submit_draft_generation_result(
    article_id: int,
    run_id: int,
    payload: DraftGenerationSubmitResultRequest,
    service: DraftGenerationRunServiceDep,
) -> DraftGenerationSubmitResultResponse:
    run = service.submit_result(article_id, run_id, payload.raw_output)
    return DraftGenerationSubmitResultResponse(run=_detail(run))


@router.get(
    "/{article_id}/draft-generation-runs",
    response_model=list[DraftGenerationRunSummaryRead],
    status_code=status.HTTP_200_OK,
    summary="記事の draft 生成 run 一覧 (メタデータのみ)",
)
def list_draft_generation_runs(
    article_id: int, service: DraftGenerationRunServiceDep
) -> list[DraftGenerationRunSummaryRead]:
    return [_summary(r) for r in service.list_for_article(article_id)]


@router.get(
    "/{article_id}/draft-generation-runs/{run_id}",
    response_model=DraftGenerationRunRead,
    status_code=status.HTTP_200_OK,
    summary="draft 生成 run を 1 件取得する (prompt / output 全文)",
)
def get_draft_generation_run(
    article_id: int, run_id: int, service: DraftGenerationRunServiceDep
) -> DraftGenerationRunRead:
    return _detail(service.get(article_id, run_id))
