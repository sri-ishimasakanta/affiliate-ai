"""Keyword REST エンドポイント (/api/v1/keywords)。

Router の責務は HTTP 入出力・DI・Service 呼び出し・レスポンス返却のみ。
"""

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.dependencies import (
    KeywordMetricsCollectionServiceDep,
    KeywordScoringServiceDep,
    KeywordServiceDep,
    KeywordSignalServiceDep,
)
from app.keyword.schemas import (
    CompetitionEaseManualCreate,
    KeywordCreate,
    KeywordRead,
    KeywordScoreCreate,
    KeywordScoreRead,
    KeywordSignalCreate,
    KeywordSignalRead,
    KeywordStatusUpdate,
    KeywordUpdate,
)
from app.models.enums import KeywordSignalComponent

router = APIRouter(prefix="/keywords", tags=["keywords"])


@router.post(
    "",
    response_model=KeywordRead,
    status_code=status.HTTP_201_CREATED,
    summary="キーワードを作成する",
)
def create_keyword(
    payload: KeywordCreate,
    service: KeywordServiceDep,
) -> KeywordRead:
    return service.create_keyword(payload)


@router.get(
    "",
    response_model=list[KeywordRead],
    status_code=status.HTTP_200_OK,
    summary="キーワード一覧を取得する",
)
def list_keywords(
    service: KeywordServiceDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[KeywordRead]:
    return service.list_keywords(limit=limit, offset=offset)


@router.get(
    "/{keyword_id}",
    response_model=KeywordRead,
    status_code=status.HTTP_200_OK,
    summary="キーワードを 1 件取得する",
)
def get_keyword(
    keyword_id: int,
    service: KeywordServiceDep,
) -> KeywordRead:
    return service.get_keyword(keyword_id)


@router.patch(
    "/{keyword_id}",
    response_model=KeywordRead,
    status_code=status.HTTP_200_OK,
    summary="キーワードを部分更新する",
)
def update_keyword(
    keyword_id: int,
    payload: KeywordUpdate,
    service: KeywordServiceDep,
) -> KeywordRead:
    return service.update_keyword(keyword_id, payload)


@router.delete(
    "/{keyword_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="キーワードを削除する",
)
def delete_keyword(
    keyword_id: int,
    service: KeywordServiceDep,
) -> None:
    service.delete_keyword(keyword_id)


@router.patch(
    "/{keyword_id}/status",
    response_model=KeywordRead,
    status_code=status.HTTP_200_OK,
    summary="キーワードの status を変更する",
)
def change_keyword_status(
    keyword_id: int,
    payload: KeywordStatusUpdate,
    service: KeywordServiceDep,
) -> KeywordRead:
    return service.change_status(keyword_id, payload.status)


# -- Opportunity Score (Phase 2A) ------------------------------------------
@router.post(
    "/{keyword_id}/scores",
    response_model=KeywordScoreRead,
    status_code=status.HTTP_201_CREATED,
    summary="キーワードの Opportunity Score を計算・保存する",
)
def create_keyword_score(
    keyword_id: int,
    payload: KeywordScoreCreate,
    service: KeywordScoringServiceDep,
) -> KeywordScoreRead:
    return service.score_keyword(keyword_id, payload)


@router.get(
    "/{keyword_id}/scores/latest",
    response_model=KeywordScoreRead,
    status_code=status.HTTP_200_OK,
    summary="キーワードの最新 Opportunity Score を取得する",
)
def get_latest_keyword_score(
    keyword_id: int,
    service: KeywordScoringServiceDep,
) -> KeywordScoreRead:
    return service.get_latest_score(keyword_id)


@router.get(
    "/{keyword_id}/scores",
    response_model=list[KeywordScoreRead],
    status_code=status.HTTP_200_OK,
    summary="キーワードの Opportunity Score 履歴を取得する",
)
def list_keyword_scores(
    keyword_id: int,
    service: KeywordScoringServiceDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[KeywordScoreRead]:
    return service.list_score_history(keyword_id, limit=limit, offset=offset)


@router.post(
    "/{keyword_id}/scores/from-signals",
    response_model=KeywordScoreRead,
    status_code=status.HTTP_201_CREATED,
    summary="最新 7 Signal から Opportunity Score を計算・保存する",
)
def create_keyword_score_from_signals(
    keyword_id: int,
    service: KeywordScoringServiceDep,
) -> KeywordScoreRead:
    return service.score_keyword_from_latest_signals(keyword_id)


@router.get(
    "/{keyword_id}/scores/{score_id}/signals",
    response_model=list[KeywordSignalRead],
    status_code=status.HTTP_200_OK,
    summary="Opportunity Score の計算に使った Signal 一覧を取得する",
)
def list_keyword_score_signals(
    keyword_id: int,
    score_id: int,
    service: KeywordScoringServiceDep,
) -> list[KeywordSignalRead]:
    return service.list_score_signals(keyword_id, score_id)


# -- Keyword Signal (Phase 2B-1) -----------------------------------------
@router.post(
    "/{keyword_id}/signals",
    response_model=KeywordSignalRead,
    status_code=status.HTTP_201_CREATED,
    summary="キーワードの Signal (根拠データ) を追加する",
)
def create_keyword_signal(
    keyword_id: int,
    payload: KeywordSignalCreate,
    service: KeywordSignalServiceDep,
) -> KeywordSignalRead:
    return service.create_signal(keyword_id, payload)


@router.post(
    "/{keyword_id}/signals/site-relevance",
    response_model=KeywordSignalRead,
    status_code=status.HTTP_201_CREATED,
    summary="サイト profile から site_relevance Signal をローカル導出する (body なし)",
)
def derive_site_relevance_signal(
    keyword_id: int,
    service: KeywordSignalServiceDep,
) -> KeywordSignalRead:
    return service.derive_site_relevance(keyword_id)


@router.post(
    "/{keyword_id}/signals/affiliate-opportunity",
    response_model=KeywordSignalRead,
    status_code=status.HTTP_201_CREATED,
    summary="ローカル Affiliate Catalog から affiliate_opportunity Signal を導出する (body なし)",
)
def derive_affiliate_opportunity_signal(
    keyword_id: int,
    service: KeywordSignalServiceDep,
) -> KeywordSignalRead:
    return service.derive_affiliate_opportunity(keyword_id)


@router.post(
    "/{keyword_id}/signals/originality",
    response_model=KeywordSignalRead,
    status_code=status.HTTP_201_CREATED,
    summary="サイト内部の既存 Keyword / Article から originality Signal を導出する (body なし)",
)
def derive_originality_signal(
    keyword_id: int,
    service: KeywordSignalServiceDep,
) -> KeywordSignalRead:
    return service.derive_originality(keyword_id)


@router.post(
    "/{keyword_id}/signals/competition-ease/manual",
    response_model=KeywordSignalRead,
    status_code=status.HTTP_201_CREATED,
    summary="手動投入した Organic SEO Keyword Difficulty から competition_ease Signal を作る",
)
def create_competition_ease_manual_signal(
    keyword_id: int,
    payload: CompetitionEaseManualCreate,
    service: KeywordSignalServiceDep,
) -> KeywordSignalRead:
    return service.derive_competition_ease_manual(keyword_id, payload)


@router.get(
    "/{keyword_id}/signals",
    response_model=list[KeywordSignalRead],
    status_code=status.HTTP_200_OK,
    summary="キーワードの Signal 履歴を取得する (新しい順)",
)
def list_keyword_signals(
    keyword_id: int,
    service: KeywordSignalServiceDep,
    component: Annotated[KeywordSignalComponent | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[KeywordSignalRead]:
    return service.list_signals(
        keyword_id, component=component, limit=limit, offset=offset
    )


@router.get(
    "/{keyword_id}/signals/{component}/latest",
    response_model=KeywordSignalRead,
    status_code=status.HTTP_200_OK,
    summary="指定 component の最新 Signal を取得する",
)
def get_latest_keyword_signal(
    keyword_id: int,
    component: KeywordSignalComponent,
    service: KeywordSignalServiceDep,
) -> KeywordSignalRead:
    return service.get_latest_signal(keyword_id, component)


# -- Google Ads collector (Phase 2B-2) ----------------------------------
@router.post(
    "/{keyword_id}/signals/google-ads/search-demand",
    response_model=KeywordSignalRead,
    status_code=status.HTTP_201_CREATED,
    summary="Google Ads から search_demand Signal を収集する (body なし)",
)
def collect_google_ads_search_demand(
    keyword_id: int,
    service: KeywordMetricsCollectionServiceDep,
) -> KeywordSignalRead:
    return service.collect_google_ads_search_demand(keyword_id)


@router.post(
    "/{keyword_id}/signals/google-ads/commercial-intent",
    response_model=KeywordSignalRead,
    status_code=status.HTTP_201_CREATED,
    summary="Google Ads + キーワード文字列から commercial_intent Signal を収集する (body なし)",
)
def collect_google_ads_commercial_intent(
    keyword_id: int,
    service: KeywordMetricsCollectionServiceDep,
) -> KeywordSignalRead:
    return service.collect_google_ads_commercial_intent(keyword_id)


@router.post(
    "/{keyword_id}/signals/google-ads/trend",
    response_model=KeywordSignalRead,
    status_code=status.HTTP_201_CREATED,
    summary="Google Ads の monthly_search_volumes から trend Signal を収集する (body なし)",
)
def collect_google_ads_trend(
    keyword_id: int,
    service: KeywordMetricsCollectionServiceDep,
) -> KeywordSignalRead:
    return service.collect_google_ads_trend(keyword_id)
