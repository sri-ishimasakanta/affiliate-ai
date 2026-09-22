"""ArticleMeasurementReportService -- 記事単位の計測ベースライン (C5.2/C5.3)。

7 つの問いを **別々のまま** answer する read-only の合成 service:

1. Google はページを見ているか      -- live / sitemap / URL Inspection (C5.1)
2. どんなクエリで露出しているか      -- Search Console (C1.1)
3. どれだけ流入しているか            -- GA4 (C5.2)
4. どれだけ読まれているか            -- GA4 engagement (C5.2)
5. アフィリエイト CTA は押されたか   -- first-party outbound click (D-B2/E1)
6. 成果/報酬は分かっているか         -- Make commission (E1)
7. 何が分からないか                  -- 明示的に ``unavailable`` と書く

**混ぜない**。指標を 1 つのスコアに畳まない。分母が 0 のとき比率を出さない。
実測が無いところに推定値を置かない。報酬をクリック数で按分しない。

すべて既存の取り込み結果を読むだけで、外部への書き込みも DB への書き込みも行わない。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AffiliateCommissionFact,
    AffiliateProgram,
    Article,
    Ga4ImportRun,
    Ga4PageDaily,
    Keyword,
    SearchConsolePageDaily,
    SearchConsoleQueryDaily,
)
from app.seo.url_normalization import normalize_url_key
from app.services.affiliate_click_metrics_service import AffiliateClickMetricsService

# -- 帰属スコープ (K) ---------------------------------------------------------
ATTRIBUTION_ARTICLE = "article"
ATTRIBUTION_PROGRAM_ONLY = "program_only"
ATTRIBUTION_UNAVAILABLE = "unavailable"

#: 記事に割り当てられない報酬の明示ラベル。按分は絶対にしない。
UNATTRIBUTED_TO_ARTICLE = "UNATTRIBUTED_TO_ARTICLE"

# -- 記述的な状態 (M) ---------------------------------------------------------
STATE_AWAITING_GSC_DATA = "awaiting_gsc_data"
STATE_AWAITING_GA4_DATA = "awaiting_ga4_data"
STATE_GA4_NOT_CONFIGURED = "ga4_not_configured"
STATE_INDEXED_NO_IMPRESSIONS_YET = "indexed_no_impressions_yet"
STATE_IMPRESSIONS_OBSERVED = "impressions_observed"
STATE_CLICKS_OBSERVED = "clicks_observed"
STATE_SESSIONS_OBSERVED = "sessions_observed"
STATE_AFFILIATE_CLICKS_OBSERVED = "affiliate_clicks_observed"

_CHANNEL_ALL = "all"
_CHANNEL_ORGANIC = "organic_search"


@dataclass
class ArticleMeasurement:
    # -- identity --
    article_id: int
    keyword: str | None
    url: str
    published_at: str | None
    article_type: str | None
    monetization_mode: str | None

    # -- indexing (C5.1; スナップショットであることを時刻で示す) --
    live_state: str | None = None
    sitemap_state: str | None = None
    google_index_state: str | None = None
    index_state_observed_at: str | None = None

    # -- Search Console --
    gsc_rows: int = 0
    impressions: int = 0
    clicks: int = 0
    ctr: float | None = None
    average_position: float | None = None
    query_count: int = 0
    top_queries: list[dict] = field(default_factory=list)

    # -- GA4 --
    ga4_rows: int = 0
    sessions: int = 0
    active_users: int = 0
    new_users: int = 0
    engaged_sessions: int = 0
    engagement_rate: float | None = None
    average_engagement_time_seconds: float | None = None
    screen_page_views: int = 0
    organic_sessions: int = 0
    organic_active_users: int = 0

    # -- affiliate --
    affiliate_clicks: int = 0
    affiliate_programs: list[str] = field(default_factory=list)
    conversions: int | None = None
    commission_amount: str | None = None
    commission_currency: str | None = None
    attribution_scope: str = ATTRIBUTION_UNAVAILABLE

    states: list[str] = field(default_factory=list)


@dataclass
class MeasurementReport:
    generated_at: datetime
    window_start: date
    window_end: date
    site_base_url: str
    article_count: int
    gsc_property: str | None = None
    gsc_data_through: date | None = None
    #: C8.5: 取り込みが問い合わせ終えた最終日 (活動の最終日とは別)。
    gsc_coverage_through: date | None = None
    ga4_property: str | None = None
    ga4_data_through: date | None = None
    ga4_coverage_through: date | None = None
    ga4_property_timezone: str | None = None
    ga4_configured: bool = False
    indexability_included: bool = False
    articles: list[ArticleMeasurement] = field(default_factory=list)
    unattributed_commissions: list[dict] = field(default_factory=list)
    affiliate_click_total: int = 0
    affiliate_unattributed_clicks: int = 0
    data_quality: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["generated_at"] = self.generated_at.isoformat()
        for key in (
            "window_start",
            "window_end",
            "gsc_data_through",
            "gsc_coverage_through",
            "ga4_data_through",
            "ga4_coverage_through",
        ):
            value = getattr(self, key)
            payload[key] = value.isoformat() if value else None
        return payload


class ArticleMeasurementReportService:
    def __init__(self, session: Session, *, settings) -> None:
        self._session = session
        self._settings = settings

    # -- public ---------------------------------------------------------------
    def build(
        self, *, days: int = 30, indexability=None, now: datetime | None = None
    ) -> MeasurementReport:
        now = now or datetime.now(UTC)
        window_end = now.date()
        window_start = window_end - timedelta(days=days - 1)
        base = (self._settings.wordpress_base_url or "").rstrip("/")
        ga4_property = (getattr(self._settings, "ga4_property_id", None) or "").strip() or None

        report = MeasurementReport(
            generated_at=now,
            window_start=window_start,
            window_end=window_end,
            site_base_url=base,
            article_count=0,
            gsc_property=getattr(self._settings, "search_console_property_uri", None),
            ga4_property=ga4_property,
            ga4_configured=bool(ga4_property),
            indexability_included=indexability is not None,
        )

        articles = self._published_articles()
        report.article_count = len(articles)
        article_keys = {normalize_url_key(a.published_url or ""): a.id for a in articles}

        gsc_pages, report.gsc_data_through = self._gsc_pages(window_start, window_end)
        gsc_queries = self._gsc_queries(window_start, window_end)
        ga4_pages, report.ga4_data_through = self._ga4_pages(window_start, window_end, base)
        report.ga4_property_timezone = self._ga4_timezone()
        coverage = self._coverage_through()
        report.gsc_coverage_through = coverage.get("search_console")
        report.ga4_coverage_through = coverage.get("ga4")
        clicks = AffiliateClickMetricsService(self._session).aggregate(
            start_date=window_start, end_date=window_end
        )
        report.affiliate_click_total = clicks.total_clicks
        report.affiliate_unattributed_clicks = clicks.unattributed_clicks
        programs = {p.id: p.name for p in self._session.scalars(select(AffiliateProgram)).all()}
        index_rows = self._index_rows(indexability)

        for article in articles:
            key = normalize_url_key(article.published_url or "")
            row = ArticleMeasurement(
                article_id=article.id,
                keyword=self._keyword(article),
                url=article.published_url or "",
                published_at=(article.published_at.isoformat() if article.published_at else None),
                article_type=article.article_type,
                monetization_mode=article.monetization_mode,
            )
            self._apply_index(row, index_rows.get(article.id), indexability)
            self._apply_gsc(row, gsc_pages.get(key), gsc_queries.get(key))
            self._apply_ga4(row, ga4_pages.get(key))
            self._apply_affiliate(row, clicks.for_article(article.id), programs)
            row.states = self._states(row, report)
            report.articles.append(row)

        report.unattributed_commissions = self._unattributed_commissions(
            window_start, window_end, programs
        )
        report.data_quality = self._data_quality(
            report, articles, article_keys, gsc_pages, ga4_pages, clicks
        )
        return report

    # -- sources --------------------------------------------------------------
    def _published_articles(self) -> list[Article]:
        stmt = (
            select(Article)
            .where(Article.status == "published", Article.published_url.is_not(None))
            .order_by(Article.id)
        )
        return list(self._session.scalars(stmt).all())

    def _keyword(self, article: Article) -> str | None:
        if not article.keyword_id:
            return None
        keyword = self._session.get(Keyword, article.keyword_id)
        return keyword.keyword if keyword else None

    def _gsc_pages(self, start: date, end: date) -> tuple[dict, date | None]:
        rows = self._session.scalars(
            select(SearchConsolePageDaily).where(
                SearchConsolePageDaily.metric_date >= start,
                SearchConsolePageDaily.metric_date <= end,
            )
        ).all()
        grouped: dict[str | None, dict] = defaultdict(
            lambda: {"rows": 0, "clicks": 0, "impressions": 0, "positions": []}
        )
        for r in rows:
            bucket = grouped[normalize_url_key(r.page)]
            bucket["rows"] += 1
            bucket["clicks"] += r.clicks
            bucket["impressions"] += r.impressions
            bucket["positions"].append((r.position, r.impressions))
        data_through = self._session.scalar(select(func.max(SearchConsolePageDaily.metric_date)))
        return grouped, data_through

    def _gsc_queries(self, start: date, end: date) -> dict:
        rows = self._session.scalars(
            select(SearchConsoleQueryDaily).where(
                SearchConsoleQueryDaily.metric_date >= start,
                SearchConsoleQueryDaily.metric_date <= end,
            )
        ).all()
        grouped: dict[str | None, dict[str, dict]] = defaultdict(dict)
        for r in rows:
            key = normalize_url_key(r.page)
            bucket = grouped[key].setdefault(
                r.query, {"query": r.query, "clicks": 0, "impressions": 0, "positions": []}
            )
            bucket["clicks"] += r.clicks
            bucket["impressions"] += r.impressions
            bucket["positions"].append((r.position, r.impressions))
        return grouped

    def _ga4_pages(self, start: date, end: date, base: str) -> tuple[dict, date | None]:
        rows = self._session.scalars(
            select(Ga4PageDaily).where(
                Ga4PageDaily.metric_date >= start, Ga4PageDaily.metric_date <= end
            )
        ).all()
        grouped: dict[str | None, dict] = defaultdict(
            lambda: {
                "rows": 0,
                _CHANNEL_ALL: _empty_ga4(),
                _CHANNEL_ORGANIC: _empty_ga4(),
            }
        )
        for r in rows:
            key = normalize_url_key(f"{base}{r.page_path}")
            bucket = grouped[key]
            bucket["rows"] += 1
            scope = bucket.get(r.channel_scope)
            if scope is None:
                continue
            scope["sessions"] += r.sessions
            scope["active_users"] += r.active_users
            scope["new_users"] += r.new_users
            scope["engaged_sessions"] += r.engaged_sessions
            scope["screen_page_views"] += r.screen_page_views
            scope["engagement_seconds"] += r.average_engagement_time_seconds * r.active_users
        data_through = self._session.scalar(select(func.max(Ga4PageDaily.metric_date)))
        return grouped, data_through

    def _coverage_through(self) -> dict[str, date | None]:
        """取り込みが実際に問い合わせ終えた最終日 (0 行成功でも前進する)。"""

        from app.services.operations_source_health_service import collect_source_freshness

        return {
            name: value.coverage_through
            for name, value in collect_source_freshness(self._session).items()
        }

    def _ga4_timezone(self) -> str | None:
        run = self._session.scalars(
            select(Ga4ImportRun)
            .where(Ga4ImportRun.property_timezone.is_not(None))
            .order_by(Ga4ImportRun.id.desc())
            .limit(1)
        ).first()
        return run.property_timezone if run else None

    def _index_rows(self, indexability) -> dict:
        if indexability is None:
            return {}
        return {row.article_id: row for row in indexability.articles}

    # -- per-article --------------------------------------------------------
    def _apply_index(self, row: ArticleMeasurement, state, indexability) -> None:
        if state is None:
            return
        row.live_state = state.live_state
        row.sitemap_state = state.sitemap_state
        row.google_index_state = state.google_index_state
        # URL Inspection は時点スナップショット -- 観測時刻を必ず併記する
        # (恒久的な真実として固定しない)。
        row.index_state_observed_at = indexability.generated_at.isoformat()

    def _apply_gsc(self, row: ArticleMeasurement, pages, queries) -> None:
        if pages:
            row.gsc_rows = pages["rows"]
            row.clicks = pages["clicks"]
            row.impressions = pages["impressions"]
            row.average_position = _weighted_position(pages["positions"])
            # 分母が 0 のとき CTR は出さない (0 除算も 0.0 の偽装もしない)。
            row.ctr = round(row.clicks / row.impressions, 6) if row.impressions > 0 else None
        if queries:
            row.query_count = len(queries)
            ranked = sorted(
                queries.values(), key=lambda q: (-q["impressions"], -q["clicks"], q["query"])
            )
            row.top_queries = [
                {
                    "query": q["query"],
                    "clicks": q["clicks"],
                    "impressions": q["impressions"],
                    "average_position": _weighted_position(q["positions"]),
                }
                for q in ranked[:10]
            ]

    def _apply_ga4(self, row: ArticleMeasurement, pages) -> None:
        if not pages:
            return
        row.ga4_rows = pages["rows"]
        total = pages[_CHANNEL_ALL]
        organic = pages[_CHANNEL_ORGANIC]
        row.sessions = total["sessions"]
        row.active_users = total["active_users"]
        row.new_users = total["new_users"]
        row.engaged_sessions = total["engaged_sessions"]
        row.screen_page_views = total["screen_page_views"]
        row.engagement_rate = (
            round(total["engaged_sessions"] / total["sessions"], 6)
            if total["sessions"] > 0
            else None
        )
        row.average_engagement_time_seconds = (
            round(total["engagement_seconds"] / total["active_users"], 2)
            if total["active_users"] > 0
            else None
        )
        row.organic_sessions = organic["sessions"]
        row.organic_active_users = organic["active_users"]

    def _apply_affiliate(self, row: ArticleMeasurement, buckets, programs) -> None:
        if not buckets:
            return
        row.affiliate_clicks = sum(b.clicks for b in buckets)
        row.affiliate_programs = sorted(
            {programs.get(b.affiliate_program_id, str(b.affiliate_program_id)) for b in buckets}
        )
        # 記事単位のクリックは first-party なので帰属できる。成果/報酬は provider
        # 側に記事への join key が無いため、ここでは決して記事に割り当てない。
        row.attribution_scope = ATTRIBUTION_PROGRAM_ONLY
        row.conversions = None
        row.commission_amount = None

    def _states(self, row: ArticleMeasurement, report: MeasurementReport) -> list[str]:
        states: list[str] = []
        if row.impressions > 0:
            states.append(STATE_IMPRESSIONS_OBSERVED)
        elif row.google_index_state == "GSC_INDEXED":
            states.append(STATE_INDEXED_NO_IMPRESSIONS_YET)
        else:
            states.append(STATE_AWAITING_GSC_DATA)
        if row.clicks > 0:
            states.append(STATE_CLICKS_OBSERVED)
        if not report.ga4_configured:
            states.append(STATE_GA4_NOT_CONFIGURED)
        elif row.sessions > 0:
            states.append(STATE_SESSIONS_OBSERVED)
        else:
            states.append(STATE_AWAITING_GA4_DATA)
        if row.affiliate_clicks > 0:
            states.append(STATE_AFFILIATE_CLICKS_OBSERVED)
        return states

    # -- commissions ----------------------------------------------------------
    def _unattributed_commissions(self, start: date, end: date, programs) -> list[dict]:
        """記事に帰属できない報酬を、プログラム単位で **そのまま** 出す。

        Make の commission には click / 記事への join key が無いため、記事別収益は
        作らない。按分も推定もしない。
        """

        rows = self._session.scalars(
            select(AffiliateCommissionFact).where(
                func.date(AffiliateCommissionFact.occurred_at) >= start.isoformat(),
                func.date(AffiliateCommissionFact.occurred_at) <= end.isoformat(),
            )
        ).all()
        grouped: dict[tuple[int, str, str | None], dict] = {}
        for fact in rows:
            key = (fact.affiliate_program_id, fact.provider_status, fact.currency)
            bucket = grouped.setdefault(
                key,
                {
                    "affiliate_program_id": fact.affiliate_program_id,
                    "affiliate_program": programs.get(fact.affiliate_program_id),
                    "provider_status": fact.provider_status,
                    "currency": fact.currency,
                    "conversions": 0,
                    "commission_amount": "0",
                    "attribution": UNATTRIBUTED_TO_ARTICLE,
                },
            )
            bucket["conversions"] += 1
            if fact.commission_amount is not None:
                bucket["commission_amount"] = str(
                    _to_decimal(bucket["commission_amount"]) + fact.commission_amount
                )
        return list(grouped.values())

    # -- data quality (P) -----------------------------------------------------
    def _data_quality(
        self, report, articles, article_keys, gsc_pages, ga4_pages, clicks
    ) -> list[str]:
        findings: list[str] = []
        today = report.window_end

        seen: dict[str | None, int] = {}
        for article in articles:
            key = normalize_url_key(article.published_url or "")
            if key in seen:
                findings.append(
                    f"duplicate article URL mapping: articles {seen[key]} and {article.id} "
                    f"normalize to the same URL"
                )
            seen[key] = article.id

        for key in gsc_pages:
            if key not in article_keys:
                findings.append(f"search console page not mapped to an article: {key}")
        for key in ga4_pages:
            if key not in article_keys:
                findings.append(f"ga4 page not mapped to an article: {key}")

        future_gsc = self._session.scalar(
            select(func.count())
            .select_from(SearchConsolePageDaily)
            .where(SearchConsolePageDaily.metric_date > today)
        )
        future_ga4 = self._session.scalar(
            select(func.count()).select_from(Ga4PageDaily).where(Ga4PageDaily.metric_date > today)
        )
        if future_gsc:
            findings.append(f"{future_gsc} search console rows have a future metric_date")
        if future_ga4:
            findings.append(f"{future_ga4} ga4 rows have a future metric_date")

        negatives = self._session.scalar(
            select(func.count())
            .select_from(SearchConsolePageDaily)
            .where(
                (SearchConsolePageDaily.clicks < 0)
                | (SearchConsolePageDaily.impressions < 0)
                | (SearchConsolePageDaily.position < 0)
            )
        )
        if negatives:
            findings.append(f"{negatives} search console rows have impossible negative metrics")

        for row in report.articles:
            if row.impressions == 0 and row.clicks > 0:
                findings.append(
                    f"article {row.article_id}: clicks without impressions (inconsistent CTR)"
                )
            if row.clicks > row.impressions > 0:
                findings.append(f"article {row.article_id}: clicks exceed impressions")
            if row.organic_sessions > row.sessions:
                findings.append(f"article {row.article_id}: organic sessions exceed total sessions")

        if clicks.unattributed_tokens:
            findings.append(
                f"{clicks.unattributed_clicks} outbound click(s) reference "
                f"{len(clicks.unattributed_tokens)} token(s) with no affiliate link target"
            )
        if clicks.inactive_target_clicks:
            findings.append(
                f"{clicks.inactive_target_clicks} outbound click(s) reference a "
                f"non-active affiliate link target"
            )

        missing_currency = self._session.scalar(
            select(func.count())
            .select_from(AffiliateCommissionFact)
            .where(
                AffiliateCommissionFact.commission_amount.is_not(None),
                AffiliateCommissionFact.currency.is_(None),
            )
        )
        if missing_currency:
            findings.append(f"{missing_currency} commission fact(s) have an amount but no currency")
        return findings


def _empty_ga4() -> dict:
    return {
        "sessions": 0,
        "active_users": 0,
        "new_users": 0,
        "engaged_sessions": 0,
        "screen_page_views": 0,
        "engagement_seconds": 0.0,
    }


def _weighted_position(pairs: list[tuple[float, int]]) -> float | None:
    """表示回数で重み付けした平均掲載順位。母数が 0 なら ``None``。"""

    weight = sum(max(impressions, 0) for _position, impressions in pairs)
    if weight <= 0:
        return None
    total = sum(position * max(impressions, 0) for position, impressions in pairs)
    return round(total / weight, 2)


def _to_decimal(value: str):
    from decimal import Decimal

    return Decimal(value)
