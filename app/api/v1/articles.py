"""Article REST エンドポイント (/api/v1/articles)。

Router の責務は HTTP 入出力・DI・Service 呼び出し・レスポンス返却のみ。
"""

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.dependencies import ArticleAffiliateProgramServiceDep, ArticleServiceDep
from app.article.schemas import (
    ArticleAffiliateProgramCreate,
    ArticleAffiliateProgramRead,
    ArticleAffiliateProgramUpdate,
    ArticleCreate,
    ArticleRead,
    ArticleStatusUpdate,
    ArticleUpdate,
)

router = APIRouter(prefix="/articles", tags=["articles"])


@router.post(
    "",
    response_model=ArticleRead,
    status_code=status.HTTP_201_CREATED,
    summary="記事を作成する",
)
def create_article(
    payload: ArticleCreate,
    service: ArticleServiceDep,
) -> ArticleRead:
    return service.create_article(payload)


@router.get(
    "",
    response_model=list[ArticleRead],
    status_code=status.HTTP_200_OK,
    summary="記事一覧を取得する",
)
def list_articles(
    service: ArticleServiceDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ArticleRead]:
    return service.list_articles(limit=limit, offset=offset)


@router.get(
    "/{article_id}",
    response_model=ArticleRead,
    status_code=status.HTTP_200_OK,
    summary="記事を 1 件取得する",
)
def get_article(
    article_id: int,
    service: ArticleServiceDep,
) -> ArticleRead:
    return service.get_article(article_id)


@router.patch(
    "/{article_id}",
    response_model=ArticleRead,
    status_code=status.HTTP_200_OK,
    summary="記事を部分更新する",
)
def update_article(
    article_id: int,
    payload: ArticleUpdate,
    service: ArticleServiceDep,
) -> ArticleRead:
    return service.update_article(article_id, payload)


@router.delete(
    "/{article_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="記事を削除する",
)
def delete_article(
    article_id: int,
    service: ArticleServiceDep,
) -> None:
    service.delete_article(article_id)


@router.patch(
    "/{article_id}/status",
    response_model=ArticleRead,
    status_code=status.HTTP_200_OK,
    summary="記事の status を変更する",
)
def change_article_status(
    article_id: int,
    payload: ArticleStatusUpdate,
    service: ArticleServiceDep,
) -> ArticleRead:
    return service.change_status(article_id, payload.status)


# --- 記事 × 広告案件の紐付け (中間モデル操作) --------------------------------
# planned 段階での relation 登録は可。tracking URL の本文挿入 (link injection) は
# approved 後の後続 Phase の責務であり、ここでは行わない。


@router.get(
    "/{article_id}/affiliate-programs",
    response_model=list[ArticleAffiliateProgramRead],
    status_code=status.HTTP_200_OK,
    summary="記事に紐付いた広告案件の一覧を取得する",
)
def list_article_affiliate_programs(
    article_id: int,
    service: ArticleAffiliateProgramServiceDep,
) -> list[ArticleAffiliateProgramRead]:
    return service.list_by_article(article_id)


@router.post(
    "/{article_id}/affiliate-programs",
    response_model=ArticleAffiliateProgramRead,
    status_code=status.HTTP_201_CREATED,
    summary="記事に広告案件を紐付ける (同一案件の重複は 409)",
)
def attach_article_affiliate_program(
    article_id: int,
    payload: ArticleAffiliateProgramCreate,
    service: ArticleAffiliateProgramServiceDep,
) -> ArticleAffiliateProgramRead:
    return service.attach(article_id, payload)


@router.patch(
    "/{article_id}/affiliate-programs/{link_id}",
    response_model=ArticleAffiliateProgramRead,
    status_code=status.HTTP_200_OK,
    summary="紐付けを更新する (primary=true は同一記事で最大 1 件に正規化)",
)
def update_article_affiliate_program(
    article_id: int,
    link_id: int,
    payload: ArticleAffiliateProgramUpdate,
    service: ArticleAffiliateProgramServiceDep,
) -> ArticleAffiliateProgramRead:
    return service.update_link(article_id, link_id, payload)


@router.delete(
    "/{article_id}/affiliate-programs/{link_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="記事から広告案件の紐付けを外す",
)
def detach_article_affiliate_program(
    article_id: int,
    link_id: int,
    service: ArticleAffiliateProgramServiceDep,
) -> None:
    service.detach(article_id, link_id)
