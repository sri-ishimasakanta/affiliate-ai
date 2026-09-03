"""FastAPI 依存性。

- DB Session は既存の ``get_session()`` をそのまま再利用する
  (Router 内で engine / SessionLocal を生成しない)。
- Service は Router で複雑に初期化しないよう、ここで組み立てる。
  DI フレームワークは導入しない。
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.config.database import get_session
from app.services.affiliate_program_service import AffiliateProgramService
from app.services.article_affiliate_program_service import (
    ArticleAffiliateProgramService,
)
from app.services.article_fact_service import ArticleFactService
from app.services.article_plan_service import ArticlePlanService
from app.services.article_service import ArticleService
from app.services.draft_generation_run_service import DraftGenerationRunService
from app.services.draft_input_snapshot_service import DraftInputSnapshotService
from app.services.draft_prompt_preview_service import DraftPromptPreviewService
from app.services.fact_pack_service import FactPackService
from app.services.keyword_metrics_collection_service import (
    KeywordMetricsCollectionService,
)
from app.services.keyword_scoring_service import KeywordScoringService
from app.services.keyword_service import KeywordService
from app.services.keyword_signal_service import KeywordSignalService
from app.services.source_service import SourceService

SessionDep = Annotated[Session, Depends(get_session)]


def get_keyword_service(session: SessionDep) -> KeywordService:
    return KeywordService(session)


def get_article_service(session: SessionDep) -> ArticleService:
    return ArticleService(session)


def get_article_plan_service(session: SessionDep) -> ArticlePlanService:
    return ArticlePlanService(session)


def get_article_affiliate_program_service(
    session: SessionDep,
) -> ArticleAffiliateProgramService:
    return ArticleAffiliateProgramService(session)


def get_source_service(session: SessionDep) -> SourceService:
    return SourceService(session)


def get_article_fact_service(session: SessionDep) -> ArticleFactService:
    return ArticleFactService(session)


def get_fact_pack_service(session: SessionDep) -> FactPackService:
    return FactPackService(session)


def get_draft_input_snapshot_service(
    session: SessionDep,
) -> DraftInputSnapshotService:
    return DraftInputSnapshotService(session)


def get_draft_prompt_preview_service(
    session: SessionDep,
) -> DraftPromptPreviewService:
    return DraftPromptPreviewService(session)


def get_draft_generation_run_service(
    session: SessionDep,
) -> DraftGenerationRunService:
    return DraftGenerationRunService(session)


def get_affiliate_program_service(session: SessionDep) -> AffiliateProgramService:
    return AffiliateProgramService(session)


def get_keyword_scoring_service(session: SessionDep) -> KeywordScoringService:
    return KeywordScoringService(session)


def get_keyword_signal_service(session: SessionDep) -> KeywordSignalService:
    return KeywordSignalService(session)


def get_keyword_metrics_collection_service(
    session: SessionDep,
) -> KeywordMetricsCollectionService:
    return KeywordMetricsCollectionService(session)


KeywordServiceDep = Annotated[KeywordService, Depends(get_keyword_service)]
ArticleServiceDep = Annotated[ArticleService, Depends(get_article_service)]
ArticlePlanServiceDep = Annotated[
    ArticlePlanService, Depends(get_article_plan_service)
]
ArticleAffiliateProgramServiceDep = Annotated[
    ArticleAffiliateProgramService,
    Depends(get_article_affiliate_program_service),
]
SourceServiceDep = Annotated[SourceService, Depends(get_source_service)]
ArticleFactServiceDep = Annotated[
    ArticleFactService, Depends(get_article_fact_service)
]
FactPackServiceDep = Annotated[FactPackService, Depends(get_fact_pack_service)]
DraftInputSnapshotServiceDep = Annotated[
    DraftInputSnapshotService, Depends(get_draft_input_snapshot_service)
]
DraftPromptPreviewServiceDep = Annotated[
    DraftPromptPreviewService, Depends(get_draft_prompt_preview_service)
]
DraftGenerationRunServiceDep = Annotated[
    DraftGenerationRunService, Depends(get_draft_generation_run_service)
]
AffiliateProgramServiceDep = Annotated[
    AffiliateProgramService, Depends(get_affiliate_program_service)
]
KeywordScoringServiceDep = Annotated[
    KeywordScoringService, Depends(get_keyword_scoring_service)
]
KeywordSignalServiceDep = Annotated[
    KeywordSignalService, Depends(get_keyword_signal_service)
]
KeywordMetricsCollectionServiceDep = Annotated[
    KeywordMetricsCollectionService, Depends(get_keyword_metrics_collection_service)
]
