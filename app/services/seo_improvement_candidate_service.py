"""SeoImprovementCandidateService -- SEO 改善候補の評価 (C6)。

「この記事に手を入れるべき証拠が揃っているか、揃っているなら何を、どの根拠で」
に答える。**2 つ目の分析基盤は作らない** -- C5.1/C5.2 の計測レイヤをそのまま
消費する:

- 記事ごとの GSC / GA4 / アフィリエイト集計 -- :class:`ArticleMeasurementReportService`
- live / sitemap / URL Inspection の状態 -- :class:`ArticleIndexabilityReportService`
- URL 正規化 -- :mod:`app.seo.url_normalization`
- 閾値 -- ``app/config/seo_policy.json`` (:mod:`app.seo.policy`)
- 成熟度 -- :mod:`app.seo.maturity`
- 判定ルール -- :mod:`app.seo.candidates` (pure)

**評価は read-only** -- 記事本文にも analytics のソース表にも一切書き込まない。
``persist()`` を明示的に呼んだときだけ、評価結果を append-only な
:class:`SeoImprovementRun` / :class:`SeoImprovementCandidate` として残す。

中心にある約束は 1 つ:

    指標がゼロであることと、成績が悪いことを混同しない。

成熟していない記事には性能系の候補を **出さない**。理由 (``no_action_reason``) は
必ず添える。
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Article, SearchConsoleQueryDaily, Source
from app.models.seo_improvement_candidate import SeoImprovementCandidate
from app.models.seo_improvement_run import SeoImprovementRun
from app.seo.candidates import (
    CANDIDATE_TYPES,
    SeoCandidate,
    evaluate_cannibalization,
    evaluate_ctr,
    evaluate_engagement,
    evaluate_freshness,
    evaluate_indexing_followup,
    evaluate_internal_link,
    evaluate_query_expansion,
    evaluate_ranking,
)
from app.seo.maturity import ArticleMaturity, assess_maturity
from app.seo.policy import PRIORITIES, SeoPolicy, get_policy
from app.seo.url_normalization import normalize_url_key
from app.services.article_measurement_report_service import ArticleMeasurementReportService

_PORTFOLIO_PATH = Path(__file__).resolve().parents[1] / "config" / "content_portfolio.json"


@dataclass
class ArticleEvaluation:
    article_id: int
    keyword: str | None
    url: str
    published_at: str | None
    article_type: str | None
    monetization_mode: str | None

    maturity_search: str = ""
    maturity_engagement: str = ""
    maturity_search_reason: str = ""
    maturity_engagement_reason: str = ""
    age_days: int | None = None

    # 各出所の要約 (欠測は 0 ではなく None のままにする)。
    gsc: dict = field(default_factory=dict)
    ga4: dict = field(default_factory=dict)
    indexability: dict = field(default_factory=dict)
    freshness: dict = field(default_factory=dict)

    candidates: list[dict] = field(default_factory=list)
    no_action_reason: str | None = None


@dataclass
class SeoCandidateReport:
    generated_at: datetime
    policy_version: str
    window_start: date
    window_end: date
    gsc_data_through: date | None
    ga4_data_through: date | None
    ga4_configured: bool
    index_observed_at: datetime | None
    article_count: int
    articles: list[ArticleEvaluation] = field(default_factory=list)
    candidate_counts: dict[str, int] = field(default_factory=dict)
    priority_counts: dict[str, int] = field(default_factory=dict)
    maturity_counts: dict[str, int] = field(default_factory=dict)
    actionable_article_ids: list[int] = field(default_factory=list)
    insufficient_data_article_ids: list[int] = field(default_factory=list)
    internal_link_debt: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def total_candidates(self) -> int:
        return sum(len(a.candidates) for a in self.articles)

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["generated_at"] = self.generated_at.isoformat()
        payload["window_start"] = self.window_start.isoformat()
        payload["window_end"] = self.window_end.isoformat()
        payload["gsc_data_through"] = (
            self.gsc_data_through.isoformat() if self.gsc_data_through else None
        )
        payload["ga4_data_through"] = (
            self.ga4_data_through.isoformat() if self.ga4_data_through else None
        )
        payload["index_observed_at"] = (
            self.index_observed_at.isoformat() if self.index_observed_at else None
        )
        return payload


class SeoImprovementCandidateService:
    def __init__(
        self,
        session: Session,
        *,
        settings,
        policy: SeoPolicy | None = None,
        portfolio_path: Path | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._policy = policy or get_policy()
        self._portfolio_path = portfolio_path or _PORTFOLIO_PATH

    # -- public ---------------------------------------------------------------
    def evaluate(
        self, *, days: int = 30, indexability=None, now: datetime | None = None
    ) -> SeoCandidateReport:
        now = now or datetime.now(UTC)
        today = now.date()
        policy = self._policy

        measurement = ArticleMeasurementReportService(self._session, settings=self._settings).build(
            days=days, indexability=indexability, now=now
        )

        index_rows = {r.article_id: r for r in (indexability.articles if indexability else [])}
        index_observed_at = indexability.generated_at if indexability else None
        index_observed_days_ago = (
            (today - index_observed_at.date()).days if index_observed_at else None
        )

        articles = {
            a.id: a
            for a in self._session.scalars(
                select(Article).where(Article.status == "published")
            ).all()
        }
        source_ages = self._latest_source_dates()
        source_counts = self._source_counts()
        link_inventory = self._internal_link_inventory(articles)
        query_evidence = self._query_evidence(measurement.window_start, measurement.window_end)

        report = SeoCandidateReport(
            generated_at=now,
            policy_version=policy.policy_version,
            window_start=measurement.window_start,
            window_end=measurement.window_end,
            gsc_data_through=measurement.gsc_data_through,
            ga4_data_through=measurement.ga4_data_through,
            ga4_configured=measurement.ga4_configured,
            index_observed_at=index_observed_at,
            article_count=measurement.article_count,
        )

        maturities: dict[int, ArticleMaturity] = {}
        per_article: dict[int, list[SeoCandidate]] = defaultdict(list)

        for row in measurement.articles:
            article = articles.get(row.article_id)
            if article is None:
                continue
            index_row = index_rows.get(row.article_id)
            maturity = assess_maturity(
                article_id=row.article_id,
                published_at=article.published_at,
                today=today,
                google_index_state=row.google_index_state,
                gsc_data_through=measurement.gsc_data_through,
                gsc_coverage_through=measurement.gsc_coverage_through,
                ga4_data_through=measurement.ga4_data_through,
                impressions=row.impressions,
                organic_sessions=row.organic_sessions,
                ga4_configured=measurement.ga4_configured,
                policy=policy,
            )
            maturities[row.article_id] = maturity

            found = per_article[row.article_id]
            candidate = evaluate_indexing_followup(
                article_id=row.article_id,
                maturity=maturity,
                live_state=index_row.live_state if index_row else None,
                sitemap_state=index_row.sitemap_state if index_row else None,
                google_index_state=row.google_index_state,
                index_observed_days_ago=index_observed_days_ago,
                policy=policy,
            )
            if candidate:
                found.append(candidate)

            candidate = evaluate_ctr(
                article_id=row.article_id,
                maturity=maturity,
                impressions=row.impressions,
                clicks=row.clicks,
                ctr=row.ctr,
                average_position=row.average_position,
                policy=policy,
            )
            if candidate:
                found.append(candidate)

            candidate = evaluate_ranking(
                article_id=row.article_id,
                maturity=maturity,
                impressions=row.impressions,
                average_position=row.average_position,
                policy=policy,
            )
            if candidate:
                found.append(candidate)

            found.extend(
                evaluate_query_expansion(
                    article_id=row.article_id,
                    maturity=maturity,
                    queries=row.top_queries,
                    body_text=article.body or "",
                    policy=policy,
                )
            )

            candidate = evaluate_engagement(
                article_id=row.article_id,
                maturity=maturity,
                organic_sessions=row.organic_sessions,
                engagement_rate=row.engagement_rate,
                average_engagement_seconds=row.average_engagement_time_seconds,
                policy=policy,
            )
            if candidate:
                found.append(candidate)

            candidate = evaluate_freshness(
                article_id=row.article_id,
                article_type=article.article_type,
                latest_source_checked_on=source_ages.get(row.article_id),
                today=today,
                source_count=source_counts.get(row.article_id, 0),
                policy=policy,
            )
            if candidate:
                found.append(candidate)

        # -- クエリ横断の判定 (カニバリ) ---------------------------------------
        for query, pages in query_evidence.items():
            for candidate in evaluate_cannibalization(query=query, pages=pages, policy=policy):
                per_article[candidate.article_id].append(candidate)

        # -- 構造的な内部リンク候補 --------------------------------------------
        debt = self._internal_link_candidates(articles, link_inventory, policy)
        report.internal_link_debt = [d["record"] for d in debt]
        for item in debt:
            if item["candidate"] is not None:
                per_article[item["candidate"].article_id].append(item["candidate"])

        # -- 記事ごとの結果を組み立てる ----------------------------------------
        for row in measurement.articles:
            article = articles.get(row.article_id)
            if article is None:
                continue
            maturity = maturities[row.article_id]
            index_row = index_rows.get(row.article_id)
            found = _dedupe(per_article.get(row.article_id, []))
            evaluation = ArticleEvaluation(
                article_id=row.article_id,
                keyword=row.keyword,
                url=row.url,
                published_at=row.published_at,
                article_type=article.article_type,
                monetization_mode=article.monetization_mode,
                maturity_search=maturity.search_state,
                maturity_engagement=maturity.engagement_state,
                maturity_search_reason=maturity.search_reason,
                maturity_engagement_reason=maturity.engagement_reason,
                age_days=maturity.age_days,
                gsc={
                    "impressions": row.impressions,
                    "clicks": row.clicks,
                    "ctr": row.ctr,
                    "average_position": row.average_position,
                    "query_count": row.query_count,
                    "top_queries": row.top_queries,
                    "data_through": (
                        measurement.gsc_data_through.isoformat()
                        if measurement.gsc_data_through
                        else None
                    ),
                    "rows_present": row.gsc_rows > 0,
                },
                ga4={
                    "configured": measurement.ga4_configured,
                    "rows_present": row.ga4_rows > 0,
                    "sessions": row.sessions if row.ga4_rows else None,
                    "organic_sessions": row.organic_sessions if row.ga4_rows else None,
                    "engagement_rate": row.engagement_rate,
                    "average_engagement_time_seconds": row.average_engagement_time_seconds,
                    "data_through": (
                        measurement.ga4_data_through.isoformat()
                        if measurement.ga4_data_through
                        else None
                    ),
                },
                indexability={
                    "live_state": index_row.live_state if index_row else None,
                    "sitemap_state": index_row.sitemap_state if index_row else None,
                    "google_index_state": row.google_index_state,
                    # スナップショットであることを明示する (恒久的な真実ではない)。
                    "observed_at": (index_observed_at.isoformat() if index_observed_at else None),
                    "observed_days_ago": index_observed_days_ago,
                },
                freshness={
                    "latest_source_checked_on": (
                        source_ages[row.article_id].isoformat()
                        if source_ages.get(row.article_id)
                        else None
                    ),
                    "source_count": source_counts.get(row.article_id, 0),
                    "policy_days": policy.freshness_days_for(article.article_type),
                },
                candidates=[_candidate_dict(c) for c in found],
            )
            if not found:
                evaluation.no_action_reason = _no_action_reason(maturity)
            report.articles.append(evaluation)

        _summarize(report)
        report.notes.append(
            "thresholds are operational heuristics from app/config/seo_policy.json "
            f"(version {policy.policy_version}); they are not claims about Google's "
            "ranking algorithm"
        )
        if not report.gsc_data_through:
            report.notes.append("no search console data has been imported yet")
        if not measurement.ga4_configured:
            report.notes.append("ga4 is not configured; engagement review is suppressed")
        elif report.ga4_data_through is None:
            report.notes.append("ga4 has no daily rows yet; engagement review is suppressed")
        return report

    def persist(
        self, report: SeoCandidateReport, *, idempotency_key: str | None = None
    ) -> SeoImprovementRun:
        """評価結果を append-only な run + 候補として残す (transaction owner)。"""

        if idempotency_key is not None:
            existing = self._session.scalars(
                select(SeoImprovementRun).where(
                    SeoImprovementRun.idempotency_key == idempotency_key
                )
            ).first()
            if existing is not None:
                return existing

        run = SeoImprovementRun(
            policy_version=report.policy_version,
            window_start=report.window_start,
            window_end=report.window_end,
            gsc_data_through=report.gsc_data_through,
            ga4_data_through=report.ga4_data_through,
            index_observed_at=report.index_observed_at,
            evaluated_article_count=report.article_count,
            candidate_count=report.total_candidates,
            summary_json={
                "candidate_counts": report.candidate_counts,
                "priority_counts": report.priority_counts,
                "maturity_counts": report.maturity_counts,
                "actionable_article_ids": report.actionable_article_ids,
                "insufficient_data_article_ids": report.insufficient_data_article_ids,
            },
            notes="; ".join(report.notes) or None,
            idempotency_key=idempotency_key,
        )
        self._session.add(run)
        self._session.flush()

        for evaluation in report.articles:
            for candidate in evaluation.candidates:
                self._session.add(
                    SeoImprovementCandidate(
                        seo_improvement_run_id=run.id,
                        article_id=evaluation.article_id,
                        candidate_type=candidate["candidate_type"],
                        reason_code=candidate["reason_code"],
                        priority=candidate["priority"],
                        evidence_strength=candidate["evidence_strength"],
                        suggested_action=candidate["suggested_action"],
                        evidence_json=candidate["evidence"],
                        dedupe_key=candidate["dedupe_key"],
                    )
                )
        self._session.commit()
        self._session.refresh(run)
        return run

    # -- sources --------------------------------------------------------------
    def _latest_source_dates(self) -> dict[int, date]:
        rows = self._session.execute(
            select(Source.article_id, func.max(Source.checked_at)).group_by(Source.article_id)
        ).all()
        out: dict[int, date] = {}
        for article_id, checked_at in rows:
            if checked_at is None:
                continue
            out[article_id] = checked_at.date() if isinstance(checked_at, datetime) else checked_at
        return out

    def _source_counts(self) -> dict[int, int]:
        rows = self._session.execute(
            select(Source.article_id, func.count()).group_by(Source.article_id)
        ).all()
        return {article_id: int(count) for article_id, count in rows}

    def _query_evidence(self, start: date, end: date) -> dict[str, list[dict]]:
        """クエリごとに「どの記事が表示回数を得たか」を集める (実データのみ)。"""

        articles = {
            normalize_url_key(a.published_url or ""): a
            for a in self._session.scalars(
                select(Article).where(Article.status == "published")
            ).all()
        }
        rows = self._session.scalars(
            select(SearchConsoleQueryDaily).where(
                SearchConsoleQueryDaily.metric_date >= start,
                SearchConsoleQueryDaily.metric_date <= end,
            )
        ).all()
        grouped: dict[str, dict[int | None, dict]] = defaultdict(dict)
        for row in rows:
            article = articles.get(normalize_url_key(row.page))
            if article is None:
                continue  # 記事ではないページ (カテゴリ等) はカニバリ判定に含めない
            bucket = grouped[row.query].setdefault(
                article.id,
                {
                    "article_id": article.id,
                    "url": article.published_url,
                    "impressions": 0,
                    "clicks": 0,
                    "_positions": [],
                },
            )
            bucket["impressions"] += row.impressions
            bucket["clicks"] += row.clicks
            bucket["_positions"].append((row.position, row.impressions))
        out: dict[str, list[dict]] = {}
        for query, pages in grouped.items():
            entries = []
            for page in pages.values():
                weight = sum(i for _p, i in page["_positions"])
                page["average_position"] = (
                    round(sum(p * i for p, i in page["_positions"]) / weight, 2) if weight else None
                )
                page.pop("_positions")
                entries.append(page)
            out[query] = entries
        return out

    # -- internal links -------------------------------------------------------
    def _internal_link_inventory(self, articles: dict[int, Article]) -> dict[int, set[int]]:
        """各記事の本文に既に存在する内部リンク先 (記事 id) を集める。"""

        by_path: dict[str, int] = {}
        for article in articles.values():
            path = unquote(urlparse(article.published_url or "").path).strip("/")
            if path:
                by_path[path] = article.id
        inventory: dict[int, set[int]] = {}
        for article in articles.values():
            targets: set[int] = set()
            for url in re.findall(r"\]\((https?://[^)\s]+)\)", article.body or ""):
                parsed = urlparse(url)
                if "bizfluxlab.com" not in parsed.netloc:
                    continue
                target = by_path.get(unquote(parsed.path).strip("/"))
                if target is not None and target != article.id:
                    targets.add(target)
            inventory[article.id] = targets
        return inventory

    def _portfolio_relations(self, articles: dict[int, Article]) -> list[tuple[int, int, str]]:
        """構造的に妥当な内部リンク関係だけを列挙する (実測ではない)。

        2 種類しか採らない -- 無関係なクラスタ間リンクや、機械的な総当たりは作らない:

        1. **ポリシーに明記された deferred pair** -- C4.9 で先送りした具体的な関係。
           バージョン管理された ``seo_policy.json`` にあるので、後から議論できる。
        2. **spoke -> クラスタのアンカー** -- 各記事から、そのクラスタの pillar
           (無ければ hub) への 1 本。話題の集約という明確な根拠がある方向だけ。

        逆方向 (アンカー -> spoke) は記事あたりの上限に従って後段で絞る。
        """

        relations: list[tuple[int, int, str]] = []
        seen: set[tuple[int, int]] = set()

        for pair in self._policy.section("internal_links").get("deferred_pairs") or []:
            source = pair.get("source")
            target = pair.get("target")
            if not isinstance(source, int) or not isinstance(target, int):
                continue
            if source not in articles or target not in articles or source == target:
                continue
            key = (source, target)
            if key in seen:
                continue
            seen.add(key)
            relations.append((source, target, f"deferred pair: {pair.get('reason', '')}".strip()))

        try:
            document = json.loads(self._portfolio_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return relations

        from app.models import Keyword

        keyword_to_article: dict[str, int] = {}
        for article in articles.values():
            if article.keyword_id is None:
                continue
            keyword = self._session.get(Keyword, article.keyword_id)
            if keyword is not None:
                keyword_to_article[keyword.keyword] = article.id

        clusters: dict[str, list[tuple[int, dict]]] = defaultdict(list)
        for entry in document.get("articles") or []:
            article_id = keyword_to_article.get(entry.get("keyword") or "")
            cluster = entry.get("cluster")
            if article_id in articles and isinstance(cluster, str):
                clusters[cluster].append((article_id, entry))

        for cluster, members in sorted(clusters.items()):
            anchor = _cluster_anchor(members, articles)
            if anchor is None:
                continue
            for article_id, _entry in members:
                if article_id == anchor:
                    continue
                for pair, label in (
                    ((article_id, anchor), f"cluster {cluster}: spoke -> anchor"),
                    ((anchor, article_id), f"cluster {cluster}: anchor -> spoke"),
                ):
                    if pair in seen:
                        continue
                    seen.add(pair)
                    relations.append((pair[0], pair[1], label))
        return relations

    def _internal_link_candidates(
        self, articles: dict[int, Article], inventory: dict[int, set[int]], policy: SeoPolicy
    ) -> list[dict]:
        """構造的な内部リンク候補。既にあるリンクは提案しない。"""

        limit = int(policy.gate("internal_links", "max_candidates_per_article", 3))
        emitted: dict[int, int] = defaultdict(int)
        seen: set[tuple[int, int]] = set()
        out: list[dict] = []
        for source_id, target_id, relation in self._portfolio_relations(articles):
            if source_id not in articles or target_id not in articles:
                continue
            pair = (source_id, target_id)
            if pair in seen:
                continue
            seen.add(pair)
            already = target_id in inventory.get(source_id, set())
            record = {
                "source_article_id": source_id,
                "target_article_id": target_id,
                "relation": relation,
                "status": "already_satisfied" if already else "candidate",
            }
            if already or emitted[source_id] >= limit:
                if already:
                    out.append({"record": record, "candidate": None})
                continue
            emitted[source_id] += 1
            target = articles[target_id]
            out.append(
                {
                    "record": record,
                    "candidate": evaluate_internal_link(
                        source_article_id=source_id,
                        target_article_id=target_id,
                        relation=relation,
                        target_url=target.published_url or "",
                        target_title=target.title,
                        policy=policy,
                    ),
                }
            )
        return out


def _cluster_anchor(members: list[tuple[int, dict]], articles: dict[int, Article]) -> int | None:
    """クラスタの集約先 (pillar > hub 表記 > category_landing > roundup) を 1 つ選ぶ。"""

    for article_id, entry in members:
        if entry.get("role") == "pillar":
            return article_id
    for article_id, entry in members:
        if "hub" in (entry.get("internal_link_role") or "").lower():
            return article_id
    for wanted in ("category_landing", "recommendation_roundup"):
        for article_id, _entry in members:
            if (articles[article_id].article_type or "") == wanted:
                return article_id
    return None


# -- helpers -------------------------------------------------------------------
def _candidate_dict(candidate: SeoCandidate) -> dict:
    key = candidate.dedupe_key or _dedupe_key(candidate)
    return {
        "candidate_type": candidate.candidate_type,
        "reason_code": candidate.reason_code,
        "priority": candidate.priority,
        "evidence_strength": candidate.evidence_strength,
        "suggested_action": candidate.suggested_action,
        "evidence": candidate.evidence,
        "dedupe_key": key,
    }


def _dedupe_key(candidate: SeoCandidate) -> str:
    evidence = candidate.evidence or {}
    discriminator = (
        evidence.get("query") or evidence.get("target_article_id") or evidence.get("reason") or ""
    )
    return f"{candidate.article_id}:{candidate.candidate_type}:{discriminator}"


def _dedupe(candidates: list[SeoCandidate]) -> list[SeoCandidate]:
    """同じ dedupe_key の候補は 1 件にし、決定的な順序で返す。"""

    unique: dict[str, SeoCandidate] = {}
    for candidate in candidates:
        key = candidate.dedupe_key or _dedupe_key(candidate)
        unique.setdefault(key, candidate.with_dedupe_key(key))
    order = {t: i for i, t in enumerate(CANDIDATE_TYPES)}
    return sorted(
        unique.values(),
        key=lambda c: (order.get(c.candidate_type, 99), c.dedupe_key),
    )


def _no_action_reason(maturity: ArticleMaturity) -> str:
    return (
        f"search: {maturity.search_state} ({maturity.search_reason}); "
        f"engagement: {maturity.engagement_state} ({maturity.engagement_reason})"
    )


def _summarize(report: SeoCandidateReport) -> None:
    candidate_counts: dict[str, int] = {t: 0 for t in CANDIDATE_TYPES}
    priority_counts: dict[str, int] = {p: 0 for p in PRIORITIES}
    maturity_counts: dict[str, int] = defaultdict(int)
    for evaluation in report.articles:
        maturity_counts[evaluation.maturity_search] += 1
        for candidate in evaluation.candidates:
            candidate_counts[candidate["candidate_type"]] = (
                candidate_counts.get(candidate["candidate_type"], 0) + 1
            )
            priority_counts[candidate["priority"]] = (
                priority_counts.get(candidate["priority"], 0) + 1
            )
        if evaluation.candidates:
            report.actionable_article_ids.append(evaluation.article_id)
        else:
            report.insufficient_data_article_ids.append(evaluation.article_id)
    report.candidate_counts = {k: v for k, v in candidate_counts.items() if v}
    report.priority_counts = priority_counts
    report.maturity_counts = dict(maturity_counts)
