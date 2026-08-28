"""外部 Provider から KeywordSignal を収集するビジネスロジック。

Google Ads 固有処理 (client 生成 / API 呼び出し / DTO 変換) は
:class:`GoogleAdsKeywordMetricsProvider` に委譲し、ここには押し込まない。
KeywordSignalService とは別 Service にして責務を分ける。

書き込みは Service が commit、失敗時 rollback (既存方針を維持)。
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.config.settings import Settings, get_settings
from app.exceptions import EntityNotFoundError, ExternalProviderDataError
from app.keyword.normalizers.commercial_intent import (
    CommercialIntentResult,
    calculate_commercial_intent,
)
from app.keyword.normalizers.search_demand import (
    NORMALIZER_NAME,
    NORMALIZER_VERSION,
    normalize_search_demand,
)
from app.keyword.normalizers.trend import TrendResult, calculate_trend
from app.keyword.providers.google_ads import (
    GOOGLE_ADS_SOURCE_REFERENCE,
    GoogleAdsKeywordMetrics,
    GoogleAdsKeywordMetricsProvider,
)
from app.keyword.schemas import KeywordSignalRead
from app.models.enums import KeywordSignalComponent
from app.repositories.keyword_repository import KeywordRepository
from app.repositories.keyword_signal_repository import KeywordSignalRepository
from app.services.keyword_signal_service import to_signal_read

_KEYWORD_ENTITY = "Keyword"
_PROVIDER = "google_ads"

# 1 回の Historical Metrics fetch から導出できる Google Ads 由来 component。
GOOGLE_ADS_BUNDLE_COMPONENTS = ("search_demand", "commercial_intent", "trend")


@dataclass(frozen=True)
class BulkKeywordSignals:
    """bulk 収集の keyword ごとの結果。``created`` は component -> signal id。"""

    keyword_id: int
    keyword: str
    created: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)


def normalize_keyword_match_text(text: str) -> str:
    """Google Ads keyword 照合用の正規化: NFKC → casefold → 連続空白を単一空白へ。

    Unicode の全角 / 互換文字と大文字小文字の差を吸収する。空白は「表記の揺れ」
    として単一の半角空白へ畳むだけで、除去はしない (除去は :func:`compact_keyword_match_key`
    の役割)。pure function。
    """

    normalized = unicodedata.normalize("NFKC", text)
    return " ".join(normalized.casefold().split())


def compact_keyword_match_key(text: str) -> str:
    """:func:`normalize_keyword_match_text` からさらに空白をすべて除去した compact key。

    Google Ads は Historical Metrics 応答で CJK keyword を分かち書きし直して返す
    ことがある ("AI 議事録 おすすめ" → "ai 議事 録 おすすめ")。その「空白位置の差」
    だけを吸収するために使う。pure function。
    """

    return normalize_keyword_match_text(text).replace(" ", "")


def _match_metrics(
    metrics_list: list[GoogleAdsKeywordMetrics],
    keyword: str,
    *,
    allow_single_result_fallback: bool = True,
    allow_whitespace_insensitive_match: bool = True,
) -> GoogleAdsKeywordMetrics | None:
    """requested keyword に対応する Google Ads metrics 行を安全に 1 つ選ぶ。

    照合は「表記上の空白差」だけを吸収する目的で段階的に行う (fuzzy match / 形態素
    解析はしない):

    1. NFKC + casefold + 連続空白正規化した文字列での **完全一致**。
    2. 1 で一致しない場合のみ、空白をすべて除去した **compact key** での一致
       (``allow_whitespace_insensitive_match`` が真のときだけ)。

    どちらの段階でも複数行が同じ key に該当したら「曖昧」として ``None`` を返す
    (誤割当を避け、未取得を優先する)。どの段階でも一致しなければ、
    ``allow_single_result_fallback`` かつ応答が 1 件だけのとき従来どおりその 1 件を
    返す (単体 collector 向け。bulk collector では ``False`` を渡して無効化する)。
    """

    if not metrics_list:
        return None

    norm_target = normalize_keyword_match_text(keyword)
    normalized_hits = [
        metrics
        for metrics in metrics_list
        if normalize_keyword_match_text(metrics.keyword) == norm_target
    ]
    if len(normalized_hits) == 1:
        return normalized_hits[0]
    if len(normalized_hits) > 1:
        return None

    if allow_whitespace_insensitive_match:
        compact_target = compact_keyword_match_key(keyword)
        compact_hits = [
            metrics
            for metrics in metrics_list
            if compact_keyword_match_key(metrics.keyword) == compact_target
        ]
        if len(compact_hits) == 1:
            return compact_hits[0]
        if len(compact_hits) > 1:
            return None

    if allow_single_result_fallback and len(metrics_list) == 1:
        return metrics_list[0]
    return None


def _period_from_volumes(
    metrics: GoogleAdsKeywordMetrics,
) -> tuple[datetime | None, datetime | None]:
    months = [
        (v.year, v.month)
        for v in metrics.monthly_search_volumes
        if v.year > 0 and 1 <= v.month <= 12
    ]
    if not months:
        return None, None
    oldest_year, oldest_month = min(months)
    newest_year, newest_month = max(months)
    return (
        datetime(oldest_year, oldest_month, 1, tzinfo=UTC),
        datetime(newest_year, newest_month, 1, tzinfo=UTC),
    )


def _build_raw_data(
    metrics: GoogleAdsKeywordMetrics,
    *,
    settings: Settings,
) -> dict[str, Any]:
    """Google Ads 指標 + normalizer metadata を JSON-safe な primitive dict で返す。"""

    return {
        "avg_monthly_searches": metrics.avg_monthly_searches,
        "monthly_search_volumes": [
            {
                "year": v.year,
                "month": v.month,
                "monthly_searches": v.monthly_searches,
            }
            for v in metrics.monthly_search_volumes
        ],
        "competition": metrics.competition,
        "competition_index": metrics.competition_index,
        "low_top_of_page_bid_micros": metrics.low_top_of_page_bid_micros,
        "high_top_of_page_bid_micros": metrics.high_top_of_page_bid_micros,
        "geo_target_id": settings.google_ads_geo_target_id,
        "language_id": settings.google_ads_language_id,
        "normalizer": {"name": NORMALIZER_NAME, "version": NORMALIZER_VERSION},
    }


def _build_commercial_intent_raw_data(
    metrics: GoogleAdsKeywordMetrics,
    result: CommercialIntentResult,
    *,
    settings: Settings,
) -> dict[str, Any]:
    """commercial_intent の計算根拠 + Google Ads 生指標を JSON-safe な dict で返す。

    market evidence (CPC / competition_index) の有無を後から判断できるよう、
    サブスコア・weight・available_weight (= evidence_coverage) も保存する。
    """

    return {
        # query 由来の意図 (Google Ads 非依存)
        "query_intent_type": result.query_intent_type,
        "query_intent_score": result.query_intent_score,
        # 正規化済みサブスコア (None = 欠測。0 点扱いせず weight を再正規化済み)
        "cpc_score": result.cpc_score,
        "ad_competition_score": result.ad_competition_score,
        # redistribution 前の V1 重み
        "query_intent_weight": result.query_intent_weight,
        "cpc_weight": result.cpc_weight,
        "ad_competition_weight": result.ad_competition_weight,
        # 利用できた元 weight の合計 (= evidence coverage)
        "available_weight": result.available_weight,
        "evidence_coverage": result.evidence_coverage,
        "market_evidence_available": result.market_evidence_available,
        # Google Ads 生指標 (保存のみ。high bid は V1 score に不使用)
        "low_top_of_page_bid_micros": metrics.low_top_of_page_bid_micros,
        "high_top_of_page_bid_micros": metrics.high_top_of_page_bid_micros,
        "competition": metrics.competition,
        "competition_index": metrics.competition_index,
        # ターゲティング / metadata
        "geo_target_id": settings.google_ads_geo_target_id,
        "language_id": settings.google_ads_language_id,
        "normalizer_version": result.normalizer_version,
        "currency_assumption": result.currency_assumption,
        "normalizer": {
            "name": result.normalizer_name,
            "version": result.normalizer_version,
        },
    }


def _build_trend_raw_data(
    metrics: GoogleAdsKeywordMetrics,
    result: TrendResult,
    *,
    settings: Settings,
) -> dict[str, Any]:
    """trend の計算根拠 (前半/後半 3 か月平均・変化率・使った 6 か月) を JSON-safe dict で返す。"""

    return {
        "previous_3_average": result.previous_3_average,
        "recent_3_average": result.recent_3_average,
        "change_ratio": result.change_ratio,
        "months_used": result.months_used,
        "available_months": result.available_months,
        # trend 計算に実際に使った最新 6 か月 (年月昇順)
        "monthly_search_volumes": [
            {
                "year": month.year,
                "month": month.month,
                "monthly_searches": month.monthly_searches,
            }
            for month in result.window
        ],
        "geo_target_id": settings.google_ads_geo_target_id,
        "language_id": settings.google_ads_language_id,
        "normalizer_version": result.normalizer_version,
        "normalizer": {
            "name": result.normalizer_name,
            "version": result.normalizer_version,
        },
    }


class KeywordMetricsCollectionService:
    def __init__(
        self,
        session: Session,
        *,
        provider: GoogleAdsKeywordMetricsProvider | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._keywords = KeywordRepository(session)
        self._signals = KeywordSignalRepository(session)
        self._settings = settings or get_settings()
        self._provider = provider or GoogleAdsKeywordMetricsProvider(self._settings)

    def collect_google_ads_search_demand(self, keyword_id: int) -> KeywordSignalRead:
        keyword = self._keywords.get_by_id(keyword_id)
        if keyword is None:
            raise EntityNotFoundError(_KEYWORD_ENTITY, keyword_id)

        # provider 呼び出しを実施した UTC 日時 = observed_at
        observed_at = datetime.now(UTC)
        metrics_list = self._provider.fetch_historical_metrics([keyword.keyword])
        metrics = _match_metrics(
            metrics_list, keyword.keyword, allow_single_result_fallback=True
        )
        if metrics is None or metrics.avg_monthly_searches is None:
            # 対象 keyword が無い / 有効な historical metrics が無い。0 点は作らない。
            raise ExternalProviderDataError(
                _PROVIDER,
                f"no Google Ads historical metrics for keyword {keyword.keyword!r}",
            )

        normalized = normalize_search_demand(metrics.avg_monthly_searches)
        raw_data = _build_raw_data(metrics, settings=self._settings)
        period_start, period_end = _period_from_volumes(metrics)

        try:
            entity = self._signals.create(
                keyword_id=keyword_id,
                component=KeywordSignalComponent.SEARCH_DEMAND,
                normalized_value=normalized,
                provider=_PROVIDER,
                observed_at=observed_at,
                raw_data=raw_data,
                source_reference=GOOGLE_ADS_SOURCE_REFERENCE,
                period_start=period_start,
                period_end=period_end,
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        self._session.refresh(entity)
        return to_signal_read(entity)

    def collect_google_ads_trend(self, keyword_id: int) -> KeywordSignalRead:
        """Google Ads の monthly_search_volumes から trend Signal を作る。

        検索需要の *方向と勢い* のみを評価する (絶対量は search_demand の担当)。
        年月順にソートした最新 6 か月で前半 3 / 後半 3 平均を比較する。有効月が
        6 未満・負値などで計算できない場合は ``ExternalProviderDataError`` (0 点や
        50 点の Signal を無条件に作らない)。
        """

        keyword = self._keywords.get_by_id(keyword_id)
        if keyword is None:
            raise EntityNotFoundError(_KEYWORD_ENTITY, keyword_id)

        # provider 呼び出しを実施した UTC 日時 = observed_at
        observed_at = datetime.now(UTC)
        metrics_list = self._provider.fetch_historical_metrics([keyword.keyword])
        metrics = _match_metrics(
            metrics_list, keyword.keyword, allow_single_result_fallback=True
        )
        if metrics is None:
            raise ExternalProviderDataError(
                _PROVIDER,
                f"no Google Ads metrics for keyword {keyword.keyword!r}",
            )

        try:
            result = calculate_trend(metrics.monthly_search_volumes)
        except ValueError as exc:
            # 6 か月未満 / 負値。新規例外は作らず data error に寄せる。
            raise ExternalProviderDataError(
                _PROVIDER,
                f"cannot compute trend from Google Ads monthly search volumes: {exc}",
            ) from exc

        raw_data = _build_trend_raw_data(metrics, result, settings=self._settings)
        period_start, period_end = _period_from_volumes(metrics)

        try:
            entity = self._signals.create(
                keyword_id=keyword_id,
                component=KeywordSignalComponent.TREND,
                normalized_value=result.normalized_value,
                provider=_PROVIDER,
                observed_at=observed_at,
                raw_data=raw_data,
                source_reference=GOOGLE_ADS_SOURCE_REFERENCE,
                period_start=period_start,
                period_end=period_end,
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        self._session.refresh(entity)
        return to_signal_read(entity)

    def collect_google_ads_signals_bulk(
        self,
        requests: Sequence[tuple[int, Collection[str]]],
    ) -> list[BulkKeywordSignals]:
        """**1 回** の Historical Metrics bulk fetch から keyword ごとに最大 3 Signal を作る。

        ``requests`` は ``(keyword_id, 作成したい component 集合)``。単体 collector と
        同じ normalizer / raw_data builder / source_reference / エラー判定を再利用する
        (計算式のコピーはしない)。keyword ごとに commit するため、ある keyword の
        失敗が他 keyword を巻き添えにしない。**fetch 自体の失敗** (provider not
        configured / 通信エラー) は bulk 全体の失敗として呼び出し側へ伝播する。
        """

        planned: list[tuple[Any, frozenset[str]]] = []
        for keyword_id, components in requests:
            wanted = frozenset(components) & frozenset(GOOGLE_ADS_BUNDLE_COMPONENTS)
            if not wanted:
                continue
            keyword = self._keywords.get_by_id(keyword_id)
            if keyword is None:
                raise EntityNotFoundError(_KEYWORD_ENTITY, keyword_id)
            planned.append((keyword, wanted))

        if not planned:
            return []

        observed_at = datetime.now(UTC)
        metrics_list = self._provider.fetch_historical_metrics(
            [keyword.keyword for keyword, _ in planned]
        )

        # requested 側で compact key が衝突する keyword は、空白無視の照合を許すと
        # 応答 1 行を複数 keyword へ誤割当しかねない。その keyword は完全一致だけに絞る。
        requested_compact_counts = Counter(
            compact_keyword_match_key(keyword.keyword) for keyword, _ in planned
        )

        results: list[BulkKeywordSignals] = []
        for keyword, wanted in planned:
            # bulk では単一応答 fallback を使わない (無関係な keyword への誤配布を防ぐ)。
            metrics = _match_metrics(
                metrics_list,
                keyword.keyword,
                allow_single_result_fallback=False,
                allow_whitespace_insensitive_match=(
                    requested_compact_counts[
                        compact_keyword_match_key(keyword.keyword)
                    ]
                    == 1
                ),
            )
            created: dict[str, int] = {}
            skipped: dict[str, str] = {}
            try:
                self._bundle_one(
                    keyword_id=keyword.id,
                    keyword_text=keyword.keyword,
                    metrics=metrics,
                    wanted=wanted,
                    observed_at=observed_at,
                    created=created,
                    skipped=skipped,
                )
                self._session.commit()
            except Exception:
                self._session.rollback()
                raise
            results.append(
                BulkKeywordSignals(
                    keyword_id=keyword.id,
                    keyword=keyword.keyword,
                    created=created,
                    skipped=skipped,
                )
            )
        return results

    def _bundle_one(
        self,
        *,
        keyword_id: int,
        keyword_text: str,
        metrics: GoogleAdsKeywordMetrics | None,
        wanted: frozenset[str],
        observed_at: datetime,
        created: dict[str, int],
        skipped: dict[str, str],
    ) -> None:
        period_start, period_end = (
            _period_from_volumes(metrics) if metrics is not None else (None, None)
        )

        if "search_demand" in wanted:
            if metrics is None or metrics.avg_monthly_searches is None:
                skipped["search_demand"] = (
                    "no_metrics" if metrics is None else "no_avg_monthly_searches"
                )
            else:
                entity = self._signals.create(
                    keyword_id=keyword_id,
                    component=KeywordSignalComponent.SEARCH_DEMAND,
                    normalized_value=normalize_search_demand(
                        metrics.avg_monthly_searches
                    ),
                    provider=_PROVIDER,
                    observed_at=observed_at,
                    raw_data=_build_raw_data(metrics, settings=self._settings),
                    source_reference=GOOGLE_ADS_SOURCE_REFERENCE,
                    period_start=period_start,
                    period_end=period_end,
                )
                created["search_demand"] = entity.id

        if "commercial_intent" in wanted:
            if metrics is None:
                skipped["commercial_intent"] = "no_metrics"
            else:
                ci = calculate_commercial_intent(
                    keyword=keyword_text,
                    low_top_of_page_bid_micros=metrics.low_top_of_page_bid_micros,
                    competition_index=metrics.competition_index,
                )
                entity = self._signals.create(
                    keyword_id=keyword_id,
                    component=KeywordSignalComponent.COMMERCIAL_INTENT,
                    normalized_value=ci.score,
                    provider=_PROVIDER,
                    observed_at=observed_at,
                    raw_data=_build_commercial_intent_raw_data(
                        metrics, ci, settings=self._settings
                    ),
                    source_reference=GOOGLE_ADS_SOURCE_REFERENCE,
                    period_start=period_start,
                    period_end=period_end,
                )
                created["commercial_intent"] = entity.id

        if "trend" in wanted:
            if metrics is None:
                skipped["trend"] = "no_metrics"
            else:
                try:
                    tr = calculate_trend(metrics.monthly_search_volumes)
                except ValueError:
                    skipped["trend"] = "insufficient_monthly_volumes"
                else:
                    entity = self._signals.create(
                        keyword_id=keyword_id,
                        component=KeywordSignalComponent.TREND,
                        normalized_value=tr.normalized_value,
                        provider=_PROVIDER,
                        observed_at=observed_at,
                        raw_data=_build_trend_raw_data(
                            metrics, tr, settings=self._settings
                        ),
                        source_reference=GOOGLE_ADS_SOURCE_REFERENCE,
                        period_start=period_start,
                        period_end=period_end,
                    )
                    created["trend"] = entity.id

    def collect_google_ads_commercial_intent(
        self, keyword_id: int
    ) -> KeywordSignalRead:
        """keyword 文字列 + Google Ads 指標から commercial_intent Signal を作る。

        keyword-derived な Query Intent と Google Ads の市場指標 (CPC / 広告競争度)
        を合成した Signal のため provider は ``google_ads`` (search_demand collector
        と同じ)。合成の内訳は raw_data に保存する。CPC / competition_index が
        欠測でも Query Intent だけで score を出す (0 点 Signal は作らない)。
        """

        keyword = self._keywords.get_by_id(keyword_id)
        if keyword is None:
            raise EntityNotFoundError(_KEYWORD_ENTITY, keyword_id)

        # provider 呼び出しを実施した UTC 日時 = observed_at
        observed_at = datetime.now(UTC)
        metrics_list = self._provider.fetch_historical_metrics([keyword.keyword])
        metrics = _match_metrics(
            metrics_list, keyword.keyword, allow_single_result_fallback=True
        )
        if metrics is None:
            # Google Ads がこの keyword の行を返さなかった。Query Intent だけの
            # Signal を無条件に作らず、データエラーとして扱う (search_demand と同方針)。
            raise ExternalProviderDataError(
                _PROVIDER,
                f"no Google Ads metrics for keyword {keyword.keyword!r}",
            )

        result = calculate_commercial_intent(
            keyword=keyword.keyword,
            low_top_of_page_bid_micros=metrics.low_top_of_page_bid_micros,
            competition_index=metrics.competition_index,
        )
        raw_data = _build_commercial_intent_raw_data(
            metrics, result, settings=self._settings
        )
        period_start, period_end = _period_from_volumes(metrics)

        try:
            entity = self._signals.create(
                keyword_id=keyword_id,
                component=KeywordSignalComponent.COMMERCIAL_INTENT,
                normalized_value=result.score,
                provider=_PROVIDER,
                observed_at=observed_at,
                raw_data=raw_data,
                source_reference=GOOGLE_ADS_SOURCE_REFERENCE,
                period_start=period_start,
                period_end=period_end,
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        self._session.refresh(entity)
        return to_signal_read(entity)
