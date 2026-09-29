"""出所ごとの状態を 1 つの形で集める (C10-A)。**読むだけ・外に問い合わせない。**

``app/analysis/sources.py`` の ``SourceStatus`` を、7 つの出所について返す:

- 取り込みの出所 (search_console / ga4 / affiliate_clicks / make_commissions): 既存の
  ``collect_source_freshness`` と運用の ``evaluate_source_refresh`` をそのまま使う
  (``legacy_state`` は C9 の証拠と同じ)。そのうえで、最新の取り込みの失敗 (``provider_error``)・
  取り込めたが行が無い (``insufficient``)・設定が無い / 一度も無い (``missing``) を分ける。
- google_ads: 保存済みの Google Ads の signal の観測の時刻 (分析は Google Ads を呼ばない)。
- threads_insights: 保存済みの Threads の観測 (worker が取り込む)。
- url_inspection: URL Inspection をした最新の確認 (``IndexStateService``)。

C9 の証拠 (``GrowthEvidenceService``) と C9-C の観測 (``GrowthMeasurementService``) はここを使う
(同じ判定を 2 か所に書かない)。
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analysis import sources as src
from app.article.fact_freshness import ensure_aware
from app.models import (
    AffiliateClickImportRun,
    AffiliateCommissionImportRun,
    Ga4ImportRun,
    Keyword,
    KeywordSignal,
    SearchConsoleImportRun,
    ThreadsInsightSnapshot,
)

IMPORT_SOURCES = ("search_console", "ga4", "affiliate_clicks", "make_commissions")
_RUN_TABLES = {"search_console": SearchConsoleImportRun, "ga4": Ga4ImportRun,
               "affiliate_clicks": AffiliateClickImportRun,
               "make_commissions": AffiliateCommissionImportRun}  # fmt: skip


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_aware(value).isoformat()
    return value.isoformat()


class SourceHealthService:
    def __init__(self, session: Session, *, settings=None, timezone: ZoneInfo | None = None):
        self._session = session
        if settings is None:
            from app.config.settings import get_settings

            settings = get_settings()
        self._settings = settings
        if timezone is None:
            from app.operations.policy import get_policy as get_ops_policy

            timezone = get_ops_policy().timezone
        self._tz = timezone

    # -- 取り込みの出所 ------------------------------------------------------------------------
    def import_freshness(self, *, now: datetime) -> tuple[dict, dict]:
        """C9 の証拠と同じ (``SourceFreshness`` の辞書・C9 の鮮度の言葉)。"""

        from app.operations.policy import get_policy as get_ops_policy
        from app.operations.source_health import evaluate_source_refresh
        from app.services.operations_source_health_service import collect_source_freshness

        policy = get_ops_policy()
        today = now.astimezone(self._tz).date()
        raw = collect_source_freshness(self._session)
        legacy = {}
        for name, f in raw.items():
            if not f.ever_imported:
                legacy[name] = src.LEGACY_UNAVAILABLE
            elif evaluate_source_refresh(freshness=f, today=today, now=now, policy=policy):
                legacy[name] = src.LEGACY_STALE
            else:
                legacy[name] = src.LEGACY_FRESH
        if not self._ga4_configured():
            legacy["ga4"] = src.LEGACY_UNAVAILABLE
        return raw, legacy

    def _ga4_configured(self) -> bool:
        return bool((getattr(self._settings, "ga4_property_id", None) or "").strip())

    def _import_status(self, name: str, f, legacy: str, *, now: datetime) -> src.SourceStatus:
        from app.operations.policy import get_policy as get_ops_policy
        from app.operations.source_health import evaluate_source_refresh

        definition = src.source_definition(name)
        table = _RUN_TABLES[name]
        latest = self._session.scalars(select(table).order_by(table.id.desc()).limit(1)).first()
        through = f.coverage_through
        if through is None and f.last_successful_import_at is not None and (
                name == "affiliate_clicks"):
            # cursor の取り込み: 取り込んだ日の前の日までは全部届いている。
            through = (ensure_aware(f.last_successful_import_at).astimezone(self._tz).date()
                       - timedelta(days=1))
        today = now.astimezone(self._tz).date()
        if through is not None and through > today:
            through = today  # 観測の時点より後の日は使わない
        flags, reason = [], None
        if name == "ga4" and not self._ga4_configured():
            state, reason = src.MISSING, "ga4_property_id is not configured"
        elif not f.ever_imported:
            state, reason = src.MISSING, "never imported"
        elif latest is not None and latest.status == "failed":
            state, reason = src.PROVIDER_ERROR, "the latest import run failed"
        else:
            alert = evaluate_source_refresh(freshness=f, today=today, now=now,
                                            policy=get_ops_policy())
            if alert is not None:
                state, reason = src.STALE, getattr(alert, "summary", None) or "stale import"
            elif not f.ever_had_data:
                state, reason = src.INSUFFICIENT, "imports succeeded but returned no rows yet"
            else:
                state = src.FRESH
        if f.latest_observed_data_date and through and f.latest_observed_data_date < through:
            flags.append("no_rows_after_" + f.latest_observed_data_date.isoformat())
        return src.SourceStatus(
            source=name, provider=definition.get("provider", name), freshness_state=state,
            observed_at=_iso(f.last_successful_import_at),
            collected_at=_iso(getattr(latest, "created_at", None)) if latest else None,
            data_through=_iso(through), expected_lag_days=src.expected_lag_days(name),
            missing_reason=reason, quality_flags=tuple(flags),
            provenance=table.__tablename__, legacy_state=legacy,
            affected={"latest_observed_data_date": _iso(f.latest_observed_data_date)},
            refresh=definition.get("refresh"))

    # -- 取り込みの無い出所 --------------------------------------------------------------------
    def _google_ads(self, *, now: datetime) -> src.SourceStatus:
        from app.keyword.normalizers.commercial_intent import market_evidence

        definition = src.source_definition("google_ads")
        rows = list(self._session.scalars(select(KeywordSignal).where(
            KeywordSignal.provider == "google_ads",
            KeywordSignal.component == "commercial_intent")
            .order_by(KeywordSignal.keyword_id, KeywordSignal.observed_at.desc(),
                      KeywordSignal.id.desc())))  # fmt: skip
        latest: dict[int, KeywordSignal] = {}
        for row in rows:
            latest.setdefault(row.keyword_id, row)
        observed = [ensure_aware(r.observed_at) for r in latest.values()
                    if ensure_aware(r.observed_at) <= now]  # fmt: skip
        newest = max(observed) if observed else None
        keywords = self._session.scalar(select(func.count()).select_from(Keyword)) or 0
        market = Counter()
        for row in latest.values():
            raw = dict(row.raw_data or {})
            market[market_evidence(
                low_top_of_page_bid_micros=raw.get("low_top_of_page_bid_micros"),
                competition_index=raw.get("competition_index"),
                competition=raw.get("competition"),
                high_top_of_page_bid_micros=raw.get("high_top_of_page_bid_micros")).state] += 1
        state = src.age_state(newest, now=now, stale_after_days=definition.get("stale_after_days"))
        flags = []
        if keywords - len(latest):
            flags.append(f"{keywords - len(latest)}_keywords_without_stored_metrics")
        if market.get("missing"):
            flags.append(f"{market['missing']}_keywords_without_market_evidence")
        return src.SourceStatus(
            source="google_ads", provider=definition["provider"], freshness_state=state,
            observed_at=_iso(newest), data_through=_iso(newest.date()) if newest else None,
            expected_lag_days=src.expected_lag_days("google_ads"),
            missing_reason="no stored Google Ads signal" if newest is None else None,
            quality_flags=tuple(flags), provenance="keyword_signals[provider=google_ads]",
            legacy_state=src.legacy_from(state),
            affected={"keywords": keywords, "keywords_with_stored_metrics": len(latest),
                      "market_evidence": dict(sorted(market.items()))},
            refresh=definition.get("refresh"))

    def _threads(self, *, now: datetime) -> src.SourceStatus:
        definition = src.source_definition("threads_insights")
        newest = self._session.scalar(select(func.max(ThreadsInsightSnapshot.observed_at)).where(
            ThreadsInsightSnapshot.outcome == "observed",
            ThreadsInsightSnapshot.observed_at <= now))  # fmt: skip
        newest = ensure_aware(newest) if newest else None
        since = now - timedelta(hours=float(definition.get("stale_after_hours") or 48))
        errors = self._session.scalar(select(func.count()).select_from(ThreadsInsightSnapshot)
                                      .where(ThreadsInsightSnapshot.outcome != "observed",
                                             ThreadsInsightSnapshot.observed_at >= since)) or 0
        state = src.age_state(newest, now=now,
                              stale_after_hours=definition.get("stale_after_hours"))
        return src.SourceStatus(
            source="threads_insights", provider=definition["provider"], freshness_state=state,
            observed_at=_iso(newest), data_through=_iso(newest),
            expected_lag_days=src.expected_lag_days("threads_insights"),
            missing_reason="no Threads observation stored" if newest is None else None,
            quality_flags=(f"{errors}_unsuccessful_observations_recently",) if errors else (),
            provenance="threads_insight_snapshots", legacy_state=src.legacy_from(state),
            refresh=definition.get("refresh"))

    def _url_inspection(self, *, now: datetime) -> src.SourceStatus:
        from app.services.index_state_service import IndexStateService

        definition = src.source_definition("url_inspection")
        observations, meta = IndexStateService(self._session).latest(now=now)
        at = meta.get("latest_inspected_at")
        state = src.age_state(datetime.fromisoformat(at) if at else None, now=now,
                              stale_after_days=definition.get("stale_after_days"))
        counts = Counter(o.normalized_status for o in observations.values())
        unknown = counts.get("unknown", 0) + counts.get("error", 0)
        return src.SourceStatus(
            source="url_inspection", provider=definition["provider"], freshness_state=state,
            observed_at=at, data_through=at, expected_lag_days=0,
            missing_reason="no check_indexability run used URL Inspection" if not at else None,
            quality_flags=(f"{unknown}_articles_unknown",) if unknown else (),
            provenance=f"operations_step_runs[check_indexability] run "
                       f"{meta.get('latest_inspected_run_id')}",
            legacy_state=src.legacy_from(state),
            affected={"articles": len(observations),
                      "normalized_status": dict(sorted(counts.items()))},
            refresh=definition.get("refresh"))

    # -- public -------------------------------------------------------------------------------
    def collect(self, *, now: datetime | None = None) -> dict[str, src.SourceStatus]:
        now = ensure_aware(now or datetime.now(UTC))
        raw, legacy = self.import_freshness(now=now)
        out = {name: self._import_status(name, raw[name], legacy[name], now=now)
               for name in IMPORT_SOURCES if name in raw}
        out["google_ads"] = self._google_ads(now=now)
        out["threads_insights"] = self._threads(now=now)
        out["url_inspection"] = self._url_inspection(now=now)
        return out


__all__ = ["IMPORT_SOURCES", "SourceHealthService"]
