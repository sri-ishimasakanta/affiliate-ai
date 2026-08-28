"""API v1 のルータ集約。"""

from fastapi import APIRouter

from app.api.v1 import affiliate_programs, articles, keywords

router = APIRouter()
router.include_router(keywords.router)
router.include_router(articles.router)
router.include_router(affiliate_programs.router)
