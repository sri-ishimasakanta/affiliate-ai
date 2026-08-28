"""Article の公式 Source (観測記録) の REST エンドポイント。

Source は immutable のため PATCH は提供しない (CREATE / GET / LIST / DELETE)。
"""

from fastapi import APIRouter, status

from app.api.dependencies import SourceServiceDep
from app.article.schemas import SourceCreate, SourceRead

router = APIRouter(prefix="/articles", tags=["article-sources"])


@router.get(
    "/{article_id}/sources",
    response_model=list[SourceRead],
    status_code=status.HTTP_200_OK,
    summary="記事の公式 Source 一覧を取得する",
)
def list_article_sources(article_id: int, service: SourceServiceDep) -> list[SourceRead]:
    return service.list_by_article(article_id)


@router.post(
    "/{article_id}/sources",
    response_model=SourceRead,
    status_code=status.HTTP_201_CREATED,
    summary="公式ページの観測記録を登録する (URL safety を検証)",
)
def create_article_source(
    article_id: int, payload: SourceCreate, service: SourceServiceDep
) -> SourceRead:
    return service.create(article_id, payload)


@router.get(
    "/{article_id}/sources/{source_id}",
    response_model=SourceRead,
    status_code=status.HTTP_200_OK,
    summary="Source を 1 件取得する",
)
def get_article_source(
    article_id: int, source_id: int, service: SourceServiceDep
) -> SourceRead:
    return service.get(article_id, source_id)


@router.delete(
    "/{article_id}/sources/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Source を削除する (Fact から参照されている場合は 409)",
)
def delete_article_source(
    article_id: int, source_id: int, service: SourceServiceDep
) -> None:
    service.delete(article_id, source_id)
