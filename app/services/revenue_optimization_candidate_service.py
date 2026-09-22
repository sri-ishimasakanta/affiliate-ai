"""RevenueOptimizationCandidateService -- 収益最適化候補の評価 (C7)。

C6 が「どの SEO 改善が正当化されるか」に答えるのに対し、ここは「どの収益化の改善が
正当化されるか」に答える。責務は分ける。

**2 つ目の収益基盤は作らない** -- 既存のものを合成するだけ:

- 記事ごとの GA4 / GSC / クリック集計 -- :class:`ArticleMeasurementReportService`
- 信頼できるクリックの切り出し -- :class:`AffiliateCleanClickService`
- カタログ / target / mapping -- :class:`AffiliateProgram` /
  :class:`AffiliateLinkTarget` / :class:`ArticleLinkSubstitutionMapping`
- 報酬 -- :class:`AffiliateCommissionFact` (プログラム単位)
- 閾値 -- ``app/config/revenue_policy.json``
- 判定ルール -- :mod:`app.revenue.candidates` (pure)

**評価は read-only** -- 記事本文にも WordPress にも analytics のソース表にも
書き込まない。``persist()`` を明示的に呼んだときだけ履歴を残す。

守る約束:

- 自分たちの計測用クリックを読者行動として扱わない (信頼境界より前は除外)。
- データが無いことを「成果が悪い」と言わない。
- 帰属できない報酬を記事へ配分しない。記事別収益は ``None`` であって 0 ではない。
- live の ``/go/`` を叩いて健全性を確認しない (それ自体がクリックを作るため)。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.article.fact_freshness import to_storage_utc
from app.models import (
    AffiliateCommissionFact,
    AffiliateLinkTarget,
    AffiliateProgram,
    Article,
    ArticleLinkSubstitutionMapping,
)
from app.models.revenue_optimization_candidate import RevenueOptimizationCandidate
from app.models.revenue_optimization_run import RevenueOptimizationRun
from app.revenue.candidates import (
    CANDIDATE_TYPES,
    RevenueCandidate,
    evaluate_click_through,
    evaluate_commission_signal,
    evaluate_coverage,
    evaluate_data_quality,
    evaluate_high_traffic_unmonetized,
    evaluate_link_health,
    evaluate_tracking_setup,
    evaluate_zero_click,
)
from app.revenue.maturity import (
    ATTRIBUTION_PROGRAM_ONLY,
    ATTRIBUTION_UNAVAILABLE,
    assess_revenue_maturity,
)
from app.revenue.policy import PRIORITIES, RevenuePolicy, get_policy
from app.services.affiliate_clean_click_service import AffiliateCleanClickService
from app.services.article_measurement_report_service import ArticleMeasurementReportService

_AFFILIATE_MODE = "affiliate"


@dataclass
class ArticleRevenueEvaluation:
    article_id: int
    url: str
    published_at: str | None
    article_type: str | None
    monetization_mode: str | None

    maturity_state: str = ""
    maturity_reason: str = ""
    attribution: str = ATTRIBUTION_UNAVAILABLE
    attribution_reason: str = ""

    monetized: bool = False
    active_target_count: int = 0
    active_mapping_count: int = 0
    linked_programs: list[dict] = field(default_factory=list)

    # 流入は欠測を 0 にしない (None のまま)。
    sessions: int | None = None
    organic_sessions: int | None = None
    raw_clicks: int = 0
    excluded_clicks: int = 0
    clean_clicks: int = 0
    clicks_per_100_organic_sessions: float | None = None
    #: 記事別の収益は帰属できないため常に None (0 ではない)。
    article_level_revenue: None = None

    candidates: list[dict] = field(default_factory=list)
    no_action_reason: str | None = None


@dataclass
class RevenueCandidateReport:
    generated_at: datetime
    policy_version: str
    window_start: date
    window_end: date
    trusted_measurement_start_at: datetime | None
    trusted_measurement_rationale: list[str]
    ga4_data_through: date | None
    ga4_configured: bool
    affiliate_click_data_through: date | None
    clean_click_data_through: date | None
    commission_data_through: date | None
    raw_clicks: int
    excluded_clicks: int
    clean_clicks: int
    windows_overlap: bool
    article_count: int
    monetized_article_count: int = 0
    articles: list[ArticleRevenueEvaluation] = field(default_factory=list)
    program_candidates: list[dict] = field(default_factory=list)
    data_quality_candidates: list[dict] = field(default_factory=list)
    candidate_counts: dict[str, int] = field(default_factory=dict)
    priority_counts: dict[str, int] = field(default_factory=dict)
    maturity_counts: dict[str, int] = field(default_factory=dict)
    program_commissions: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def total_candidates(self) -> int:
        return (
            sum(len(a.candidates) for a in self.articles)
            + len(self.program_candidates)
            + len(self.data_quality_candidates)
        )

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["generated_at"] = self.generated_at.isoformat()
        payload["trusted_measurement_start_at"] = (
            self.trusted_measurement_start_at.isoformat()
            if self.trusted_measurement_start_at
            else None
        )
        for key in (
            "window_start",
            "window_end",
            "ga4_data_through",
            "affiliate_click_data_through",
            "clean_click_data_through",
            "commission_data_through",
        ):
            value = getattr(self, key)
            payload[key] = value.isoformat() if value else None
        return payload


class RevenueOptimizationCandidateService:
    def __init__(self, session: Session, *, settings, policy: RevenuePolicy | None = None) -> None:
        self._session = session
        self._settings = settings
        self._policy = policy or get_policy()

    # -- public ---------------------------------------------------------------
    def evaluate(self, *, days: int = 30, now: datetime | None = None) -> RevenueCandidateReport:
        now = now or datetime.now(UTC)
        today = now.date()
        policy = self._policy

        measurement = ArticleMeasurementReportService(self._session, settings=self._settings).build(
            days=days, indexability=None, now=now
        )
        baseline = AffiliateCleanClickService(self._session, policy=policy).build(
            window_start=measurement.window_start, window_end=measurement.window_end
        )

        commissions, commission_through = self._program_commissions(
            measurement.window_start, measurement.window_end
        )
        windows_overlap = _windows_overlap(
            measurement.ga4_data_through, baseline.clean_data_through
        )

        report = RevenueCandidateReport(
            generated_at=now,
            policy_version=policy.policy_version,
            window_start=measurement.window_start,
            window_end=measurement.window_end,
            trusted_measurement_start_at=policy.trusted_measurement_start_at,
            trusted_measurement_rationale=policy.trusted_measurement_rationale,
            ga4_data_through=measurement.ga4_data_through,
            ga4_configured=measurement.ga4_configured,
            affiliate_click_data_through=baseline.raw_data_through,
            clean_click_data_through=baseline.clean_data_through,
            commission_data_through=commission_through,
            raw_clicks=baseline.raw_clicks,
            excluded_clicks=baseline.excluded_clicks,
            clean_clicks=baseline.clean_clicks,
            windows_overlap=windows_overlap,
            article_count=measurement.article_count,
            program_commissions=commissions,
        )

        articles = {
            a.id: a
            for a in self._session.scalars(
                select(Article).where(Article.status == "published")
            ).all()
        }
        programs = {p.id: p for p in self._session.scalars(select(AffiliateProgram)).all()}
        targets = self._targets_by_article()
        mappings = self._mappings_by_article()
        linked = self._linked_programs(programs)

        for row in measurement.articles:
            article = articles.get(row.article_id)
            if article is None:
                continue
            article_targets = targets.get(article.id, [])
            article_mappings = mappings.get(article.id, [])
            active_targets = [t for t in article_targets if t["status"] == "active"]
            active_mappings = [m for m in article_mappings if m["status"] == "active"]
            counts = baseline.counts_for(article.id)
            traffic_available = row.ga4_rows > 0
            sessions = row.sessions if traffic_available else None
            organic = row.organic_sessions if traffic_available else None

            maturity = assess_revenue_maturity(
                article_id=article.id,
                monetization_mode=article.monetization_mode,
                published_at=article.published_at,
                today=today,
                active_target_count=len(active_targets),
                active_mapping_count=len(active_mappings),
                traffic_data_available=traffic_available,
                sessions=sessions,
                organic_sessions=organic,
                clean_clicks=counts.clean_clicks,
                excluded_clicks=counts.excluded_clicks,
                raw_clicks=counts.raw_clicks,
                commission_attribution=self._attribution_for(
                    [t["affiliate_program_id"] for t in active_targets], commissions
                ),
                policy=policy,
            )

            found: list[RevenueCandidate] = []
            found.extend(
                evaluate_link_health(
                    article_id=article.id,
                    monetization_mode=article.monetization_mode,
                    targets=article_targets,
                    mappings=article_mappings,
                    policy=policy,
                )
            )
            found.extend(
                evaluate_coverage(
                    article_id=article.id,
                    maturity=maturity,
                    monetization_mode=article.monetization_mode,
                    linked_programs=linked.get(article.id, []),
                    policy=policy,
                )
            )
            found.extend(
                evaluate_tracking_setup(
                    article_id=article.id,
                    maturity=maturity,
                    linked_programs=linked.get(article.id, []),
                    policy=policy,
                )
            )
            candidate = evaluate_high_traffic_unmonetized(
                article_id=article.id,
                maturity=maturity,
                sessions=sessions,
                linked_programs=linked.get(article.id, []),
                policy=policy,
            )
            if candidate:
                found.append(candidate)
            candidate = evaluate_zero_click(
                article_id=article.id,
                maturity=maturity,
                sessions=sessions,
                windows_overlap=windows_overlap,
                policy=policy,
            )
            if candidate:
                found.append(candidate)
            candidate = evaluate_click_through(
                article_id=article.id,
                maturity=maturity,
                organic_sessions=organic,
                windows_overlap=windows_overlap,
                policy=policy,
            )
            if candidate:
                found.append(candidate)

            found = _dedupe(_cap_structural(found, policy))
            evaluation = ArticleRevenueEvaluation(
                article_id=article.id,
                url=row.url,
                published_at=row.published_at,
                article_type=article.article_type,
                monetization_mode=article.monetization_mode,
                maturity_state=maturity.state,
                maturity_reason=maturity.reason,
                attribution=maturity.attribution,
                attribution_reason=maturity.attribution_reason,
                monetized=maturity.monetized,
                active_target_count=len(active_targets),
                active_mapping_count=len(active_mappings),
                linked_programs=linked.get(article.id, []),
                sessions=sessions,
                organic_sessions=organic,
                raw_clicks=counts.raw_clicks,
                excluded_clicks=counts.excluded_clicks,
                clean_clicks=counts.clean_clicks,
                clicks_per_100_organic_sessions=(
                    round(counts.clean_clicks / organic * 100, 2)
                    if organic and maturity.behavioral_evaluation_allowed
                    else None
                ),
                candidates=[_candidate_dict(c) for c in found],
            )
            if not found:
                evaluation.no_action_reason = maturity.reason
            if maturity.monetized:
                report.monetized_article_count += 1
            report.articles.append(evaluation)

        # -- プログラム単位 / データ品質 ---------------------------------------
        clicked_articles = defaultdict(list)
        for article_id, counts in baseline.by_article.items():
            if counts.clean_clicks > 0:
                for program_id in counts.affiliate_program_ids:
                    clicked_articles[program_id].append(article_id)

        for bucket in commissions:
            candidate = evaluate_commission_signal(
                affiliate_program_id=bucket["affiliate_program_id"],
                program_name=bucket["program_name"],
                attribution=bucket["attribution"],
                conversions=bucket["conversions"],
                commission_amount=bucket["commission_amount"],
                currency=bucket["currency"],
                article_ids_with_clean_clicks=clicked_articles.get(
                    bucket["affiliate_program_id"], []
                ),
                policy=policy,
            )
            if candidate:
                report.program_candidates.append(_candidate_dict(candidate))

        for candidate in evaluate_data_quality(
            baseline=baseline,
            ga4_data_through=measurement.ga4_data_through,
            windows_overlap=windows_overlap,
            attribution=(ATTRIBUTION_PROGRAM_ONLY if commissions else ATTRIBUTION_UNAVAILABLE),
            policy=policy,
        ):
            report.data_quality_candidates.append(_candidate_dict(candidate))

        _summarize(report)
        report.notes.append(
            "thresholds are operational heuristics from app/config/revenue_policy.json "
            f"(version {policy.policy_version}); they are not universal conversion benchmarks"
        )
        report.notes.append(
            f"{baseline.excluded_clicks} of {baseline.raw_clicks} outbound click(s) predate the "
            "trusted measurement start and are excluded from behavioural evaluation; "
            "the rows themselves are retained for audit"
        )
        if not measurement.ga4_configured:
            report.notes.append("ga4 is not configured; behavioural evaluation is suppressed")
        elif measurement.ga4_data_through is None:
            report.notes.append("ga4 has no daily rows yet; behavioural evaluation is suppressed")
        report.notes.append(
            "commission data is program-level only; article-level revenue is reported as "
            "unavailable and is never apportioned between articles"
        )
        return report

    def persist(
        self, report: RevenueCandidateReport, *, idempotency_key: str | None = None
    ) -> RevenueOptimizationRun:
        if idempotency_key is not None:
            existing = self._session.scalars(
                select(RevenueOptimizationRun).where(
                    RevenueOptimizationRun.idempotency_key == idempotency_key
                )
            ).first()
            if existing is not None:
                return existing

        run = RevenueOptimizationRun(
            policy_version=report.policy_version,
            window_start=report.window_start,
            window_end=report.window_end,
            # SQLite は tzinfo を落とすため、他の run と同じく UTC の naive wall-clock
            # に正規化して保存する (保存値 = UTC instant で意味を統一する)。
            trusted_measurement_start_at=(
                to_storage_utc(report.trusted_measurement_start_at)
                if report.trusted_measurement_start_at
                else None
            ),
            ga4_data_through=report.ga4_data_through,
            affiliate_click_data_through=report.affiliate_click_data_through,
            commission_data_through=report.commission_data_through,
            raw_clicks=report.raw_clicks,
            excluded_clicks=report.excluded_clicks,
            clean_clicks=report.clean_clicks,
            evaluated_article_count=report.article_count,
            monetized_article_count=report.monetized_article_count,
            candidate_count=report.total_candidates,
            summary_json={
                "candidate_counts": report.candidate_counts,
                "priority_counts": report.priority_counts,
                "maturity_counts": report.maturity_counts,
                "windows_overlap": report.windows_overlap,
                "program_commissions": report.program_commissions,
                # C8 の構造退行監視が「前回どうだったか」を読むための健全な状態。
                # 候補は「欠けている」ことしか記録しないため、正常値もここに残す。
                "monetization_state": {
                    str(e.article_id): {
                        "active_target_count": e.active_target_count,
                        "active_mapping_count": e.active_mapping_count,
                    }
                    for e in report.articles
                },
            },
            notes="; ".join(report.notes) or None,
            idempotency_key=idempotency_key,
        )
        self._session.add(run)
        self._session.flush()

        rows = [
            (evaluation.article_id, candidate)
            for evaluation in report.articles
            for candidate in evaluation.candidates
        ]
        rows += [(None, c) for c in report.program_candidates]
        rows += [(None, c) for c in report.data_quality_candidates]
        for article_id, candidate in rows:
            self._session.add(
                RevenueOptimizationCandidate(
                    revenue_optimization_run_id=run.id,
                    article_id=candidate.get("article_id") or article_id,
                    affiliate_program_id=candidate.get("affiliate_program_id"),
                    candidate_type=candidate["candidate_type"],
                    reason_code=candidate["reason_code"],
                    priority=candidate["priority"],
                    evidence_basis=candidate["evidence_basis"],
                    suggested_action=candidate["suggested_action"],
                    evidence_json=candidate["evidence"],
                    dedupe_key=candidate["dedupe_key"],
                )
            )
        self._session.commit()
        self._session.refresh(run)
        return run

    # -- sources --------------------------------------------------------------
    def _targets_by_article(self) -> dict[int, list[dict]]:
        out: dict[int, list[dict]] = defaultdict(list)
        for target in self._session.scalars(select(AffiliateLinkTarget)).all():
            out[target.article_id].append(
                {
                    "id": target.id,
                    "status": target.status,
                    "affiliate_program_id": target.affiliate_program_id,
                    "destination_host": target.destination_host,
                }
            )
        return out

    def _mappings_by_article(self) -> dict[int, list[dict]]:
        out: dict[int, list[dict]] = defaultdict(list)
        for mapping in self._session.scalars(select(ArticleLinkSubstitutionMapping)).all():
            out[mapping.article_id].append(
                {
                    "id": mapping.id,
                    "status": mapping.status,
                    "affiliate_link_target_id": mapping.affiliate_link_target_id,
                }
            )
        return out

    def _linked_programs(self, programs: dict[int, AffiliateProgram]) -> dict[int, list[dict]]:
        """``article_affiliate_programs`` の関連 (カタログの現状をそのまま読む)。"""

        out: dict[int, list[dict]] = defaultdict(list)
        rows = self._session.execute(
            text(
                "SELECT article_id, affiliate_program_id, is_primary "
                "FROM article_affiliate_programs ORDER BY article_id, affiliate_program_id"
            )
        ).all()
        for article_id, program_id, is_primary in rows:
            program = programs.get(program_id)
            if program is None:
                continue
            out[article_id].append(
                {
                    "id": program.id,
                    "name": program.name,
                    "status": program.status,
                    "tracking_url": bool(program.tracking_url),
                    "is_primary": bool(is_primary),
                }
            )
        return out

    def _program_commissions(self, start: date, end: date) -> tuple[list[dict], date | None]:
        """報酬を **プログラム単位のまま** 集計する。記事へは配分しない。"""

        rows = self._session.scalars(
            select(AffiliateCommissionFact).where(
                func.date(AffiliateCommissionFact.occurred_at) >= start.isoformat(),
                func.date(AffiliateCommissionFact.occurred_at) <= end.isoformat(),
            )
        ).all()
        programs = {p.id: p for p in self._session.scalars(select(AffiliateProgram)).all()}
        grouped: dict[tuple[int, str | None], dict] = {}
        latest: date | None = None
        for fact in rows:
            occurred = fact.occurred_at.date()
            latest = occurred if latest is None or occurred > latest else latest
            key = (fact.affiliate_program_id, fact.currency)
            bucket = grouped.setdefault(
                key,
                {
                    "affiliate_program_id": fact.affiliate_program_id,
                    "program_name": (
                        programs[fact.affiliate_program_id].name
                        if fact.affiliate_program_id in programs
                        else str(fact.affiliate_program_id)
                    ),
                    "currency": fact.currency,
                    "conversions": 0,
                    "commission_amount": "0",
                    # provider に記事への join key が無いため常に program_only。
                    "attribution": ATTRIBUTION_PROGRAM_ONLY,
                    "article_level_revenue": None,
                },
            )
            bucket["conversions"] += 1
            if fact.commission_amount is not None:
                bucket["commission_amount"] = str(
                    Decimal(bucket["commission_amount"]) + fact.commission_amount
                )
        return list(grouped.values()), latest

    def _attribution_for(self, program_ids: list[int], commissions: list[dict]) -> str:
        """記事に成果を結び付けられるか -- Make は常に ``program_only`` 止まり。"""

        if not program_ids:
            return ATTRIBUTION_UNAVAILABLE
        relevant = [c for c in commissions if c["affiliate_program_id"] in set(program_ids)]
        return ATTRIBUTION_PROGRAM_ONLY if relevant else ATTRIBUTION_UNAVAILABLE


# -- helpers -------------------------------------------------------------------
def _candidate_dict(candidate: RevenueCandidate) -> dict:
    key = candidate.dedupe_key or _dedupe_key(candidate)
    return {
        "candidate_type": candidate.candidate_type,
        "reason_code": candidate.reason_code,
        "priority": candidate.priority,
        "evidence_basis": candidate.evidence_basis,
        "suggested_action": candidate.suggested_action,
        "article_id": candidate.article_id,
        "affiliate_program_id": candidate.affiliate_program_id,
        "evidence": candidate.evidence,
        "dedupe_key": key,
    }


def _dedupe_key(candidate: RevenueCandidate) -> str:
    scope = candidate.article_id if candidate.article_id is not None else "-"
    program = candidate.affiliate_program_id if candidate.affiliate_program_id is not None else "-"
    return f"{scope}:{program}:{candidate.candidate_type}:{candidate.reason_code}"


def _dedupe(candidates: list[RevenueCandidate]) -> list[RevenueCandidate]:
    unique: dict[str, RevenueCandidate] = {}
    for candidate in candidates:
        key = candidate.dedupe_key or _dedupe_key(candidate)
        unique.setdefault(key, candidate.with_dedupe_key(key))
    order = {t: i for i, t in enumerate(CANDIDATE_TYPES)}
    return sorted(unique.values(), key=lambda c: (order.get(c.candidate_type, 99), c.dedupe_key))


def _cap_structural(candidates: list[RevenueCandidate], policy: RevenuePolicy):
    """構造的な候補が 1 記事に積み上がりすぎないようにする。"""

    limit = int(policy.gate("structural", "max_candidates_per_article", 2))
    structural = [c for c in candidates if c.evidence_basis == "structural"]
    others = [c for c in candidates if c.evidence_basis != "structural"]
    return others + structural[:limit]


def _windows_overlap(ga4_through: date | None, click_through: date | None) -> bool:
    """流入とクリックの観測期間が重なっているか (どちらか欠ければ重ならない)。"""

    return ga4_through is not None and click_through is not None


def _summarize(report: RevenueCandidateReport) -> None:
    candidate_counts: dict[str, int] = {}
    priority_counts: dict[str, int] = {p: 0 for p in PRIORITIES}
    maturity_counts: dict[str, int] = defaultdict(int)
    everything = (
        [c for e in report.articles for c in e.candidates]
        + report.program_candidates
        + report.data_quality_candidates
    )
    for evaluation in report.articles:
        maturity_counts[evaluation.maturity_state] += 1
    for candidate in everything:
        candidate_counts[candidate["candidate_type"]] = (
            candidate_counts.get(candidate["candidate_type"], 0) + 1
        )
        priority_counts[candidate["priority"]] = priority_counts.get(candidate["priority"], 0) + 1
    report.candidate_counts = candidate_counts
    report.priority_counts = priority_counts
    report.maturity_counts = dict(maturity_counts)
