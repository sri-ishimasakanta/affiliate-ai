"""Article の検証済み事実 (ArticleFact) と FactPack の REST エンドポイント。

ArticleFact は immutable 履歴のため PATCH / DELETE は提供しない
(「更新」は新しい checked_at の行を POST する)。FactPack は read-only の導出物。
"""

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.dependencies import ArticleFactServiceDep, FactPackServiceDep
from app.article.schemas import ArticleFactCreate, ArticleFactRead, FactPackDTO

router = APIRouter(prefix="/articles", tags=["article-facts"])


@router.get(
    "/{article_id}/facts",
    response_model=list[ArticleFactRead],
    status_code=status.HTTP_200_OK,
    summary="記事の事実を取得する (?subject_ref= / ?fact_key= / ?latest=true)",
)
def list_article_facts(
    article_id: int,
    service: ArticleFactServiceDep,
    subject_ref: Annotated[str | None, Query()] = None,
    fact_key: Annotated[str | None, Query()] = None,
    latest: Annotated[bool, Query()] = False,
) -> list[ArticleFactRead]:
    return service.list_facts(
        article_id, subject_ref=subject_ref, fact_key=fact_key, latest=latest
    )


@router.get(
    "/{article_id}/facts/{fact_id}",
    response_model=ArticleFactRead,
    status_code=status.HTTP_200_OK,
    summary="事実を 1 件取得する",
)
def get_article_fact(
    article_id: int, fact_id: int, service: ArticleFactServiceDep
) -> ArticleFactRead:
    return service.get_fact(article_id, fact_id)


@router.post(
    "/{article_id}/facts",
    response_model=ArticleFactRead,
    status_code=status.HTTP_201_CREATED,
    summary="検証済み事実を append する (immutable。exact duplicate は既存を返す)",
)
def create_article_fact(
    article_id: int, payload: ArticleFactCreate, service: ArticleFactServiceDep
) -> ArticleFactRead:
    return service.create_fact(article_id, payload)


@router.get(
    "/{article_id}/fact-pack",
    response_model=FactPackDTO,
    status_code=status.HTTP_200_OK,
    summary="FactPack (Source/Fact の最新状態 + readiness) を導出する (read-only)",
)
def get_article_fact_pack(
    article_id: int, service: FactPackServiceDep
) -> FactPackDTO:
    return service.build(article_id)
