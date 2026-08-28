"""Article REST エンドポイント (/api/v1/articles)。

Router の責務は HTTP 入出力・DI・Service 呼び出し・レスポンス返却のみ。
"""

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.dependencies import ArticleServiceDep
from app.article.schemas import (
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
