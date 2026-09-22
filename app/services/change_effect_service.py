"""ChangeEffectService -- 適用済み変更の before/after 観測 (C9.3)。

read-only。DB にも WordPress にも書かない。やることは 1 つだけ:

    「この変更の前後で、同じ長さの窓の同じ指標はどうだったか」

を並べ、**それを信じてよいかを一緒に返す**。

意図的にやらないこと:

- 因果の主張 (``causal_claim`` は常に ``none``)
- 合成スコア (「改善度 72」のような 1 つの数に畳まない)
- 欠損の穴埋め (観測が無い側は ``None`` のまま。0 にしない)
- 成熟前の判定 (窓が経過していない/取り込みが届いていなければ
  ``insufficient_data`` を返し、差分は参考値として添えるだけ)

アフィリエイトクリックは C7 の ``trusted_measurement_start_at`` より前を
**読者行動として数えない** (計測用リクエストが作ったクリックのため)。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.change.effect import (
    CAUSAL_CLAIM_NONE,
    DEFAULT_MINIMUM_IMPRESSIONS,
    DEFAULT_WINDOW_DAYS,
    EffectWindow,
    assess_maturity,
    build_windows,
    delta,
)
from app.models import (
    Article,
    ChangeApplication,
    ChangeRequest,
    Ga4PageDaily,
    SearchConsolePageDaily,
)
from app.revenue.policy import load_policy
from app.seo.url_normalization import normalize_url_key
from app.services.affiliate_click_metrics_service import AffiliateClickMetricsService

#: 効果を観測できる適用結果 (失敗した適用は記事を変えていないので比較しない)。
APPLIED_OUTCOMES = ("succeeded", "reconciled")

_CHANNEL_ORGANIC = "organic_search"


@dataclass
class WindowMetrics:
    """1 つの窓の観測。観測が無い指標は ``None`` のままにする。"""

    rows: int = 0
    impressions: int | None = None
    clicks: int | None = None
    position: float | None = None
    ga4_sessions: int | None = None
    ga4_organic_sessions: int | None = None
    affiliate_clicks: int | None = None
    affiliate_clicks_trusted: bool = True

    def as_dict(self) -> dict:
        return {
            "rows": self.rows,
            "impressions": self.impressions,
            "clicks": self.clicks,
            "position": self.position,
            "ga4_sessions": self.ga4_sessions,
            "ga4_organic_sessions": self.ga4_organic_sessions,
            "affiliate_clicks": self.affiliate_clicks,
            "affiliate_clicks_trusted": self.affiliate_clicks_trusted,
        }


@dataclass
class ChangeEffect:
    """1 件の適用に対する before/after。"""

    change_application_id: int
    change_request_id: int
    article_id: int
    article_url: str
    change_type: str
    proposal_hash: str
    applied_at: str
    change_date: str
    window_days: int
    pre_window: dict
    post_window: dict
    pre: dict
    post: dict
    deltas: dict
    maturity: dict
    #: 常に ``"none"``。差分は相関ですらなく、並置に過ぎない。
    causal_claim: str = CAUSAL_CLAIM_NONE
    caveats: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "change_application_id": self.change_application_id,
            "change_request_id": self.change_request_id,
            "article_id": self.article_id,
            "article_url": self.article_url,
            "change_type": self.change_type,
            "proposal_hash": self.proposal_hash,
            "applied_at": self.applied_at,
            "change_date": self.change_date,
            "window_days": self.window_days,
            "pre_window": self.pre_window,
            "post_window": self.post_window,
            "pre": self.pre,
            "post": self.post,
            "deltas": self.deltas,
            "maturity": self.maturity,
            "causal_claim": self.causal_claim,
            "caveats": list(self.caveats),
        }


@dataclass
class ChangeEffectReport:
    generated_at: str
    window_days: int
    minimum_impressions: int
    gsc_coverage_through: str | None
    ga4_coverage_through: str | None
    trusted_click_measurement_start_at: str | None
    effects: list[ChangeEffect] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "window_days": self.window_days,
            "minimum_impressions": self.minimum_impressions,
            "gsc_coverage_through": self.gsc_coverage_through,
            "ga4_coverage_through": self.ga4_coverage_through,
            "trusted_click_measurement_start_at": self.trusted_click_measurement_start_at,
            "effects": [effect.as_dict() for effect in self.effects],
            "notes": list(self.notes),
        }


_CAVEATS = (
    "順位変動・季節性・他ページの更新・Google 側の更新は観測できない。",
    "この並置は因果を示さない。単独の変更を効果として報告してはいけない。",
    "窓が短いほど、差は変更ではなく通常の変動を写している可能性が高い。",
)


class ChangeEffectService:
    def __init__(self, session: Session, *, settings) -> None:
        self._session = session
        self._settings = settings

    # -- public ---------------------------------------------------------------
    def build(
        self,
        *,
        window_days: int = DEFAULT_WINDOW_DAYS,
        minimum_impressions: int = DEFAULT_MINIMUM_IMPRESSIONS,
        request_id: int | None = None,
        now: datetime | None = None,
    ) -> ChangeEffectReport:
        now = now or datetime.now(UTC)
        today = now.date()
        coverage = self._coverage_through()
        policy = load_policy()
        trusted_from = policy.trusted_measurement_start_at

        report = ChangeEffectReport(
            generated_at=now.isoformat(),
            window_days=window_days,
            minimum_impressions=minimum_impressions,
            gsc_coverage_through=_iso(coverage.get("search_console")),
            ga4_coverage_through=_iso(coverage.get("ga4")),
            trusted_click_measurement_start_at=(trusted_from.isoformat() if trusted_from else None),
        )

        applications = self._applications(request_id)
        if not applications:
            report.notes.append(
                "no applied change exists yet; effect tracking has nothing to compare"
            )
            return report

        base = (self._settings.wordpress_base_url or "").rstrip("/")
        for application in applications:
            effect = self._effect_for(
                application,
                base=base,
                today=today,
                window_days=window_days,
                minimum_impressions=minimum_impressions,
                gsc_coverage=coverage.get("search_console"),
                trusted_from=trusted_from,
            )
            if effect is not None:
                report.effects.append(effect)
        return report

    # -- internals ------------------------------------------------------------
    def _applications(self, request_id: int | None) -> list[ChangeApplication]:
        stmt = select(ChangeApplication).where(ChangeApplication.outcome.in_(APPLIED_OUTCOMES))
        if request_id is not None:
            stmt = stmt.where(ChangeApplication.change_request_id == request_id)
        return list(self._session.scalars(stmt.order_by(ChangeApplication.id)).all())

    def _effect_for(
        self,
        application: ChangeApplication,
        *,
        base: str,
        today: date,
        window_days: int,
        minimum_impressions: int,
        gsc_coverage: date | None,
        trusted_from: datetime | None,
    ) -> ChangeEffect | None:
        article = self._session.get(Article, application.article_id)
        request = self._session.get(ChangeRequest, application.change_request_id)
        if article is None or request is None:
            return None

        applied_at = application.finished_at or application.attempted_at
        change_date = applied_at.date()
        pre, post = build_windows(
            change_date=change_date,
            today=today,
            window_days=window_days,
            coverage_through=gsc_coverage,
        )

        key = normalize_url_key(article.published_url or "")
        pre_metrics = self._measure(pre, key, base, article.id, trusted_from)
        post_metrics = self._measure(post, key, base, article.id, trusted_from)

        maturity = assess_maturity(
            pre=pre,
            post=post,
            today=today,
            pre_impressions=pre_metrics.impressions or 0,
            post_impressions=post_metrics.impressions or 0,
            pre_rows=pre_metrics.rows,
            minimum_impressions=minimum_impressions,
        )
        caveats = list(_CAVEATS)
        if not maturity.sufficient:
            caveats.insert(
                0,
                "成熟していない。以下の差分は結論ではなく、経過観察のための参考値である。",
            )

        return ChangeEffect(
            change_application_id=application.id,
            change_request_id=application.change_request_id,
            article_id=article.id,
            article_url=article.published_url or "",
            change_type=request.change_type,
            proposal_hash=application.proposal_hash,
            applied_at=applied_at.isoformat(),
            change_date=change_date.isoformat(),
            window_days=window_days,
            pre_window=pre.as_dict(),
            post_window=post.as_dict(),
            pre=pre_metrics.as_dict(),
            post=post_metrics.as_dict(),
            deltas=_deltas(pre_metrics, post_metrics),
            maturity=maturity.as_dict(),
            caveats=caveats,
        )

    def _measure(
        self,
        window: EffectWindow,
        url_key: str | None,
        base: str,
        article_id: int,
        trusted_from: datetime | None,
    ) -> WindowMetrics:
        metrics = WindowMetrics()

        rows = self._session.scalars(
            select(SearchConsolePageDaily).where(
                SearchConsolePageDaily.metric_date >= window.start,
                SearchConsolePageDaily.metric_date <= window.end,
            )
        ).all()
        weighted: list[tuple[float, int]] = []
        for row in rows:
            if normalize_url_key(row.page) != url_key:
                continue
            metrics.rows += 1
            metrics.impressions = (metrics.impressions or 0) + row.impressions
            metrics.clicks = (metrics.clicks or 0) + row.clicks
            weighted.append((row.position, row.impressions))
        metrics.position = _weighted_position(weighted)

        ga4_rows = self._session.scalars(
            select(Ga4PageDaily).where(
                Ga4PageDaily.metric_date >= window.start,
                Ga4PageDaily.metric_date <= window.end,
            )
        ).all()
        sessions: dict[str, int] = defaultdict(int)
        seen_ga4 = False
        for row in ga4_rows:
            if normalize_url_key(f"{base}{row.page_path}") != url_key:
                continue
            seen_ga4 = True
            sessions[row.channel_scope] += row.sessions
        if seen_ga4:
            metrics.ga4_sessions = sessions.get("all", 0)
            metrics.ga4_organic_sessions = sessions.get(_CHANNEL_ORGANIC, 0)

        # 信頼できる計測開始より前の窓では、クリックを読者行動として数えない。
        trusted_date = trusted_from.date() if trusted_from else None
        if trusted_date is not None and window.start < trusted_date:
            metrics.affiliate_clicks = None
            metrics.affiliate_clicks_trusted = False
        else:
            buckets = AffiliateClickMetricsService(self._session).aggregate(
                start_date=window.start, end_date=window.end
            )
            metrics.affiliate_clicks = sum(b.clicks for b in buckets.for_article(article_id))
        return metrics

    def _coverage_through(self) -> dict[str, date | None]:
        from app.services.operations_source_health_service import collect_source_freshness

        return {
            name: value.coverage_through
            for name, value in collect_source_freshness(self._session).items()
        }


def _deltas(pre: WindowMetrics, post: WindowMetrics) -> dict:
    return {
        "impressions": delta(pre.impressions, post.impressions),
        "clicks": delta(pre.clicks, post.clicks),
        "position": delta(pre.position, post.position),
        "ga4_sessions": delta(pre.ga4_sessions, post.ga4_sessions),
        "ga4_organic_sessions": delta(pre.ga4_organic_sessions, post.ga4_organic_sessions),
        "affiliate_clicks": (
            delta(pre.affiliate_clicks, post.affiliate_clicks)
            if pre.affiliate_clicks_trusted and post.affiliate_clicks_trusted
            else None
        ),
    }


def _weighted_position(pairs: list[tuple[float, int]]) -> float | None:
    total = sum(weight for _, weight in pairs)
    if not pairs:
        return None
    if total <= 0:
        return round(sum(position for position, _ in pairs) / len(pairs), 2)
    return round(sum(position * weight for position, weight in pairs) / total, 2)


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None
