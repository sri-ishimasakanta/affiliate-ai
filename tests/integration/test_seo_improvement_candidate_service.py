"""SeoImprovementCandidateService の統合テスト (C6)。

pin する契約:

- 公開直後でデータが無い記事に、性能系の候補を **一切** 出さない。
- 欠測は 0 ではなく「データ待ち」として扱われ、理由が必ず添えられる。
- 既に存在する内部リンクは二重に推奨しない。
- 構造的な候補は ``structural`` と明示され、実測由来と混同されない。
- 同じ入力なら結果は決定的。
- 評価は記事を一切変更しない。``--execute`` 相当の永続化だけが履歴を残す。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Article,
    Keyword,
    SearchConsoleImportRun,
    SearchConsolePageDaily,
    SearchConsoleQueryDaily,
    SeoImprovementCandidate,
    SeoImprovementRun,
    Source,
)
from app.seo.candidates import (
    CANNIBALIZATION_REVIEW,
    CONTENT_FRESHNESS_REVIEW,
    CTR_IMPROVEMENT,
    EVIDENCE_STRUCTURAL,
    INTERNAL_LINK_OPPORTUNITY,
    RANKING_IMPROVEMENT,
)
from app.seo.maturity import SEARCH_NEWLY_PUBLISHED, SEARCH_SUFFICIENT_SAMPLE
from app.services.seo_improvement_candidate_service import SeoImprovementCandidateService

_BASE = "https://bizfluxlab.com"
_PROPERTY = "sc-domain:bizfluxlab.com"
_NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
_TODAY = _NOW.date()


class _Settings:
    wordpress_base_url = _BASE
    search_console_property_uri = _PROPERTY
    search_console_credentials_file = None
    ga4_property_id = None


def _article(
    session: Session,
    article_id: int,
    slug: str,
    *,
    days_old: int,
    article_type: str = "informational",
    body: str = "本文",
    keyword: str | None = None,
) -> Article:
    keyword_id = None
    if keyword:
        row = Keyword(keyword=keyword)
        session.add(row)
        session.commit()
        keyword_id = row.id
    article = Article(
        id=article_id,
        title=slug,
        slug=slug,
        keyword_id=keyword_id,
        body=body,
        status="published",
        published_url=f"{_BASE}/{slug}/",
        published_at=_NOW - timedelta(days=days_old),
        article_type=article_type,
        monetization_mode="supporting",
    )
    session.add(article)
    session.commit()
    return article


def _sc_run(session: Session) -> SearchConsoleImportRun:
    run = SearchConsoleImportRun(
        property_uri=_PROPERTY,
        start_date=_TODAY,
        end_date=_TODAY,
        status="succeeded",
        dimensions_json='[["date","page"]]',
        import_identity_hash="a" * 64,
    )
    session.add(run)
    session.commit()
    return run


def _page(session, run, slug, *, clicks, impressions, position, day=None) -> None:
    session.add(
        SearchConsolePageDaily(
            property_uri=_PROPERTY,
            metric_date=day or _TODAY - timedelta(days=1),
            page=f"{_BASE}/{slug}/",
            clicks=clicks,
            impressions=impressions,
            ctr=(clicks / impressions) if impressions else 0.0,
            position=position,
            source_import_run_id=run.id,
        )
    )
    session.commit()


def _query(session, run, slug, query, *, clicks, impressions, position) -> None:
    session.add(
        SearchConsoleQueryDaily(
            property_uri=_PROPERTY,
            metric_date=_TODAY - timedelta(days=1),
            page=f"{_BASE}/{slug}/",
            query=query,
            clicks=clicks,
            impressions=impressions,
            ctr=(clicks / impressions) if impressions else 0.0,
            position=position,
            source_import_run_id=run.id,
        )
    )
    session.commit()


def _source(session, article_id: int, checked_on: date) -> None:
    session.add(
        Source(
            article_id=article_id,
            source_type="official",
            source_url="https://official.test/pricing",
            title="pricing",
            checked_at=datetime.combine(checked_on, datetime.min.time(), tzinfo=UTC),
        )
    )
    session.commit()


def _evaluate(session: Session, *, indexability=None, days: int = 30):
    return SeoImprovementCandidateService(session, settings=_Settings()).evaluate(
        days=days, indexability=indexability, now=_NOW
    )


def _types(evaluation) -> set[str]:
    return {c["candidate_type"] for c in evaluation.candidates}


def _find(report, article_id: int):
    return next(e for e in report.articles if e.article_id == article_id)


# ==================== day-zero suppression ====================================
def test_newly_published_article_with_zero_data_gets_no_performance_candidate(
    session: Session,
) -> None:
    _article(session, 1, "fresh", days_old=0)
    _sc_run(session)

    evaluation = _find(_evaluate(session), 1)

    assert evaluation.maturity_search == SEARCH_NEWLY_PUBLISHED
    assert CTR_IMPROVEMENT not in _types(evaluation)
    assert RANKING_IMPROVEMENT not in _types(evaluation)
    assert evaluation.no_action_reason is not None


def test_zero_metrics_are_reported_as_awaiting_not_as_poor_performance(
    session: Session,
) -> None:
    _article(session, 1, "fresh", days_old=0)
    evaluation = _find(_evaluate(session), 1)
    assert evaluation.gsc["rows_present"] is False
    assert evaluation.gsc["ctr"] is None
    assert evaluation.gsc["average_position"] is None
    assert "awaiting" in evaluation.no_action_reason or "newly" in evaluation.no_action_reason


def test_missing_ga4_reports_awaiting_data_not_weak_engagement(session: Session) -> None:
    _article(session, 1, "old", days_old=120)
    evaluation = _find(_evaluate(session), 1)
    assert evaluation.ga4["configured"] is False
    assert evaluation.ga4["sessions"] is None
    assert "awaiting_ga4_data" in evaluation.maturity_engagement


# ==================== performance candidates ==================================
def test_mature_article_with_weak_ctr_gets_a_ctr_candidate(session: Session) -> None:
    _article(session, 1, "mature", days_old=120)
    run = _sc_run(session)
    _page(session, run, "mature", clicks=2, impressions=1000, position=6.0)

    evaluation = _find(_evaluate(session), 1)

    assert evaluation.maturity_search == SEARCH_SUFFICIENT_SAMPLE
    assert CTR_IMPROVEMENT in _types(evaluation)


def test_impressions_below_the_gate_produce_no_ctr_candidate(session: Session) -> None:
    _article(session, 1, "mature", days_old=120)
    run = _sc_run(session)
    _page(session, run, "mature", clicks=0, impressions=10, position=6.0)

    evaluation = _find(_evaluate(session), 1)
    assert CTR_IMPROVEMENT not in _types(evaluation)


def test_ranking_opportunity_is_detected(session: Session) -> None:
    _article(session, 1, "mature", days_old=120)
    run = _sc_run(session)
    _page(session, run, "mature", clicks=30, impressions=500, position=14.0)

    assert RANKING_IMPROVEMENT in _types(_find(_evaluate(session), 1))


def test_query_expansion_uses_real_query_rows(session: Session) -> None:
    _article(session, 1, "mature", days_old=120, body="導入手順だけを扱う")
    run = _sc_run(session)
    _page(session, run, "mature", clicks=10, impressions=500, position=12.0)
    _query(session, run, "mature", "rpa 比較", clicks=1, impressions=200, position=15.0)

    evaluation = _find(_evaluate(session), 1)
    expansions = [c for c in evaluation.candidates if c["candidate_type"] == "QUERY_EXPANSION"]
    assert len(expansions) == 1
    assert expansions[0]["evidence"]["query"] == "rpa 比較"


def test_same_query_on_two_pages_is_a_cannibalization_review(session: Session) -> None:
    _article(session, 1, "page-a", days_old=120)
    _article(session, 2, "page-b", days_old=120)
    run = _sc_run(session)
    _query(session, run, "page-a", "共通クエリ", clicks=1, impressions=300, position=8.0)
    _query(session, run, "page-b", "共通クエリ", clicks=0, impressions=200, position=15.0)

    report = _evaluate(session)

    assert CANNIBALIZATION_REVIEW in _types(_find(report, 1))
    assert CANNIBALIZATION_REVIEW in _types(_find(report, 2))
    evidence = next(
        c["evidence"]
        for c in _find(report, 1).candidates
        if c["candidate_type"] == CANNIBALIZATION_REVIEW
    )
    assert {p["article_id"] for p in evidence["pages"]} == {1, 2}


def test_category_pages_do_not_create_cannibalization(session: Session) -> None:
    """記事ではないページは判定に混ぜない。"""

    _article(session, 1, "page-a", days_old=120)
    run = _sc_run(session)
    _query(session, run, "page-a", "共通クエリ", clicks=1, impressions=300, position=8.0)
    session.add(
        SearchConsoleQueryDaily(
            property_uri=_PROPERTY,
            metric_date=_TODAY - timedelta(days=1),
            page=f"{_BASE}/category/x/",
            query="共通クエリ",
            clicks=0,
            impressions=300,
            ctr=0.0,
            position=20.0,
            source_import_run_id=run.id,
        )
    )
    session.commit()

    assert CANNIBALIZATION_REVIEW not in _types(_find(_evaluate(session), 1))


# ==================== freshness ===============================================
def test_aged_pricing_sources_produce_a_freshness_candidate(session: Session) -> None:
    _article(session, 1, "pricing", days_old=200, article_type="pricing")
    _source(session, 1, date(2026, 1, 1))

    evaluation = _find(_evaluate(session), 1)
    assert CONTENT_FRESHNESS_REVIEW in _types(evaluation)
    assert evaluation.freshness["policy_days"] == 60


def test_recent_sources_produce_no_freshness_candidate(session: Session) -> None:
    _article(session, 1, "pricing", days_old=200, article_type="pricing")
    _source(session, 1, _TODAY - timedelta(days=5))

    assert CONTENT_FRESHNESS_REVIEW not in _types(_find(_evaluate(session), 1))


def test_freshness_works_without_any_traffic_data(session: Session) -> None:
    _article(session, 1, "pricing", days_old=200, article_type="pricing")
    _source(session, 1, date(2026, 1, 1))

    evaluation = _find(_evaluate(session), 1)
    assert evaluation.gsc["rows_present"] is False
    assert CONTENT_FRESHNESS_REVIEW in _types(evaluation)


# ==================== indexing ================================================
class _IndexRow:
    def __init__(self, article_id, live_state, sitemap_state):
        self.article_id = article_id
        self.live_state = live_state
        self.sitemap_state = sitemap_state
        self.google_index_state = "GSC_NOT_KNOWN_TO_GOOGLE"


class _Indexability:
    def __init__(self, rows, generated_at=_NOW):
        self.articles = rows
        self.generated_at = generated_at


def test_old_unindexed_article_gets_indexing_followup(session: Session) -> None:
    _article(session, 1, "old", days_old=60)
    indexability = _Indexability([_IndexRow(1, "LIVE_HEALTHY", "SITEMAP_PRESENT")])

    # 測定側も同じ index 状態を見るよう、indexability を渡して評価する。
    report = _evaluate(session, indexability=indexability)
    evaluation = _find(report, 1)

    assert "INDEXING_FOLLOWUP" in _types(evaluation)
    assert evaluation.indexability["observed_at"] is not None


def test_indexing_followup_is_suppressed_inside_the_grace_period(session: Session) -> None:
    _article(session, 1, "new", days_old=2)
    indexability = _Indexability([_IndexRow(1, "LIVE_HEALTHY", "SITEMAP_PRESENT")])

    assert "INDEXING_FOLLOWUP" not in _types(
        _find(_evaluate(session, indexability=indexability), 1)
    )


def test_stale_inspection_snapshot_does_not_trigger_followup(session: Session) -> None:
    _article(session, 1, "old", days_old=60)
    indexability = _Indexability(
        [_IndexRow(1, "LIVE_HEALTHY", "SITEMAP_PRESENT")],
        generated_at=_NOW - timedelta(days=90),
    )

    evaluation = _find(_evaluate(session, indexability=indexability), 1)
    assert "INDEXING_FOLLOWUP" not in _types(evaluation)
    assert evaluation.indexability["observed_days_ago"] == 90


# ==================== internal links ==========================================
def test_existing_internal_link_is_not_recommended_again(session: Session) -> None:
    _article(session, 16, "rpa-tools", days_old=60, article_type="recommendation_roundup")
    _article(
        session,
        17,
        "rpa-comparison",
        days_old=60,
        body=f"詳しくは[RPA比較]({_BASE}/rpa-tools/)を参照",
    )

    report = _evaluate(session)
    targets = {
        c["evidence"]["target_article_id"]
        for c in _find(report, 17).candidates
        if c["candidate_type"] == INTERNAL_LINK_OPPORTUNITY
    }
    assert 16 not in targets
    satisfied = [
        d
        for d in report.internal_link_debt
        if d["source_article_id"] == 17 and d["target_article_id"] == 16
    ]
    assert satisfied and satisfied[0]["status"] == "already_satisfied"


def test_missing_deferred_link_is_a_structural_candidate(session: Session) -> None:
    _article(session, 16, "rpa-tools", days_old=60, article_type="recommendation_roundup")
    _article(session, 17, "rpa-comparison", days_old=60)

    candidates = [
        c
        for c in _find(_evaluate(session), 17).candidates
        if c["candidate_type"] == INTERNAL_LINK_OPPORTUNITY
    ]
    assert candidates
    assert candidates[0]["evidence_strength"] == EVIDENCE_STRUCTURAL
    assert candidates[0]["priority"] == "low"


def test_internal_link_candidates_are_capped_per_article(session: Session) -> None:
    for article_id, slug in ((16, "rpa-tools"), (17, "rpa-comparison"), (18, "rpa-implementation")):
        _article(session, article_id, slug, days_old=60)

    report = _evaluate(session)
    for evaluation in report.articles:
        links = [
            c for c in evaluation.candidates if c["candidate_type"] == INTERNAL_LINK_OPPORTUNITY
        ]
        assert len(links) <= 2


def test_unknown_articles_are_never_linked(session: Session) -> None:
    """ポリシーに載っていても、公開されていない記事は候補にしない。"""

    _article(session, 17, "rpa-comparison", days_old=60)
    report = _evaluate(session)
    targets = {
        c["evidence"]["target_article_id"]
        for c in _find(report, 17).candidates
        if c["candidate_type"] == INTERNAL_LINK_OPPORTUNITY
    }
    assert 16 not in targets


# ==================== determinism / persistence ===============================
def test_repeated_evaluation_is_deterministic(session: Session) -> None:
    _article(session, 1, "mature", days_old=120)
    run = _sc_run(session)
    _page(session, run, "mature", clicks=2, impressions=1000, position=6.0)

    first = _evaluate(session)
    second = _evaluate(session)

    assert [(e.article_id, [c["dedupe_key"] for c in e.candidates]) for e in first.articles] == [
        (e.article_id, [c["dedupe_key"] for c in e.candidates]) for e in second.articles
    ]


def test_evaluation_does_not_mutate_the_article(session: Session) -> None:
    article = _article(session, 1, "mature", days_old=120)
    before = (article.body, article.title, article.status, article.published_url)

    _evaluate(session)

    session.refresh(article)
    assert (article.body, article.title, article.status, article.published_url) == before


def test_evaluation_alone_persists_nothing(session: Session) -> None:
    _article(session, 1, "mature", days_old=120)
    _evaluate(session)
    assert session.scalars(select(SeoImprovementRun)).all() == []


def test_persist_writes_an_append_only_run_with_candidates(session: Session) -> None:
    _article(session, 1, "mature", days_old=120, article_type="pricing")
    _source(session, 1, date(2026, 1, 1))
    service = SeoImprovementCandidateService(session, settings=_Settings())
    report = service.evaluate(days=30, now=_NOW)

    run = service.persist(report)

    assert run.policy_version == report.policy_version
    assert run.gsc_data_through == report.gsc_data_through
    assert run.candidate_count == report.total_candidates
    stored = session.scalars(select(SeoImprovementCandidate)).all()
    assert {c.candidate_type for c in stored} == {
        c["candidate_type"] for e in report.articles for c in e.candidates
    }


def test_persist_is_idempotent_for_the_same_key(session: Session) -> None:
    _article(session, 1, "mature", days_old=120)
    service = SeoImprovementCandidateService(session, settings=_Settings())
    report = service.evaluate(days=30, now=_NOW)

    first = service.persist(report, idempotency_key="k1")
    second = service.persist(report, idempotency_key="k1")

    assert first.id == second.id
    assert len(session.scalars(select(SeoImprovementRun)).all()) == 1


def test_two_runs_can_be_compared_over_time(session: Session) -> None:
    _article(session, 1, "mature", days_old=120)
    service = SeoImprovementCandidateService(session, settings=_Settings())
    service.persist(service.evaluate(days=30, now=_NOW), idempotency_key="day-0")
    service.persist(service.evaluate(days=30, now=_NOW), idempotency_key="day-3")

    runs = session.scalars(select(SeoImprovementRun).order_by(SeoImprovementRun.id)).all()
    assert len(runs) == 2
    assert all(r.policy_version for r in runs)


def test_report_records_the_data_through_dates_it_judged_with(session: Session) -> None:
    _article(session, 1, "mature", days_old=120)
    run = _sc_run(session)
    _page(session, run, "mature", clicks=1, impressions=10, position=9.0, day=date(2026, 10, 20))

    report = _evaluate(session)
    assert report.gsc_data_through == date(2026, 10, 20)
    assert report.ga4_data_through is None
    assert report.policy_version
