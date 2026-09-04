"""API v1 のルータ集約。"""

from fastapi import APIRouter

from app.api.v1 import (
    affiliate_programs,
    article_facts,
    article_plans,
    article_sources,
    articles,
    draft_generation_runs,
    draft_input_snapshots,
    draft_promotions,
    keywords,
)

router = APIRouter()
router.include_router(keywords.router)
router.include_router(article_plans.router)
router.include_router(articles.router)
router.include_router(article_sources.router)
router.include_router(article_facts.router)
router.include_router(affiliate_programs.router)
router.include_router(draft_input_snapshots.router)
router.include_router(draft_generation_runs.router)
router.include_router(draft_promotions.router)
