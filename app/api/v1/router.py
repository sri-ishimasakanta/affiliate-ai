"""API v1 のルータ集約。"""

from fastapi import APIRouter

from app.api.v1 import affiliate_programs, article_plans, articles, keywords

router = APIRouter()
router.include_router(keywords.router)
router.include_router(article_plans.router)
router.include_router(articles.router)
router.include_router(affiliate_programs.router)
