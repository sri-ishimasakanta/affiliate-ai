"""OperationsWeeklyReportService -- 週次運用レポート (C8.7)。

**新しい分析基盤は作らない。** 既に永続化されている事実を読んで並べるだけの
read-only な service である。DB にも外部にも一切書かない。

守る約束 (これまでの各フェーズの意味論をそのまま持ち込む):

- coverage (取り込みが問い合わせ終えた最終日) と activity (最後に観測できた
  データの日) を **分けて** 出す (C8.5)。
- 欠測を 0 と書かない。``unavailable`` と書く。
- アフィリエイトのクリックは raw / excluded / clean を必ず 3 つ同時に出す (C7)。
- Make の報酬を記事へ按分しない。記事単位は「不明」と書く (C7)。
- C9 の効果は因果を主張しない。成熟していなければ ``insufficient_data`` と書く。
- secret / tracking URL / ``/go/`` token は載せない (最終防壁は
  :func:`~app.operations.notifications.sanitize_payload`)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.change.effect import local_effective_date
from app.models import (
    AffiliateCommissionFact,
    Article,
    ChangeApplication,
    ChangeRequest,
    ChangeRequestApproval,
    Ga4PageDaily,
    OperationsAlert,
    OperationsRun,
    OperationsStepRun,
    RevenueOptimizationCandidate,
    RevenueOptimizationRun,
    SearchConsolePageDaily,
    SeoImprovementCandidate,
    SeoImprovementRun,
)
from app.operations.notifications import sanitize_payload
from app.operations.policy import OperationsPolicy, get_policy

#: 欠測を 0 と書かないための明示的な値。
UNAVAILABLE = "unavailable"

DEFAULT_PERIOD_DAYS = 7


@dataclass
class WeeklyReport:
    """週次レポートの素材。表示は :mod:`app.operations.email` 側の責務ではなく
    :func:`render_weekly_report` が担う。"""

    generated_at: str
    reporting_timezone: str
    period_start: str
    period_end: str
    complete: bool
    incomplete_reasons: list[str] = field(default_factory=list)
    system: dict = field(default_factory=dict)
    search_console: dict = field(default_factory=dict)
    ga4: dict = field(default_factory=dict)
    seo: dict = field(default_factory=dict)
    revenue: dict = field(default_factory=dict)
    content_changes: dict = field(default_factory=dict)
    article_health: dict = field(default_factory=dict)
    alerts: dict = field(default_factory=dict)
    next_attention: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return sanitize_payload(
            {
                "generated_at": self.generated_at,
                "reporting_timezone": self.reporting_timezone,
                "period_start": self.period_start,
                "period_end": self.period_end,
                "complete": self.complete,
                "incomplete_reasons": list(self.incomplete_reasons),
                "system": self.system,
                "search_console": self.search_console,
                "ga4": self.ga4,
                "seo": self.seo,
                "revenue": self.revenue,
                "content_changes": self.content_changes,
                "article_health": self.article_health,
                "alerts": self.alerts,
                "next_attention": list(self.next_attention),
            }
        )


class OperationsWeeklyReportService:
    def __init__(
        self, session: Session, *, settings, policy: OperationsPolicy | None = None
    ) -> None:
        self._session = session
        self._settings = settings
        self._policy = policy or get_policy()

    # -- public ---------------------------------------------------------------
    def build(
        self,
        *,
        operations_run_id: int | None = None,
        period_days: int = DEFAULT_PERIOD_DAYS,
        now: datetime | None = None,
    ) -> WeeklyReport:
        now = now or datetime.now(UTC)
        tz = self._policy.timezone
        today = local_effective_date(now, tz)
        period_start = today - timedelta(days=max(1, period_days) - 1)
        since = datetime.combine(period_start, datetime.min.time())

        run = self._run(operations_run_id)
        report = WeeklyReport(
            generated_at=now.isoformat(),
            reporting_timezone=str(getattr(tz, "key", tz)),
            period_start=period_start.isoformat(),
            period_end=today.isoformat(),
            complete=True,
        )

        report.system = self._system(run)
        coverage = self._coverage()
        report.search_console = self._search_console(coverage, period_start, today)
        report.ga4 = self._ga4(coverage, period_start, today)
        report.seo = self._seo()
        report.revenue = self._revenue(coverage, since)
        report.content_changes = self._content_changes(since)
        report.article_health = self._article_health()
        report.alerts = self._alerts(since)

        self._assess_completeness(report, run)
        report.next_attention = self._next_attention(report)
        return report

    # -- sections -------------------------------------------------------------
    def _run(self, operations_run_id: int | None) -> OperationsRun | None:
        if operations_run_id is not None:
            return self._session.get(OperationsRun, operations_run_id)
        return self._session.scalars(
            select(OperationsRun)
            .where(OperationsRun.profile == "weekly")
            .order_by(OperationsRun.id.desc())
            .limit(1)
        ).first()

    def _system(self, run: OperationsRun | None) -> dict:
        if run is None:
            return {"operations_run_id": None, "status": UNAVAILABLE}
        steps = self._session.scalars(
            select(OperationsStepRun).where(OperationsStepRun.operations_run_id == run.id)
        ).all()
        return {
            "operations_run_id": run.id,
            "profile": run.profile,
            "trigger": run.trigger,
            "status": run.status,
            "effective_date": run.effective_date.isoformat() if run.effective_date else None,
            "timezone": run.timezone_name,
            "policy_version": run.policy_version,
            "step_total": run.step_total,
            "step_succeeded": run.step_succeeded,
            "step_failed": run.step_failed,
            "step_skipped": run.step_skipped,
            "failing_steps": [s.step_name for s in steps if s.status == "failed"],
            "skipped_steps": [s.step_name for s in steps if s.status == "skipped"],
            "partial_steps": [s.step_name for s in steps if s.status == "partial"],
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        }

    def _coverage(self) -> dict:
        from app.services.operations_source_health_service import collect_source_freshness

        return collect_source_freshness(self._session)

    def _search_console(self, coverage: dict, start: date, end: date) -> dict:
        health = coverage.get("search_console")
        rows = self._session.execute(
            select(
                func.count(),
                func.sum(SearchConsolePageDaily.impressions),
                func.sum(SearchConsolePageDaily.clicks),
            ).where(
                SearchConsolePageDaily.metric_date >= start,
                SearchConsolePageDaily.metric_date <= end,
            )
        ).one()
        count, impressions, clicks = rows
        return {
            "coverage_through": _iso(getattr(health, "coverage_through", None)),
            "latest_observed_data_date": _iso(getattr(health, "latest_observed_data_date", None)),
            "last_successful_import_at": _iso(getattr(health, "last_successful_import_at", None)),
            "rows_in_period": count,
            # 行が無いのは「0 回表示された」ではなく「まだ観測が無い」。
            "impressions": impressions if count else UNAVAILABLE,
            "clicks": clicks if count else UNAVAILABLE,
            "note": (
                "Search Console の日付は Pacific Time の暦日。"
                "レポート期間 (Asia/Tokyo) と境界が最大 1 日ずれる。"
            ),
        }

    def _ga4(self, coverage: dict, start: date, end: date) -> dict:
        health = coverage.get("ga4")
        configured = bool((getattr(self._settings, "ga4_property_id", None) or "").strip())
        rows = self._session.execute(
            select(
                func.count(),
                func.sum(Ga4PageDaily.sessions),
            ).where(
                Ga4PageDaily.metric_date >= start,
                Ga4PageDaily.metric_date <= end,
                Ga4PageDaily.channel_scope == "all",
            )
        ).one()
        organic = self._session.execute(
            select(func.count(), func.sum(Ga4PageDaily.sessions)).where(
                Ga4PageDaily.metric_date >= start,
                Ga4PageDaily.metric_date <= end,
                Ga4PageDaily.channel_scope == "organic_search",
            )
        ).one()
        return {
            "configured": configured,
            "coverage_through": _iso(getattr(health, "coverage_through", None)),
            "latest_observed_data_date": _iso(getattr(health, "latest_observed_data_date", None)),
            "last_successful_import_at": _iso(getattr(health, "last_successful_import_at", None)),
            "rows_in_period": rows[0],
            "sessions": rows[1] if rows[0] else UNAVAILABLE,
            "organic_sessions": organic[1] if organic[0] else UNAVAILABLE,
            "maturity": ("observed" if rows[0] else "no measurement in this period yet"),
        }

    def _seo(self) -> dict:
        run = self._session.scalars(
            select(SeoImprovementRun).order_by(SeoImprovementRun.id.desc()).limit(1)
        ).first()
        if run is None:
            return {"latest_run_id": None, "status": UNAVAILABLE}
        previous = self._session.scalars(
            select(SeoImprovementRun)
            .where(SeoImprovementRun.id < run.id)
            .order_by(SeoImprovementRun.id.desc())
            .limit(1)
        ).first()
        current = self._candidates(SeoImprovementCandidate, "seo_improvement_run_id", run.id)
        prior = (
            self._candidates(SeoImprovementCandidate, "seo_improvement_run_id", previous.id)
            if previous
            else {}
        )
        changes = _diff(prior, current)
        return {
            "latest_run_id": run.id,
            "policy_version": run.policy_version,
            "window": f"{run.window_start} .. {run.window_end}",
            "evaluated_article_count": run.evaluated_article_count,
            "candidate_count": run.candidate_count,
            "by_type": _counts(current, "candidate_type"),
            "by_priority": _counts(current, "priority"),
            "compared_with_run_id": previous.id if previous else None,
            **changes,
        }

    def _revenue(self, coverage: dict, since: datetime) -> dict:
        run = self._session.scalars(
            select(RevenueOptimizationRun).order_by(RevenueOptimizationRun.id.desc()).limit(1)
        ).first()
        commissions = self._session.scalar(
            select(func.count()).select_from(AffiliateCommissionFact)
        )
        if run is None:
            return {
                "latest_run_id": None,
                "status": UNAVAILABLE,
                "commission_rows": commissions,
                "article_level_revenue": UNAVAILABLE,
            }
        current = self._candidates(
            RevenueOptimizationCandidate, "revenue_optimization_run_id", run.id
        )
        return {
            "latest_run_id": run.id,
            "policy_version": run.policy_version,
            "window": f"{run.window_start} .. {run.window_end}",
            "evaluated_article_count": run.evaluated_article_count,
            "monetized_article_count": run.monetized_article_count,
            # 3 つ同時に出す。clean だけを見せない。
            "raw_clicks": run.raw_clicks,
            "excluded_clicks": run.excluded_clicks,
            "clean_clicks": run.clean_clicks,
            "trusted_measurement_start_at": _iso(run.trusted_measurement_start_at),
            "affiliate_click_data_through": _iso(run.affiliate_click_data_through),
            "commission_data_through": _iso(run.commission_data_through),
            "commission_rows": commissions,
            "candidate_count": run.candidate_count,
            "by_type": _counts(current, "candidate_type"),
            "by_priority": _counts(current, "priority"),
            # provider 側に記事への join key が無い。按分しない。
            "article_level_revenue": UNAVAILABLE,
            "attribution_note": (
                "クリックは first-party なので記事へ帰属できるが、報酬は provider 側に "
                "記事への join key が無いため記事単位に割り当てない。"
            ),
        }

    def _content_changes(self, since: datetime) -> dict:
        created = self._session.scalars(
            select(ChangeRequest).where(ChangeRequest.created_at >= since)
        ).all()
        approvals = self._session.scalars(
            select(ChangeRequestApproval).where(ChangeRequestApproval.created_at >= since)
        ).all()
        applications = self._session.scalars(
            select(ChangeApplication)
            .where(ChangeApplication.attempted_at >= since)
            .order_by(ChangeApplication.id)
        ).all()
        effects = self._effects()
        return {
            "requests_created": len(created),
            "approved": sum(1 for a in approvals if a.decision == "approved"),
            "rejected": sum(1 for a in approvals if a.decision == "rejected"),
            "applications_attempted": len(applications),
            "applications_succeeded": sum(
                1 for a in applications if a.outcome in ("succeeded", "reconciled")
            ),
            "applications_failed": sum(1 for a in applications if a.outcome == "failed"),
            "awaiting_approval": self._session.scalar(
                select(func.count())
                .select_from(ChangeRequest)
                .where(ChangeRequest.status == "awaiting_approval")
            ),
            "latest_applied": [
                {
                    "change_request_id": a.change_request_id,
                    "article_id": a.article_id,
                    "outcome": a.outcome,
                    "attempted_at": _iso(a.attempted_at),
                }
                for a in applications
                if a.outcome in ("succeeded", "reconciled")
            ][-5:],
            "effect_tracking": effects,
            # 相関ですらない並置なので、改善したとは書かない。
            "causal_claim": "none",
        }

    def _effects(self) -> list[dict]:
        from app.services.change_effect_service import ChangeEffectService

        report = ChangeEffectService(self._session, settings=self._settings).build()
        return [
            {
                "change_request_id": e.change_request_id,
                "article_id": e.article_id,
                "change_date": e.change_date,
                "maturity": e.maturity["status"],
                "reasons": e.maturity["reasons"],
                "causal_claim": e.causal_claim,
            }
            for e in report.effects
        ]

    def _article_health(self) -> dict:
        published = self._session.scalar(
            select(func.count()).select_from(Article).where(Article.status == "published")
        )
        drafts = self._session.scalar(
            select(func.count()).select_from(Article).where(Article.status == "draft")
        )
        monetized = self._session.scalar(
            select(func.count())
            .select_from(Article)
            .where(Article.status == "published", Article.monetization_mode == "affiliate")
        )
        missing_url = self._session.scalar(
            select(func.count())
            .select_from(Article)
            .where(Article.status == "published", Article.published_url.is_(None))
        )
        return {
            "published": published,
            "drafts": drafts,
            "monetized_published": monetized,
            "published_without_url": missing_url,
            # HTTP / indexability の実測は live probe が必要なので、この read-only な
            # レポートでは行わない。直近の監視アラートで代替する。
            "indexability_note": (
                "live probe はこのレポートでは行わない。"
                "regression は ALERTS セクションの indexability アラートで見る。"
            ),
        }

    def _alerts(self, since: datetime) -> dict:
        opened = self._session.scalars(
            select(OperationsAlert).where(OperationsAlert.first_seen_at >= since)
        ).all()
        resolved = self._session.scalars(
            select(OperationsAlert).where(
                OperationsAlert.resolved_at.is_not(None), OperationsAlert.resolved_at >= since
            )
        ).all()
        open_now = self._session.scalars(
            select(OperationsAlert)
            .where(OperationsAlert.status == "open")
            .order_by(OperationsAlert.last_seen_at.desc())
        ).all()
        return {
            "newly_opened": len(opened),
            "resolved_in_period": len(resolved),
            "currently_open": len(open_now),
            "persisted": sum(1 for a in open_now if a.first_seen_at < since),
            "open_details": [
                {
                    "severity": a.severity,
                    "alert_type": a.alert_type,
                    "title": a.title,
                    "occurrence_count": a.occurrence_count,
                    "last_seen_at": _iso(a.last_seen_at),
                    "article_id": a.article_id,
                }
                for a in open_now[:10]
            ],
        }

    # -- completeness / attention --------------------------------------------
    def _assess_completeness(self, report: WeeklyReport, run: OperationsRun | None) -> None:
        reasons: list[str] = []
        if run is None:
            reasons.append("no weekly operations run has been recorded yet")
        elif run.status != "succeeded":
            reasons.append(f"the weekly operations run finished as {run.status}")
        if report.system.get("failing_steps"):
            reasons.append(f"failed steps: {', '.join(report.system['failing_steps'])}")
        if report.system.get("skipped_steps"):
            reasons.append(f"skipped steps: {', '.join(report.system['skipped_steps'])}")
        report.incomplete_reasons = reasons
        report.complete = not reasons

    def _next_attention(self, report: WeeklyReport) -> list[str]:
        items: list[str] = []
        for reason in report.incomplete_reasons:
            items.append(f"operations: {reason}")
        open_alerts = report.alerts.get("currently_open") or 0
        if open_alerts:
            items.append(f"alerts: {open_alerts} open alert(s) need a human decision")
        awaiting = report.content_changes.get("awaiting_approval") or 0
        if awaiting:
            items.append(f"change requests: {awaiting} proposal(s) awaiting human approval")
        failed = report.content_changes.get("applications_failed") or 0
        if failed:
            items.append(f"change applications: {failed} failed attempt(s) in this period")
        if report.article_health.get("published_without_url"):
            items.append("articles: published article(s) without a canonical URL")
        return items

    # -- helpers --------------------------------------------------------------
    def _candidates(self, model, run_field: str, run_id: int) -> dict[str, dict]:
        rows = self._session.scalars(select(model).where(getattr(model, run_field) == run_id)).all()
        return {
            row.dedupe_key: {
                "candidate_type": row.candidate_type,
                "priority": row.priority,
                "article_id": row.article_id,
            }
            for row in rows
        }


def _diff(prior: dict[str, dict], current: dict[str, dict]) -> dict:
    """前回 run との差。「消えた」を「直った」と書かない (C8 の規約)。"""

    new = sorted(set(current) - set(prior))
    persisted = sorted(set(current) & set(prior))
    gone = sorted(set(prior) - set(current))
    escalated, decreased = [], []
    order = {"low": 0, "medium": 1, "high": 2}
    for key in persisted:
        before = order.get(prior[key]["priority"], 0)
        after = order.get(current[key]["priority"], 0)
        if after > before:
            escalated.append(key)
        elif after < before:
            decreased.append(key)
    return {
        "new": len(new),
        "persisted": len(persisted),
        "no_longer_present": len(gone),
        "priority_increased": len(escalated),
        "priority_decreased": len(decreased),
        "no_longer_present_note": (
            "消えたことは直ったことではない。データ/期間/ポリシーの変化でも消える。"
        ),
    }


def _counts(rows: dict[str, dict], field_name: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows.values():
        key = str(row.get(field_name))
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def _iso(value) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)
