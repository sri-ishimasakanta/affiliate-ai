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
from app.services.article_service import ArticleService
from app.services.keyword_metrics_collection_service import (
    KeywordMetricsCollectionService,
)
from app.services.keyword_scoring_service import KeywordScoringService
from app.services.keyword_service import KeywordService
from app.services.keyword_signal_service import KeywordSignalService

SessionDep = Annotated[Session, Depends(get_session)]


def get_keyword_service(session: SessionDep) -> KeywordService:
    return KeywordService(session)


def get_article_service(session: SessionDep) -> ArticleService:
    return ArticleService(session)


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
