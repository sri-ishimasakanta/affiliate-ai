"""Keyword Analysis Workflow のオーケストレーション Service。

Phase 2B までに個別実装した 7 component の Signal 生成 / スコアリングを、
実運用可能な一連の分析フローとしてまとめる。

- **各 component の formula は再実装しない** (既存 normalizer / Service を呼ぶだけ)。
- `scoring.py` は変更しない (`KeywordScoringService.score_keyword_from_latest_signals`
  をそのまま利用)。
- **追加実費ゼロ**: 使うのは既存 Google Ads API (bulk 1 fetch) / local DB /
  affiliate catalog / manual competition_ease のみ。DataForSEO / 有料 SEO / SERP /
  LLM / embedding / scraper は使わない。
- 書き込みは各 Service が commit / 失敗時 rollback (既存方針を維持)。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.exceptions import (
    DuplicateEntityError,
    EntityNotFoundError,
    ExternalProviderDataError,
    ExternalProviderError,
    IncompleteSignalSetError,
    ProviderNotConfiguredError,
)
from app.keyword.schemas import KeywordCreate
from app.keyword.scoring import COMPONENT_NAMES
from app.models import Keyword
from app.repositories.keyword_repository import KeywordRepository
from app.repositories.keyword_score_repository import KeywordScoreRepository
from app.repositories.keyword_signal_repository import KeywordSignalRepository
from app.services.keyword_metrics_collection_service import (
    GOOGLE_ADS_BUNDLE_COMPONENTS,
    KeywordMetricsCollectionService,
)
from app.services.keyword_scoring_service import KeywordScoringService
from app.services.keyword_service import KeywordService
from app.services.keyword_signal_service import KeywordSignalService

# scoring.py と同じ 7 component (順序も共有)。
ALL_COMPONENTS: tuple[str, ...] = COMPONENT_NAMES
LOCAL_AUTO_COMPONENTS: tuple[str, ...] = (
    "site_relevance",
    "affiliate_opportunity",
    "originality",
)
AUTO_COMPONENTS: tuple[str, ...] = GOOGLE_ADS_BUNDLE_COMPONENTS + LOCAL_AUTO_COMPONENTS
# competition_ease は manual / CSV のみ (このワークフローでは自動生成しない)。
MANUAL_ONLY_COMPONENTS: tuple[str, ...] = ("competition_ease",)

_PROVIDER_ERRORS = (
    ProviderNotConfiguredError,
    ExternalProviderError,
    ExternalProviderDataError,
)


@dataclass(frozen=True)
class ResolvedKeywords:
    resolved: list[Keyword]
    created: list[int]
    unresolved: list[str]


@dataclass(frozen=True)
class Readiness:
    keyword_id: int
    keyword: str
    present: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.missing


@dataclass
class AutoCollectReport:
    created: dict[str, int] = field(default_factory=dict)
    reused: dict[str, int] = field(default_factory=dict)
    failed: dict[str, int] = field(default_factory=dict)
    provider_error: str | None = None
    keyword_failures: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class ScoreOutcome:
    keyword_id: int
    status: str  # "scored" | "reused" | "incomplete" | "failed"
    total_score: float | None
    missing: tuple[str, ...]


@dataclass(frozen=True)
class RankingRow:
    keyword_id: int
    keyword: str
    component_values: dict[str, float | None]
    opportunity_score: float | None
    analysis_status: str  # "complete" | "incomplete"
    missing_components: tuple[str, ...]


def normalize_keyword_inputs(
    cli_keywords: Iterable[str] | None, csv_keywords: Iterable[str] | None
) -> list[str]:
    """trim / 空除外 / 重複除去 (入力順維持)。"""

    seen: set[str] = set()
    out: list[str] = []
    for raw in list(cli_keywords or []) + list(csv_keywords or []):
        keyword = (raw or "").strip()
        if not keyword or keyword in seen:
            continue
        seen.add(keyword)
        out.append(keyword)
    return out


class KeywordAnalysisService:
    def __init__(
        self,
        session: Session,
        *,
        metrics_service: KeywordMetricsCollectionService | None = None,
        signal_service: KeywordSignalService | None = None,
        scoring_service: KeywordScoringService | None = None,
    ) -> None:
        self._session = session
        self._keywords = KeywordRepository(session)
        self._signals = KeywordSignalRepository(session)
        self._scores = KeywordScoreRepository(session)
        self._keyword_service = KeywordService(session)
        self._metrics = metrics_service or KeywordMetricsCollectionService(session)
        self._signal_service = signal_service or KeywordSignalService(session)
        self._scoring = scoring_service or KeywordScoringService(session)

    # -- resolve ---------------------------------------------------------
    def resolve_keywords(
        self, keyword_texts: Sequence[str], *, create_missing: bool
    ) -> ResolvedKeywords:
        resolved: list[Keyword] = []
        created: list[int] = []
        unresolved: list[str] = []
        for text in keyword_texts:
            existing = self._keywords.get_by_keyword(text)
            if existing is not None:
                resolved.append(existing)
                continue
            if not create_missing:
                unresolved.append(text)
                continue
            try:
                read = self._keyword_service.create_keyword(KeywordCreate(keyword=text))
            except DuplicateEntityError:
                again = self._keywords.get_by_keyword(text)
                if again is not None:
                    resolved.append(again)
                else:  # pragma: no cover - 競合時のみ
                    unresolved.append(text)
                continue
            entity = self._keywords.get_by_id(read.id)
            if entity is not None:
                resolved.append(entity)
                created.append(entity.id)
        return ResolvedKeywords(
            resolved=resolved, created=created, unresolved=unresolved
        )

    # -- readiness (read-only) ----------------------------------------
    def readiness(self, keyword_id: int) -> Readiness:
        keyword = self._keywords.get_by_id(keyword_id)
        if keyword is None:
            raise EntityNotFoundError("Keyword", keyword_id)
        present: list[str] = []
        missing: list[str] = []
        for component in ALL_COMPONENTS:
            if self._signals.get_latest(keyword_id, component) is not None:
                present.append(component)
            else:
                missing.append(component)
        return Readiness(
            keyword_id=keyword_id,
            keyword=keyword.keyword,
            present=tuple(present),
            missing=tuple(missing),
        )

    def components_to_generate(
        self, keyword_id: int, *, refresh: bool
    ) -> set[str]:
        """rerun policy: ``refresh`` なら全 auto component、そうでなければ最新 Signal が
        無い auto component だけを (再) 生成対象とする。"""

        if refresh:
            return set(AUTO_COMPONENTS)
        return {
            component
            for component in AUTO_COMPONENTS
            if self._signals.get_latest(keyword_id, component) is None
        }

    # -- auto signal collection ------------------------------------
    def collect_auto_signals(
        self, keyword_ids: Sequence[int], *, refresh: bool = False
    ) -> AutoCollectReport:
        report = AutoCollectReport()
        plans: dict[int, set[str]] = {
            kid: self.components_to_generate(kid, refresh=refresh)
            for kid in keyword_ids
        }

        # reuse カウント (今回生成しない = 既存を再利用)
        for kid in keyword_ids:
            for component in AUTO_COMPONENTS:
                if component not in plans[kid]:
                    report.reused[component] = report.reused.get(component, 0) + 1

        # Google Ads: 1 bulk fetch (必要な keyword が 1 つでもあれば)
        ga_requests = [
            (kid, wanted & set(GOOGLE_ADS_BUNDLE_COMPONENTS))
            for kid, wanted in plans.items()
            if wanted & set(GOOGLE_ADS_BUNDLE_COMPONENTS)
        ]
        if ga_requests:
            try:
                bulk = self._metrics.collect_google_ads_signals_bulk(ga_requests)
            except _PROVIDER_ERRORS as exc:
                report.provider_error = str(exc)
                for _kid, wanted in ga_requests:
                    for component in wanted:
                        report.failed[component] = (
                            report.failed.get(component, 0) + 1
                        )
            else:
                for item in bulk:
                    for component in item.created:
                        report.created[component] = (
                            report.created.get(component, 0) + 1
                        )
                    for component in item.skipped:
                        report.failed[component] = (
                            report.failed.get(component, 0) + 1
                        )

        # local: keyword ごとに既存 derive_* を呼ぶ
        local_derivers = {
            "site_relevance": self._signal_service.derive_site_relevance,
            "affiliate_opportunity": self._signal_service.derive_affiliate_opportunity,
            "originality": self._signal_service.derive_originality,
        }
        for kid in keyword_ids:
            for component in LOCAL_AUTO_COMPONENTS:
                if component not in plans[kid]:
                    continue
                try:
                    local_derivers[component](kid)
                except Exception:  # 1 keyword の失敗で batch を止めない
                    report.failed[component] = report.failed.get(component, 0) + 1
                    if kid not in report.keyword_failures:
                        report.keyword_failures.append(kid)
                else:
                    report.created[component] = report.created.get(component, 0) + 1
        return report

    # -- final scoring --------------------------------------------
    def score_ready(
        self, keyword_ids: Sequence[int], *, refresh: bool = False
    ) -> list[ScoreOutcome]:
        outcomes: list[ScoreOutcome] = []
        for kid in keyword_ids:
            state = self.readiness(kid)
            if not state.complete:
                outcomes.append(
                    ScoreOutcome(
                        keyword_id=kid,
                        status="incomplete",
                        total_score=None,
                        missing=state.missing,
                    )
                )
                continue
            if not refresh and self._scores.get_latest(kid) is not None:
                latest = self._scores.get_latest(kid)
                outcomes.append(
                    ScoreOutcome(
                        keyword_id=kid,
                        status="reused",
                        total_score=latest.total_score,
                        missing=(),
                    )
                )
                continue
            try:
                read = self._scoring.score_keyword_from_latest_signals(kid)
            except IncompleteSignalSetError as exc:  # 防御的
                outcomes.append(
                    ScoreOutcome(
                        keyword_id=kid,
                        status="incomplete",
                        total_score=None,
                        missing=tuple(exc.missing_components),
                    )
                )
            else:
                outcomes.append(
                    ScoreOutcome(
                        keyword_id=kid,
                        status="scored",
                        total_score=read.total_score,
                        missing=(),
                    )
                )
        return outcomes

    # -- ranking (read-only) -------------------------------------
    def ranking_rows(self, keyword_ids: Sequence[int]) -> list[RankingRow]:
        rows: list[RankingRow] = []
        for kid in keyword_ids:
            keyword = self._keywords.get_by_id(kid)
            if keyword is None:
                continue
            values: dict[str, float | None] = {}
            missing: list[str] = []
            for component in ALL_COMPONENTS:
                signal = self._signals.get_latest(kid, component)
                values[component] = signal.normalized_value if signal else None
                if signal is None:
                    missing.append(component)
            complete = not missing
            rows.append(
                RankingRow(
                    keyword_id=kid,
                    keyword=keyword.keyword,
                    component_values=values,
                    opportunity_score=(
                        keyword.opportunity_score if complete else None
                    ),
                    analysis_status="complete" if complete else "incomplete",
                    missing_components=tuple(missing),
                )
            )

        def _sort_key(row: RankingRow) -> tuple[int, float, str]:
            if row.analysis_status == "complete" and row.opportunity_score is not None:
                return (0, -row.opportunity_score, row.keyword)
            return (1, 0.0, row.keyword)

        rows.sort(key=_sort_key)
        return rows

    def competition_ease_missing(self, keyword_ids: Sequence[int]) -> list[Keyword]:
        out: list[Keyword] = []
        for kid in keyword_ids:
            if self._signals.get_latest(kid, "competition_ease") is None:
                keyword = self._keywords.get_by_id(kid)
                if keyword is not None:
                    out.append(keyword)
        return out
