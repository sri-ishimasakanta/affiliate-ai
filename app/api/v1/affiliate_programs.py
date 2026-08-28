"""AffiliateProgram REST エンドポイント (/api/v1/affiliate-programs)。

アフィリエイト案件カタログの CRUD。Router の責務は HTTP 入出力・DI・
Service 呼び出し・レスポンス返却のみ。affiliate_opportunity Signal の
採点はここでは扱わない (Phase 2B-6B)。
"""

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.affiliate.schemas import (
    AffiliateProgramCreate,
    AffiliateProgramRead,
    AffiliateProgramUpdate,
)
from app.api.dependencies import AffiliateProgramServiceDep
from app.models.enums import AffiliateProgramStatus

router = APIRouter(prefix="/affiliate-programs", tags=["affiliate-programs"])


@router.post(
    "",
    response_model=AffiliateProgramRead,
    status_code=status.HTTP_201_CREATED,
    summary="アフィリエイト案件を作成する",
)
def create_affiliate_program(
    payload: AffiliateProgramCreate,
    service: AffiliateProgramServiceDep,
) -> AffiliateProgramRead:
    return service.create_program(payload)


@router.get(
    "",
    response_model=list[AffiliateProgramRead],
    status_code=status.HTTP_200_OK,
    summary="アフィリエイト案件一覧を取得する",
)
def list_affiliate_programs(
    service: AffiliateProgramServiceDep,
    program_status: Annotated[AffiliateProgramStatus | None, Query(alias="status")] = None,
    provider: Annotated[str | None, Query()] = None,
    category: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AffiliateProgramRead]:
    return service.list_programs(
        status=program_status,
        provider=provider,
        category=category,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{program_id}",
    response_model=AffiliateProgramRead,
    status_code=status.HTTP_200_OK,
    summary="アフィリエイト案件を 1 件取得する",
)
def get_affiliate_program(
    program_id: int,
    service: AffiliateProgramServiceDep,
) -> AffiliateProgramRead:
    return service.get_program(program_id)


@router.patch(
    "/{program_id}",
    response_model=AffiliateProgramRead,
    status_code=status.HTTP_200_OK,
    summary="アフィリエイト案件を部分更新する",
)
def update_affiliate_program(
    program_id: int,
    payload: AffiliateProgramUpdate,
    service: AffiliateProgramServiceDep,
) -> AffiliateProgramRead:
    return service.update_program(program_id, payload)


@router.delete(
    "/{program_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="アフィリエイト案件を削除する",
)
def delete_affiliate_program(
    program_id: int,
    service: AffiliateProgramServiceDep,
) -> None:
    service.delete_program(program_id)
