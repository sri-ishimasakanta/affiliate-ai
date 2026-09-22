"""RevenueOptimizationCandidateService の統合テスト (C7)。

pin する契約 (どれも「根拠の無い収益の主張をしない」ための防具):

- GA4 の行が無ければアフィリエイト CTR を一切判定しない。
- 露出が足りない記事に行動系の候補を出さない。
- 信頼境界より前のクリックは行動評価に使わないが、行は残る。
- 公式リンクしか無い記事を、勝手にアフィリエイトリンク化する提案にしない。
- Make の報酬をプログラム単位のまま扱い、記事へ配分しない。
- 帰属できない値は 0 ではなく ``None`` のままにする。
- 同じ入力なら決定的。``--execute`` 相当は履歴だけを書き、記事は変えない。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.article.fact_freshness import to_storage_utc
from app.models import (
    AffiliateClickImportRun,
    AffiliateCommissionFact,
    AffiliateCommissionImportRun,
    AffiliateLinkTarget,
    AffiliateOutboundClick,
    AffiliateProgram,
    Article,
    ArticleLinkSubstitutionMapping,
    Ga4ImportRun,
    Ga4PageDaily,
    RevenueOptimizationCandidate,
    RevenueOptimizationRun,
)
from app.revenue.candidates import (
    AFFILIATE_CLICK_THROUGH_REVIEW,
    AFFILIATE_DATA_QUALITY_REVIEW,
    AFFILIATE_LINK_HEALTH_REVIEW,
    COMMISSION_SIGNAL_REVIEW,
    HIGH_TRAFFIC_UNMONETIZED,
    MONETIZATION_COVERAGE_GAP,
    TRACKING_SETUP_REQUIRED,
    ZERO_CLICK_REVIEW,
)
from app.revenue.maturity import ATTRIBUTION_PROGRAM_ONLY, NOT_MONETIZABLE
from app.revenue.policy import RevenuePolicy, load_policy
from app.services.revenue_optimization_candidate_service import (
    RevenueOptimizationCandidateService,
)

_BASE = "https://bizfluxlab.com"
_GA4 = "555335688"
_NOW = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
_TODAY = _NOW.date()
#: 評価期間 (直近 30 日) の内側。既定ポリシーの信頼境界より後なので clean。
_CLEAN = datetime(2026, 11, 28, 9, 0, tzinfo=UTC)
#: 評価期間の内側だが、下の遅い信頼境界より前 -- 計測ノイズを模す。
_DIRTY = datetime(2026, 11, 20, 13, 0, tzinfo=UTC)
#: 2026-11-25 を境界にすると、_DIRTY は除外され _CLEAN は残る。
_LATE_CUTOFF = {"trusted_measurement_start_at": "2026-11-25T00:00:00+00:00"}


class _Settings:
    wordpress_base_url = _BASE
    search_console_property_uri = "sc-domain:bizfluxlab.com"
    search_console_credentials_file = None
    ga4_property_id = _GA4


class _NoGa4Settings(_Settings):
    ga4_property_id = None


def _policy(**over) -> RevenuePolicy:
    raw = dict(load_policy().raw)
    for section, values in over.items():
        raw[section] = {**raw.get(section, {}), **values}
    return RevenuePolicy(policy_version="test", raw=raw)


def _article(
    session: Session,
    article_id: int,
    slug: str,
    *,
    mode: str | None = "affiliate",
    days_old: int = 60,
) -> Article:
    article = Article(
        id=article_id,
        title=slug,
        slug=slug,
        keyword_id=None,
        body="本文",
        status="published",
        published_url=f"{_BASE}/{slug}/",
        published_at=_NOW - timedelta(days=days_old),
        article_type="how_to",
        monetization_mode=mode,
    )
    session.add(article)
    session.commit()
    return article


def _program(session: Session, name: str, *, tracking: bool, status: str = "active"):
    program = AffiliateProgram(
        name=name,
        status=status,
        tracking_url="https://track.test/x" if tracking else None,
    )
    session.add(program)
    session.commit()
    return program


def _link_program(session: Session, article_id: int, program_id: int, *, primary=True) -> None:
    session.execute(
        ArticleLinkSubstitutionMapping.__table__.metadata.tables[
            "article_affiliate_programs"
        ].insert(),
        {"article_id": article_id, "affiliate_program_id": program_id, "is_primary": primary},
    )
    session.commit()


def _target(session: Session, article_id: int, program_id: int, *, status="active", token="tok"):
    target = AffiliateLinkTarget(
        token=token,
        article_id=article_id,
        affiliate_program_id=program_id,
        destination_url="https://www.make.com/en/register",
        destination_host="www.make.com",
        status=status,
        link_identity_hash=token.ljust(64, "0"),
    )
    session.add(target)
    session.commit()
    return target


def _mapping(session: Session, article_id: int, target_id: int, *, status="active"):
    mapping = ArticleLinkSubstitutionMapping(
        article_id=article_id,
        affiliate_link_target_id=target_id,
        original_href="https://www.make.com/en/register",
        occurrence_identity_hash="d" * 64,
        status=status,
        approved_at=_NOW,
    )
    session.add(mapping)
    session.commit()
    return mapping


def _ga4(session: Session, slug: str, *, sessions: int, organic: int) -> None:
    run = Ga4ImportRun(
        property_id=_GA4,
        property_timezone="Asia/Tokyo",
        start_date=_TODAY,
        end_date=_TODAY,
        status="succeeded",
        request_json="{}",
        import_identity_hash="b" * 64,
    )
    session.add(run)
    session.commit()
    for scope, count in (("all", sessions), ("organic_search", organic)):
        session.add(
            Ga4PageDaily(
                property_id=_GA4,
                metric_date=_TODAY - timedelta(days=1),
                page_path=f"/{slug}/",
                channel_scope=scope,
                sessions=count,
                active_users=count,
                new_users=count,
                engaged_sessions=count // 2,
                engagement_rate=0.5,
                average_engagement_time_seconds=40.0,
                screen_page_views=count,
                source_import_run_id=run.id,
            )
        )
    session.commit()


def _click(session: Session, token: str, when: datetime, *, source_click_id: int = 1) -> None:
    run = session.scalars(select(AffiliateClickImportRun)).first()
    if run is None:
        run = AffiliateClickImportRun(status="succeeded", requested_since_id=0, requested_limit=100)
        session.add(run)
        session.commit()
    session.add(
        AffiliateOutboundClick(
            source_click_id=source_click_id,
            token=token,
            clicked_at=when,
            source_import_run_id=run.id,
        )
    )
    session.commit()


def _commission(session: Session, program_id: int, amount: str = "12.50") -> None:
    run = AffiliateCommissionImportRun(
        provider="make",
        affiliate_program_id=program_id,
        status="succeeded",
        requested_date_from=_TODAY,
        requested_date_to=_TODAY,
    )
    session.add(run)
    session.commit()
    session.add(
        AffiliateCommissionFact(
            affiliate_program_id=program_id,
            provider="make",
            source_commission_id="c1",
            event_type="sale",
            provider_status="approved",
            commission_amount=Decimal(amount),
            currency="USD",
            occurred_at=_NOW - timedelta(days=2),
            first_seen_at=_NOW,
            last_seen_at=_NOW,
            source_import_run_id=run.id,
        )
    )
    session.commit()


def _evaluate(session: Session, *, settings=None, policy=None, days: int = 30):
    return RevenueOptimizationCandidateService(
        session, settings=settings or _Settings(), policy=policy or _policy()
    ).evaluate(days=days, now=_NOW)


def _types(evaluation) -> set[str]:
    return {c["candidate_type"] for c in evaluation.candidates}


def _find(report, article_id: int):
    return next(e for e in report.articles if e.article_id == article_id)


def _global_types(report) -> set[str]:
    return {c["candidate_type"] for c in report.program_candidates + report.data_quality_candidates}


# ==================== behavioural suppression =================================
def test_missing_ga4_rows_produce_no_click_through_judgement(session: Session) -> None:
    article = _article(session, 10, "make-how-to")
    program = _program(session, "Make", tracking=True)
    target = _target(session, article.id, program.id)
    _mapping(session, article.id, target.id)

    evaluation = _find(_evaluate(session), 10)

    assert AFFILIATE_CLICK_THROUGH_REVIEW not in _types(evaluation)
    assert ZERO_CLICK_REVIEW not in _types(evaluation)
    assert evaluation.sessions is None
    assert evaluation.clicks_per_100_organic_sessions is None


def test_unconfigured_ga4_produces_no_behavioural_candidate(session: Session) -> None:
    article = _article(session, 10, "make-how-to")
    program = _program(session, "Make", tracking=True)
    target = _target(session, article.id, program.id)
    _mapping(session, article.id, target.id)

    evaluation = _find(_evaluate(session, settings=_NoGa4Settings()), 10)
    assert AFFILIATE_CLICK_THROUGH_REVIEW not in _types(evaluation)


def test_insufficient_sessions_produce_no_behavioural_candidate(session: Session) -> None:
    article = _article(session, 10, "make-how-to")
    program = _program(session, "Make", tracking=True)
    target = _target(session, article.id, program.id)
    _mapping(session, article.id, target.id)
    _ga4(session, "make-how-to", sessions=10, organic=5)

    evaluation = _find(_evaluate(session), 10)
    assert ZERO_CLICK_REVIEW not in _types(evaluation)
    assert AFFILIATE_CLICK_THROUGH_REVIEW not in _types(evaluation)


def test_sufficient_sessions_with_zero_clean_clicks_is_a_zero_click_review(
    session: Session,
) -> None:
    article = _article(session, 10, "make-how-to")
    program = _program(session, "Make", tracking=True)
    target = _target(session, article.id, program.id, token="tok-10")
    _mapping(session, article.id, target.id)
    _ga4(session, "make-how-to", sessions=1000, organic=600)
    # 期間を重ねるために clean なクリックを別記事で 1 件記録する。
    _click(session, "tok-other", _CLEAN)

    evaluation = _find(_evaluate(session), 10)

    assert ZERO_CLICK_REVIEW in _types(evaluation)
    candidate = next(c for c in evaluation.candidates if c["candidate_type"] == ZERO_CLICK_REVIEW)
    assert candidate["evidence_basis"] == "behavioral"
    assert candidate["evidence"]["clean_clicks"] == 0
    assert "断定" in candidate["suggested_action"]


def test_instrumentation_clicks_do_not_create_click_performance(session: Session) -> None:
    """2026-09-22 の自作クリックで「送客できている」と言わせない。"""

    article = _article(session, 10, "make-how-to")
    program = _program(session, "Make", tracking=True)
    target = _target(session, article.id, program.id, token="tok-10")
    _mapping(session, article.id, target.id)
    _ga4(session, "make-how-to", sessions=1000, organic=600)
    for index in range(8):
        _click(session, "tok-10", _DIRTY, source_click_id=index + 1)
    _click(session, "tok-other", _CLEAN, source_click_id=100)

    report = _evaluate(session, policy=_policy(trusted_click_measurement=_LATE_CUTOFF))
    evaluation = _find(report, 10)

    assert evaluation.raw_clicks == 8
    assert evaluation.excluded_clicks == 8
    assert evaluation.clean_clicks == 0
    assert AFFILIATE_CLICK_THROUGH_REVIEW not in _types(evaluation)
    assert ZERO_CLICK_REVIEW in _types(evaluation)


def test_mature_sample_yields_a_click_through_review_using_organic(session: Session) -> None:
    article = _article(session, 10, "make-how-to")
    program = _program(session, "Make", tracking=True)
    target = _target(session, article.id, program.id, token="tok-10")
    _mapping(session, article.id, target.id)
    _ga4(session, "make-how-to", sessions=2000, organic=1000)
    for index in range(25):
        _click(session, "tok-10", _CLEAN, source_click_id=index + 1)

    evaluation = _find(_evaluate(session), 10)
    candidate = next(
        c for c in evaluation.candidates if c["candidate_type"] == AFFILIATE_CLICK_THROUGH_REVIEW
    )

    assert candidate["evidence"]["denominator"] == "organic_sessions"
    assert candidate["evidence"]["organic_sessions"] == 1000
    assert candidate["evidence"]["clean_clicks"] == 25
    assert evaluation.clicks_per_100_organic_sessions == 2.5


def test_non_overlapping_windows_suppress_ratios(session: Session) -> None:
    article = _article(session, 10, "make-how-to")
    program = _program(session, "Make", tracking=True)
    target = _target(session, article.id, program.id, token="tok-10")
    _mapping(session, article.id, target.id)
    _ga4(session, "make-how-to", sessions=2000, organic=1000)
    # clean なクリックが 1 件も無い -> クリック側の data-through が無い。
    _click(session, "tok-10", _DIRTY)

    report = _evaluate(session, policy=_policy(trusted_click_measurement=_LATE_CUTOFF))

    assert report.windows_overlap is False
    assert AFFILIATE_CLICK_THROUGH_REVIEW not in _types(_find(report, 10))
    assert ZERO_CLICK_REVIEW not in _types(_find(report, 10))
    assert AFFILIATE_DATA_QUALITY_REVIEW in _global_types(report)


# ==================== structural ==============================================
def test_supporting_article_is_not_asked_to_monetize(session: Session) -> None:
    _article(session, 20, "chatgpt-enterprise", mode="supporting")
    evaluation = _find(_evaluate(session), 20)
    assert evaluation.maturity_state == NOT_MONETIZABLE
    assert evaluation.candidates == []
    assert "supporting" in evaluation.no_action_reason


def test_official_only_article_does_not_become_an_affiliate_link(session: Session) -> None:
    """公式リンクしか無い記事に、アフィリエイトリンク化を自動提案しない。"""

    _article(session, 20, "chatgpt-enterprise", mode="supporting")
    _ga4(session, "chatgpt-enterprise", sessions=5000, organic=4000)

    evaluation = _find(_evaluate(session), 20)
    assert HIGH_TRAFFIC_UNMONETIZED not in _types(evaluation)
    assert MONETIZATION_COVERAGE_GAP not in _types(evaluation)


def test_affiliate_article_with_untracked_program_is_a_setup_candidate(
    session: Session,
) -> None:
    article = _article(session, 2, "ai-meeting-notes-tools")
    program = _program(session, "Krisp", tracking=False)
    _link_program(session, article.id, program.id)

    evaluation = _find(_evaluate(session), 2)
    candidate = next(
        c for c in evaluation.candidates if c["candidate_type"] == TRACKING_SETUP_REQUIRED
    )

    assert candidate["evidence"]["tracking_url_present"] is False
    assert candidate["evidence"]["program_name"] == "Krisp"
    assert candidate["evidence_basis"] == "structural"
    # 提携が承認済みだとは主張しない。
    assert "提携状況を確認" in candidate["suggested_action"]


def test_affiliate_article_without_any_program_is_a_coverage_gap(session: Session) -> None:
    _article(session, 2, "ai-meeting-notes-tools")
    evaluation = _find(_evaluate(session), 2)
    candidate = next(
        c for c in evaluation.candidates if c["candidate_type"] == MONETIZATION_COVERAGE_GAP
    )
    assert candidate["reason_code"] == "NO_AFFILIATE_PROGRAM_LINKED"


def test_tracked_program_without_a_target_is_a_coverage_gap(session: Session) -> None:
    article = _article(session, 2, "ai-meeting-notes-tools")
    program = _program(session, "Make", tracking=True)
    _link_program(session, article.id, program.id)

    evaluation = _find(_evaluate(session), 2)
    candidate = next(
        c for c in evaluation.candidates if c["candidate_type"] == MONETIZATION_COVERAGE_GAP
    )
    assert candidate["reason_code"] == "NO_ACTIVE_AFFILIATE_TARGET"


def test_inactive_target_is_a_link_health_candidate(session: Session) -> None:
    article = _article(session, 10, "make-how-to")
    program = _program(session, "Make", tracking=True)
    _target(session, article.id, program.id, status="disabled")

    evaluation = _find(_evaluate(session), 10)
    assert AFFILIATE_LINK_HEALTH_REVIEW in _types(evaluation)


def test_active_target_without_mapping_is_a_link_health_candidate(session: Session) -> None:
    article = _article(session, 10, "make-how-to")
    program = _program(session, "Make", tracking=True)
    _target(session, article.id, program.id)

    evaluation = _find(_evaluate(session), 10)
    candidate = next(
        c for c in evaluation.candidates if c["candidate_type"] == AFFILIATE_LINK_HEALTH_REVIEW
    )
    assert candidate["reason_code"] == "NO_ACTIVE_SUBSTITUTION_MAPPING"
    assert candidate["priority"] == "high"


def test_healthy_make_article_has_no_structural_candidate(session: Session) -> None:
    """記事 10/11 相当の正常な構成では構造的な指摘を出さない。"""

    for article_id, slug, token in ((10, "make-how-to", "tok-10"), (11, "make-pricing", "tok-11")):
        article = _article(session, article_id, slug)
        program = _program(session, f"Make-{article_id}", tracking=True)
        target = _target(session, article.id, program.id, token=token)
        _mapping(session, article.id, target.id)

    report = _evaluate(session)
    for article_id in (10, 11):
        evaluation = _find(report, article_id)
        assert evaluation.monetized is True
        assert AFFILIATE_LINK_HEALTH_REVIEW not in _types(evaluation)
        assert MONETIZATION_COVERAGE_GAP not in _types(evaluation)
        assert TRACKING_SETUP_REQUIRED not in _types(evaluation)


# ==================== commission attribution ==================================
def test_commission_stays_at_program_level(session: Session) -> None:
    article = _article(session, 10, "make-how-to")
    program = _program(session, "Make", tracking=True)
    target = _target(session, article.id, program.id, token="tok-10")
    _mapping(session, article.id, target.id)
    _click(session, "tok-10", _CLEAN)
    _commission(session, program.id)

    report = _evaluate(session)

    assert len(report.program_commissions) == 1
    bucket = report.program_commissions[0]
    assert bucket["attribution"] == ATTRIBUTION_PROGRAM_ONLY
    assert bucket["commission_amount"] == "12.50"
    assert bucket["article_level_revenue"] is None
    # 記事側には決して配分されない。
    assert all(e.article_level_revenue is None for e in report.articles)


def test_commission_is_never_split_between_two_articles(session: Session) -> None:
    """記事 10 と 11 の両方にクリックがあっても、報酬を按分しない。"""

    program = _program(session, "Make", tracking=True)
    for article_id, slug, token in ((10, "make-how-to", "tok-10"), (11, "make-pricing", "tok-11")):
        article = _article(session, article_id, slug)
        target = _target(session, article.id, program.id, token=token)
        _mapping(session, article.id, target.id)
    _click(session, "tok-10", _CLEAN, source_click_id=1)
    _click(session, "tok-11", _CLEAN, source_click_id=2)
    _commission(session, program.id, amount="100.00")

    report = _evaluate(session)
    candidate = next(
        c for c in report.program_candidates if c["candidate_type"] == COMMISSION_SIGNAL_REVIEW
    )

    assert candidate["affiliate_program_id"] == program.id
    assert candidate["evidence"]["article_level_revenue"] is None
    assert sorted(candidate["evidence"]["article_ids_with_clean_clicks"]) == [10, 11]
    assert all(e.article_level_revenue is None for e in report.articles)
    # 記事単位の候補に金額が紛れ込んでいない。
    for evaluation in report.articles:
        for article_candidate in evaluation.candidates:
            assert "commission_amount" not in article_candidate["evidence"]


def test_missing_commission_data_is_unavailable_not_zero(session: Session) -> None:
    article = _article(session, 10, "make-how-to")
    program = _program(session, "Make", tracking=True)
    target = _target(session, article.id, program.id, token="tok-10")
    _mapping(session, article.id, target.id)

    report = _evaluate(session)

    assert report.program_commissions == []
    assert _find(report, 10).attribution == "unavailable"
    assert _find(report, 10).article_level_revenue is None


# ==================== data quality ============================================
def test_excluded_clicks_raise_a_data_quality_candidate(session: Session) -> None:
    article = _article(session, 10, "make-how-to")
    program = _program(session, "Make", tracking=True)
    target = _target(session, article.id, program.id, token="tok-10")
    _mapping(session, article.id, target.id)
    _click(session, "tok-10", _DIRTY)

    report = _evaluate(session, policy=_policy(trusted_click_measurement=_LATE_CUTOFF))
    codes = {c["reason_code"] for c in report.data_quality_candidates}

    assert "CLICKS_BEFORE_TRUSTED_BASELINE" in codes
    assert report.raw_clicks == 1
    assert report.excluded_clicks == 1
    assert report.clean_clicks == 0


def test_unattributed_token_raises_a_data_quality_candidate(session: Session) -> None:
    _article(session, 10, "make-how-to")
    _click(session, "tok-synthetic", _CLEAN)

    codes = {c["reason_code"] for c in _evaluate(session).data_quality_candidates}
    assert "UNATTRIBUTED_CLICK_TOKENS" in codes


# ==================== determinism / persistence ===============================
def test_repeated_evaluation_is_deterministic(session: Session) -> None:
    article = _article(session, 10, "make-how-to")
    program = _program(session, "Make", tracking=True)
    target = _target(session, article.id, program.id, token="tok-10")
    _mapping(session, article.id, target.id)

    first = _evaluate(session)
    second = _evaluate(session)

    assert [(e.article_id, [c["dedupe_key"] for c in e.candidates]) for e in first.articles] == [
        (e.article_id, [c["dedupe_key"] for c in e.candidates]) for e in second.articles
    ]


def test_evaluation_does_not_mutate_articles_or_clicks(session: Session) -> None:
    article = _article(session, 10, "make-how-to")
    program = _program(session, "Make", tracking=True)
    target = _target(session, article.id, program.id, token="tok-10")
    _mapping(session, article.id, target.id)
    _click(session, "tok-10", _DIRTY)
    before = (article.body, article.status, article.published_url, target.status)

    _evaluate(session)

    session.refresh(article)
    session.refresh(target)
    assert (article.body, article.status, article.published_url, target.status) == before
    assert len(session.scalars(select(AffiliateOutboundClick)).all()) == 1


def test_evaluation_alone_persists_nothing(session: Session) -> None:
    _article(session, 10, "make-how-to")
    _evaluate(session)
    assert session.scalars(select(RevenueOptimizationRun)).all() == []


def test_persist_writes_an_append_only_run(session: Session) -> None:
    article = _article(session, 2, "ai-meeting-notes-tools")
    program = _program(session, "Krisp", tracking=False)
    _link_program(session, article.id, program.id)
    _click(session, "tok-synthetic", _CLEAN)
    service = RevenueOptimizationCandidateService(session, settings=_Settings(), policy=_policy())
    report = service.evaluate(days=30, now=_NOW)

    run = service.persist(report)

    assert run.policy_version == report.policy_version
    # SQLite は tzinfo を落とすので、UTC の wall-clock として一致することを見る。
    assert run.trusted_measurement_start_at == to_storage_utc(report.trusted_measurement_start_at)
    assert run.raw_clicks == report.raw_clicks
    assert run.excluded_clicks == report.excluded_clicks
    assert run.clean_clicks == report.clean_clicks
    assert run.candidate_count == report.total_candidates
    stored = session.scalars(select(RevenueOptimizationCandidate)).all()
    assert stored
    assert any(c.article_id == 2 for c in stored)
    assert any(c.article_id is None for c in stored)


def test_persist_is_idempotent_for_the_same_key(session: Session) -> None:
    _article(session, 10, "make-how-to")
    service = RevenueOptimizationCandidateService(session, settings=_Settings(), policy=_policy())
    report = service.evaluate(days=30, now=_NOW)

    first = service.persist(report, idempotency_key="k1")
    second = service.persist(report, idempotency_key="k1")

    assert first.id == second.id
    assert len(session.scalars(select(RevenueOptimizationRun)).all()) == 1


def test_run_freezes_the_trusted_cutoff_for_later_comparison(session: Session) -> None:
    _article(session, 10, "make-how-to")
    service = RevenueOptimizationCandidateService(session, settings=_Settings(), policy=_policy())
    run = service.persist(service.evaluate(days=30, now=_NOW), idempotency_key="day-0")

    assert run.trusted_measurement_start_at is not None
    assert run.summary_json["windows_overlap"] is False
