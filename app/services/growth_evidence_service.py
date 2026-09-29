"""GrowthEvidenceService -- 既存の計測から、記事・キーワードごとの成長の証拠を集める (C9)。

**読むだけ。** DB に書かない (``read_only_session`` で flush を止め、最後に rollback)。外の
サービスに問い合わせない (Search Console・GA4・Threads・WordPress・OpenAI・/go/ のどれにも)。

集め方は既存のものを使い、同じ集計を作り直さない:

- 鮮度: ``collect_source_freshness`` + ``evaluate_source_refresh`` (C8.4)
- SEO / GA4 と SEO の候補: ``SeoImprovementCandidateService.evaluate`` (C6)
- クリック (信頼できる計測開始より後だけ) と収益の候補: ``RevenueOptimizationCandidateService``
  (C7、``AffiliateCleanClickService`` を使う)
- 計測のデータの質・プログラム単位の報酬: ``ArticleMeasurementReportService.build`` (C5)
- 索引: 保存済みの ``check_indexability`` の結果 (``operations_step_runs``)。live の検査はしない
- Threads: ``ThreadsPerformanceAnalysisService.report`` (T6.5)
- キーワード: ``KeywordScoreRepository.get_latest``
- Growth の枠: ``ThreadsGrowthService.plan`` (読むだけ)
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware
from app.growth import analysis as ga
from app.models import (
    TP_APPROVED,
    TP_OPEN_STATES,
    Article,
    Keyword,
    ThreadsPostProposal,
    ThreadsPublication,
    WordPressContentUpdateRun,
)
from app.services.threads_learning_service import read_only_session

DEFAULT_WINDOW_DAYS = 28


def _iso(value) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


class GrowthEvidenceService:
    def __init__(self, session: Session, *, settings=None, timezone: ZoneInfo | None = None,
                 include_threads: bool = True,
                 include_growth_lane: bool = True, followup=None) -> None:  # fmt: skip
        self._session = session
        #: C9-C: 追跡の観測の出どころ (``now`` → {記事: 観測}。None なら読むだけで求める)。
        self._followup = followup
        if settings is None:
            from app.config.settings import get_settings

            settings = get_settings()
        self._settings = settings
        if timezone is None:
            from app.operations.policy import get_policy as get_ops_policy

            timezone = get_ops_policy().timezone
        self._tz = timezone
        self._include_threads = include_threads
        self._include_growth_lane = include_growth_lane

    # -- public ------------------------------------------------------------------------------
    def collect(self, *, now: datetime | None = None, days: int = DEFAULT_WINDOW_DAYS) -> dict:
        """証拠を集める。返り値は ``evidence`` (``GrowthEvidence`` の並び) とサイトの情報。"""

        now = ensure_aware(now or datetime.now(UTC))
        with read_only_session(self._session):
            try:
                bundle = self._collect(now, days)
            finally:
                self._session.rollback()
        return bundle

    # -- internals ---------------------------------------------------------------------------
    def _collect(self, now: datetime, days: int) -> dict:
        from app.revenue.policy import get_policy as get_revenue_policy
        from app.services.article_measurement_report_service import (
            ArticleMeasurementReportService,
        )
        from app.services.revenue_optimization_candidate_service import (
            RevenueOptimizationCandidateService,
        )
        from app.services.seo_improvement_candidate_service import SeoImprovementCandidateService

        freshness, freshness_detail = self._freshness(now)
        measurement = ArticleMeasurementReportService(self._session, settings=self._settings).build(
            days=days, now=now)  # fmt: skip
        seo = SeoImprovementCandidateService(self._session, settings=self._settings).evaluate(
            days=days, now=now)  # fmt: skip
        revenue = RevenueOptimizationCandidateService(
            self._session, settings=self._settings).evaluate(days=days, now=now)  # fmt: skip
        trusted = get_revenue_policy().trusted_measurement_start_at
        seo_rows = {a.article_id: a for a in seo.articles}
        rev_rows = {a.article_id: a for a in revenue.articles}
        index_rows, index_observed = self._index_snapshot(now)
        updates = self._last_updates()
        articles = self._session.scalars(select(Article).order_by(Article.id)).all()
        keywords = {k.id: k for k in self._session.scalars(select(Keyword).order_by(Keyword.id))}
        scores = self._latest_scores(keywords, now)
        open_props = self._open_proposals()
        # 以下は Threads の分析の前に、値だけの形にしておく (分析は自分で rollback する)。
        article_rows = [self._article_row(a, now, updates, rev_rows.get(a.id), keywords)
                        for a in articles]  # fmt: skip
        threads_posts = self._threads_posts(now) if self._include_threads else {}
        recent_posts = sorted((p for posts in threads_posts.values() for p in posts),
                              key=lambda p: p.get("published_at") or "", reverse=True)
        # サイト全体の直近 1〜2 本の通常の投稿の切り口 (既存の弱い好みと同じく避ける)。
        recent_regular_angles = [p.get("angle") for p in recent_posts[:2] if p.get("angle")]
        growth_plan = self._growth_plan(now) if self._include_growth_lane else None
        followup = self._followup_for(now)

        evidence: list[ga.GrowthEvidence] = []
        linked_keywords = set()
        for row in article_rows:
            aid = row["article_id"]
            if row["keyword_id"] is not None:
                linked_keywords.add(row["keyword_id"])
            s = seo_rows.get(aid)
            r = rev_rows.get(aid)
            score = scores.get(row["keyword_id"]) if row["keyword_id"] is not None else None
            sources = {
                "seo": ga.seo_source(
                    getattr(s, "gsc", None), maturity=getattr(s, "maturity_search", None),
                    freshness=freshness["search_console"],
                    coverage_through=freshness_detail["search_console"]["coverage_through"],
                    maturity_reason=getattr(s, "maturity_search_reason", "")),
                "ga4": ga.ga4_source(
                    getattr(s, "ga4", None) or {"configured": measurement.ga4_configured},
                    maturity=getattr(s, "maturity_engagement", None),
                    freshness=freshness["ga4"],
                    coverage_through=freshness_detail["ga4"]["coverage_through"],
                    maturity_reason=getattr(s, "maturity_engagement_reason", "")),
                "affiliate": ga.affiliate_source(
                    _revenue_dict(r), freshness=freshness["affiliate_clicks"],
                    data_through=_iso(revenue.clean_click_data_through),
                    trusted_start=_iso(trusted)),
                "commission": ga.commission_source(
                    ever_had_data=freshness_detail["make_commissions"]["ever_had_data"],
                    attribution=getattr(r, "attribution", None),
                    coverage_through=freshness_detail["make_commissions"]["coverage_through"]),
                "keyword": ga.keyword_source(score, age_days=(score or {}).get("age_days")),
                "threads": ga.threads_source(threads_posts.get(aid, []),
                                             open_proposals=open_props.get(aid, ())),
                "index": ga.index_source(index_rows.get(aid), observed_at=index_observed),
            }  # fmt: skip
            evidence.append(ga.GrowthEvidence(
                subject_type="article", subject_id=f"article:{aid}", article_id=aid,
                keyword_id=row["keyword_id"], article=row, sources=sources,
                followup=followup.get(aid, ()),
                existing_candidates={
                    "seo": tuple(_candidate(c, "seo") for c in getattr(s, "candidates", ())),
                    "revenue": tuple(_candidate(c, "revenue")
                                     for c in getattr(r, "candidates", ())),
                },
            ))  # fmt: skip
        keyword_context: dict[int, dict] = {}
        unlinked = [k for k in keywords.values()
                    if k.id not in linked_keywords and k.status not in ("rejected",)]  # fmt: skip
        totals = [scores[k.id]["total_score"] for k in unlinked
                  if k.id in scores and scores[k.id].get("total_score") is not None]  # fmt: skip
        commercial = {k.id: _commercial(scores.get(k.id)) for k in unlinked}
        commercial_values = [v for v in commercial.values() if v is not None]
        for k in unlinked:
            score = scores.get(k.id)
            total = (score or {}).get("total_score")
            others = list(totals)
            if total is not None:
                others.remove(total)
            pct = ga.percentile_of(total, others) if total is not None else None
            c = commercial.get(k.id)
            c_pct = (ga.percentile_of(c, [v for v in commercial_values if v is not c])
                     if c is not None else None)  # fmt: skip
            keyword_context[k.id] = {
                "keyword_percentile": pct, "keyword_status": k.status,
                "commercial_rank": (None if c_pct is None
                                    else 3 if c_pct >= 0.75 else 2 if c_pct >= 0.25 else 1),
            }  # fmt: skip
            evidence.append(ga.GrowthEvidence(
                subject_type="keyword", subject_id=f"keyword:{k.id}", keyword_id=k.id,
                article={"keyword": k.keyword, "keyword_status": k.status},
                sources={"keyword": ga.keyword_source(score, age_days=(score or {}).get(
                    "age_days"))},
            ))  # fmt: skip
        data_quality = [{"engine": "revenue", **_candidate(c, "revenue")}
                        for c in revenue.data_quality_candidates]  # fmt: skip
        data_quality += [{"engine": "measurement", "candidate_type": "MEASUREMENT_DATA_QUALITY",
                          "finding": f, "priority": "low"} for f in measurement.data_quality]
        return {
            "as_of": now.isoformat(),
            "window": {"days": days, "start": _iso(measurement.window_start),
                       "end": _iso(measurement.window_end)},
            "freshness": freshness,
            "freshness_detail": freshness_detail,
            "ga4_configured": measurement.ga4_configured,
            "trusted_click_measurement_start_at": _iso(trusted),
            "clicks": {"raw": revenue.raw_clicks,
                       "excluded_instrumentation": revenue.excluded_clicks,
                       "clean": revenue.clean_clicks,
                       "unattributed_raw": measurement.affiliate_unattributed_clicks},
            # 報酬はプログラム単位のまま (記事の収益にしない)。
            "program_commissions": list(revenue.program_commissions),
            "unattributed_commissions": list(measurement.unattributed_commissions),
            "index_observed_at": index_observed,
            "evidence": evidence,
            "keyword_context": keyword_context,
            "data_quality": data_quality,
            "growth_plan": growth_plan,
            "recent_regular_angles": recent_regular_angles,
            "open_regular_proposals": {aid: list(ids) for aid, ids in open_props.items()},
            "engine_notes": {"seo": list(seo.notes), "revenue": list(revenue.notes)},
        }

    def _followup_for(self, now: datetime) -> dict:
        """変換した行動の追跡の観測 (記事ごと)。求められなくても証拠の収集は止めない。"""

        if self._followup is not None:
            return self._followup(now) if callable(self._followup) else dict(self._followup)
        from app.services.growth_measurement_service import (
            GrowthMeasurementService,
            measurement_ready,
        )

        try:
            if not measurement_ready(self._session):
                return {}
            return GrowthMeasurementService(self._session, settings=self._settings,
                                            timezone=self._tz).followup_by_article(now=now)
        except Exception:  # noqa: BLE001 - 追跡の観測は補助 (無くても証拠は同じ)
            return {}

    def _freshness(self, now: datetime) -> tuple[dict[str, str], dict[str, dict]]:
        """C9 の鮮度の言葉 (fresh / stale / unavailable)。判定は ``SourceHealthService`` だけ。"""

        from app.services.source_health_service import SourceHealthService

        raw, states = SourceHealthService(self._session, settings=self._settings,
                                          timezone=self._tz).import_freshness(now=now)
        detail = {name: f.as_dict() for name, f in raw.items()}
        return {k: (ga.UNAVAILABLE if v == "unavailable" else ga.STALE if v == "stale" else v)
                for k, v in states.items()}, detail

    def _index_snapshot(self, now: datetime | None = None) -> tuple[dict[int, dict], str | None]:
        """C10-A: 記事ごとに **URL Inspection をした** 最新の確認 (``IndexStateService``)。

        C9 までは最新の実行 (日ごとの確認は ``GSC_UNKNOWN`` だけ) を読んでいた。
        """

        from app.services.index_state_service import IndexStateService

        observations, meta = IndexStateService(self._session).latest(now=now)
        items = {}
        for aid, o in observations.items():
            items[aid] = {"article_id": aid, "google_index_state": o.raw_status.get(
                "google_index_state"), "normalized_status": o.normalized_status,
                "inspected": o.inspected, "freshness_state": o.freshness_state,
                "observed_at": o.observed_at, "last_crawl": o.last_crawl,
                "data_source": o.data_source, **o.site_checks}  # fmt: skip
        return items, meta.get("latest_inspected_at")

    def _last_updates(self) -> dict[int, str]:
        rows = self._session.execute(
            select(WordPressContentUpdateRun.article_id,
                   func.max(WordPressContentUpdateRun.finished_at))
            .where(WordPressContentUpdateRun.status == "succeeded")
            .group_by(WordPressContentUpdateRun.article_id)
        ).all()  # fmt: skip
        return {aid: _iso(at) for aid, at in rows if aid is not None}

    def _latest_scores(self, keywords: dict[int, Keyword], now: datetime) -> dict[int, dict]:
        from app.repositories.keyword_score_repository import KeywordScoreRepository

        repo = KeywordScoreRepository(self._session)
        out = {}
        for kid in keywords:
            score = repo.get_latest(kid)
            if score is None:
                continue
            created = ensure_aware(score.created_at) if score.created_at else None
            out[kid] = {
                "score_id": score.id, "total_score": _num(score.total_score),
                "score_version": score.score_version, "input_source": score.input_source,
                "created_at": _iso(created),
                "age_days": (now - created).days if created else None,
                "components": {c: _num(getattr(score, c, None)) for c in (
                    "search_demand", "commercial_intent", "affiliate_opportunity",
                    "competition_ease", "trend", "originality", "site_relevance")},
            }  # fmt: skip
        return out

    def _open_proposals(self) -> dict[int, list[int]]:
        published = set(self._session.scalars(select(ThreadsPublication.proposal_id)).all())
        rows = self._session.execute(
            select(ThreadsPostProposal.id, ThreadsPostProposal.source_article_id)
            .where(ThreadsPostProposal.source_article_id.is_not(None),
                   ThreadsPostProposal.status.in_((*TP_OPEN_STATES, TP_APPROVED)))
        ).all()  # fmt: skip
        out: dict[int, list[int]] = defaultdict(list)
        for pid, aid in rows:
            if pid not in published:
                out[aid].append(pid)
        return {k: sorted(v) for k, v in out.items()}

    def _article_row(self, article: Article, now: datetime, updates: dict[int, str],
                     revenue, keywords: dict[int, Keyword]) -> dict:  # fmt: skip
        published = ensure_aware(article.published_at) if article.published_at else None
        keyword = keywords.get(article.keyword_id) if article.keyword_id else None
        return {
            "article_id": article.id, "title": article.title, "status": article.status,
            "keyword_id": article.keyword_id, "keyword": keyword.keyword if keyword else None,
            "published_at": _iso(published),
            "age_days": (now - published).days if published else None,
            "last_content_update_at": updates.get(article.id),
            "article_type": getattr(revenue, "article_type", None) or article.article_type,
            "monetization_mode": getattr(revenue, "monetization_mode", None)
            or article.monetization_mode,
            "monetized": getattr(revenue, "monetized", None),
            "affiliate_link_available": (getattr(revenue, "active_target_count", 0) or 0) > 0
            if revenue is not None else None,
        }  # fmt: skip

    def _threads_posts(self, now: datetime) -> dict[int, list[dict]]:
        from app.services.threads_performance_analysis_service import (
            ThreadsPerformanceAnalysisService,
        )

        report = ThreadsPerformanceAnalysisService(
            self._session, settings=self._settings, timezone=self._tz
        ).report(as_of=now)
        out: dict[int, list[dict]] = defaultdict(list)
        for post in report.get("posts") or []:
            if post.get("article_id") is not None and post.get("lane") == "regular":
                out[int(post["article_id"])].append(post)
        return dict(out)

    def _growth_plan(self, now: datetime) -> dict | None:
        from app.services.threads_growth_service import ThreadsGrowthService

        try:
            plan = ThreadsGrowthService(self._session, timezone=self._tz,
                                        client=None).plan(now=now)  # fmt: skip
        except Exception:  # noqa: BLE001 - Growth の枠が読めなくても分析は止めない
            return None
        reason = plan.get("reason") or ""
        # client なしで読むので、「client が無い」以外で止まっていなければ、その日の枠は空いている。
        due = plan.get("due") or reason.startswith("the OpenAI client is not configured")
        return {"date_jst": plan.get("date_jst"), "due": bool(due),
                "reason": None if due else reason,
                "active_proposal": plan.get("active_proposal")}  # fmt: skip


def _num(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _commercial(score: dict | None) -> float | None:
    if not score:
        return None
    values = [score["components"].get(k) for k in ("commercial_intent", "affiliate_opportunity")]
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 3) if values else None


def _revenue_dict(r) -> dict | None:
    if r is None:
        return None
    return {k: getattr(r, k, None) for k in (
        "maturity_state", "maturity_reason", "monetized", "raw_clicks", "excluded_clicks",
        "clean_clicks", "clicks_per_100_organic_sessions", "linked_programs", "attribution")}


def _candidate(c, engine: str) -> dict:
    data = c if isinstance(c, dict) else getattr(c, "__dict__", {})
    return {"engine": engine, "candidate_type": data.get("candidate_type"),
            "reason_code": data.get("reason_code"), "priority": data.get("priority"),
            "evidence_strength": data.get("evidence_strength") or data.get("evidence_basis"),
            "evidence": data.get("evidence") or {},
            "affiliate_program_id": data.get("affiliate_program_id")}  # fmt: skip


__all__ = ["DEFAULT_WINDOW_DAYS", "GrowthEvidenceService"]
