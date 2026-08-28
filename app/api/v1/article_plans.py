"""Article Plan の REST エンドポイント。

- ``GET  /api/v1/keywords/{keyword_id}/article-plan``          : read-only の企画案
- ``POST /api/v1/keywords/{keyword_id}/article-plan/approve``  : atomic な企画承認

ArticlePlan は DB へ永続化せず、keyword から都度導出する。承認時のみ Article
(status=planned) と ArticleAffiliateProgram を 1 transaction で作成する。
"""

from fastapi import APIRouter, status

from app.api.dependencies import ArticlePlanServiceDep
from app.article.schemas import (
    ArticlePlanApproveRequest,
    ArticlePlanDTO,
    ArticleRead,
)

router = APIRouter(prefix="/keywords", tags=["article-plans"])


@router.get(
    "/{keyword_id}/article-plan",
    response_model=ArticlePlanDTO,
    status_code=status.HTTP_200_OK,
    summary="キーワードから記事企画案 (read-only) を取得する",
)
def get_article_plan(
    keyword_id: int,
    service: ArticlePlanServiceDep,
) -> ArticlePlanDTO:
    # 7 Signal 未充足でも 404/409 にせず readiness.complete=false で 200 を返す。
    return service.plan_for_keyword(keyword_id)


@router.post(
    "/{keyword_id}/article-plan/approve",
    response_model=ArticleRead,
    status_code=status.HTTP_201_CREATED,
    summary="記事企画を承認し Article(planned) と広告案件の紐付けを作成する",
)
def approve_article_plan(
    keyword_id: int,
    payload: ArticlePlanApproveRequest,
    service: ArticlePlanServiceDep,
) -> ArticleRead:
    return service.approve(keyword_id, payload)
