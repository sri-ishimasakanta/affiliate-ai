"""KeywordAnalysisService (Phase 2C-1 workflow orchestration) の検証。"""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.keyword.providers.google_ads import (
    GoogleAdsKeywordMetrics,
    MonthlySearchVolume,
)
from app.keyword.schemas import CompetitionEaseManualCreate, KeywordSignalCreate
from app.models import Keyword
from app.repositories.keyword_signal_repository import KeywordSignalRepository
from app.services.keyword_analysis_service import (
    AUTO_COMPONENTS,
    KeywordAnalysisService,
    normalize_keyword_inputs,
)
from app.services.keyword_metrics_collection_service import (
    GOOGLE_ADS_BUNDLE_COMPONENTS,
    KeywordMetricsCollectionService,
)
from app.services.keyword_signal_service import KeywordSignalService
from tests.support.google_ads_fakes import (
    FakeGoogleAdsProvider,
    dummy_google_ads_settings,
)


def _metrics(keyword: str, *, avg: int = 1000) -> GoogleAdsKeywordMetrics:
    volumes = tuple(
        MonthlySearchVolume(year=2025, month=i + 1, monthly_searches=avg + i * 15)
        for i in range(8)
    )
    return GoogleAdsKeywordMetrics(
        keyword=keyword,
        avg_monthly_searches=avg,
        monthly_search_volumes=volumes,
        competition="HIGH",
        competition_index=70,
        low_top_of_page_bid_micros=150_000_000,
        high_top_of_page_bid_micros=800_000_000,
    )


def _kw(session: Session, text: str, status: str = "analyzed") -> Keyword:
    entity = Keyword(keyword=text)
    entity.status = status
    session.add(entity)
    session.flush()
    session.commit()
    return entity


def _service(session: Session, *, provider: object | None = None) -> KeywordAnalysisService:
    metrics = KeywordMetricsCollectionService(
        session,
        provider=provider or FakeGoogleAdsProvider(metrics=[]),
        settings=dummy_google_ads_settings(),
    )
    return KeywordAnalysisService(session, metrics_service=metrics)


def _add_signal(session: Session, keyword_id: int, component: str, value: float = 50.0) -> None:
    KeywordSignalService(session).create_signal(
        keyword_id,
        KeywordSignalCreate(
            component=component,
            normalized_value=value,
            provider="manual",
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    )


# -- input normalization ------------------------------------------
def test_normalize_keyword_inputs() -> None:
    result = normalize_keyword_inputs(
        ["  AI 議事録 ", "AI 議事録", "", "  "], ["ChatGPT 料金", "AI 議事録"]
    )
    assert result == ["AI 議事録", "ChatGPT 料金"]


# -- resolve ----------------------------------------------------
def test_resolve_existing_only_by_default(session: Session) -> None:
    _kw(session, "existing kw")
    result = _service(session).resolve_keywords(
        ["existing kw", "missing kw"], create_missing=False
    )
    assert [k.keyword for k in result.resolved] == ["existing kw"]
    assert result.unresolved == ["missing kw"]
    assert result.created == []


def test_resolve_create_missing(session: Session) -> None:
    _kw(session, "existing kw")
    result = _service(session).resolve_keywords(
        ["existing kw", "brand new kw"], create_missing=True
    )
    assert {k.keyword for k in result.resolved} == {"existing kw", "brand new kw"}
    assert len(result.created) == 1
    assert result.unresolved == []


# -- readiness -------------------------------------------------
def test_readiness_complete_7_7(session: Session) -> None:
    keyword = _kw(session, "kw complete")
    for component in (
        "search_demand",
        "commercial_intent",
        "trend",
        "site_relevance",
        "affiliate_opportunity",
        "originality",
        "competition_ease",
    ):
        _add_signal(session, keyword.id, component)
    state = _service(session).readiness(keyword.id)
    assert state.complete is True
    assert state.missing == ()
    assert len(state.present) == 7


def test_readiness_missing_only_competition(session: Session) -> None:
    keyword = _kw(session, "kw 6of7")
    for component in (
        "search_demand",
        "commercial_intent",
        "trend",
        "site_relevance",
        "affiliate_opportunity",
        "originality",
    ):
        _add_signal(session, keyword.id, component)
    state = _service(session).readiness(keyword.id)
    assert state.complete is False
    assert state.missing == ("competition_ease",)


def test_readiness_multiple_missing(session: Session) -> None:
    keyword = _kw(session, "kw new")
    _add_signal(session, keyword.id, "site_relevance")
    state = _service(session).readiness(keyword.id)
    assert set(state.missing) == {
        "search_demand",
        "commercial_intent",
        "affiliate_opportunity",
        "competition_ease",
        "trend",
        "originality",
    }


# -- collect_auto_signals --------------------------------------
def test_collect_auto_signals_one_bulk_fetch(session: Session) -> None:
    a = _kw(session, "AI 議事録 おすすめ")
    b = _kw(session, "ChatGPT 料金")
    provider = FakeGoogleAdsProvider(
        metrics=[_metrics("AI 議事録 おすすめ"), _metrics("ChatGPT 料金")]
    )
    service = _service(session, provider=provider)

    report = service.collect_auto_signals([a.id, b.id], refresh=False)

    assert provider.calls == [["AI 議事録 おすすめ", "ChatGPT 料金"]]  # 1 bulk fetch
    assert report.created["search_demand"] == 2
    assert report.created["site_relevance"] == 2
    assert report.created["originality"] == 2
    assert report.provider_error is None
    for kid in (a.id, b.id):
        r = service.readiness(kid)
        assert set(r.missing) == {"competition_ease"}


def test_collect_auto_signals_default_reuses_existing(session: Session) -> None:
    a = _kw(session, "AI 議事録 おすすめ")
    for component in AUTO_COMPONENTS:
        _add_signal(session, a.id, component, value=11.0)
    provider = FakeGoogleAdsProvider(metrics=[_metrics("AI 議事録 おすすめ")])
    service = _service(session, provider=provider)

    report = service.collect_auto_signals([a.id], refresh=False)

    # 何も再生成せず、Google Ads も呼ばない
    assert provider.calls == []
    assert report.created == {}
    assert sum(report.reused.values()) == len(AUTO_COMPONENTS)
    # 既存 Signal 値は不変 (history が増えていない)
    repo = KeywordSignalRepository(session)
    assert repo.get_latest(a.id, "site_relevance").normalized_value == 11.0
    assert len(repo.list_by_component(a.id, "site_relevance")) == 1


def test_collect_auto_signals_refresh_creates_new_history(session: Session) -> None:
    a = _kw(session, "AI 議事録 おすすめ")
    for component in AUTO_COMPONENTS:
        _add_signal(session, a.id, component, value=11.0)
    provider = FakeGoogleAdsProvider(metrics=[_metrics("AI 議事録 おすすめ")])
    service = _service(session, provider=provider)

    report = service.collect_auto_signals([a.id], refresh=True)

    assert provider.calls == [["AI 議事録 おすすめ"]]
    assert sum(report.created.values()) == len(AUTO_COMPONENTS)
    repo = KeywordSignalRepository(session)
    assert len(repo.list_by_component(a.id, "site_relevance")) == 2  # history 追記


def test_collect_auto_signals_provider_error_still_does_local(session: Session) -> None:
    from app.exceptions import ExternalProviderError

    a = _kw(session, "AI 議事録 おすすめ")
    provider = FakeGoogleAdsProvider(
        error=ExternalProviderError("google_ads", "Google Ads API request failed")
    )
    service = _service(session, provider=provider)

    report = service.collect_auto_signals([a.id], refresh=False)

    assert report.provider_error is not None
    assert sum(report.failed.get(c, 0) for c in GOOGLE_ADS_BUNDLE_COMPONENTS) == 3
    # local は成功
    assert report.created["site_relevance"] == 1
    assert report.created["affiliate_opportunity"] == 1
    assert report.created["originality"] == 1


# -- score_ready --------------------------------------------
def test_score_ready_only_complete(session: Session) -> None:
    complete = _kw(session, "kw complete")
    incomplete = _kw(session, "kw incomplete")
    for component in (
        "search_demand",
        "commercial_intent",
        "trend",
        "site_relevance",
        "affiliate_opportunity",
        "originality",
        "competition_ease",
    ):
        _add_signal(session, complete.id, component, value=60.0)
    _add_signal(session, incomplete.id, "search_demand", value=60.0)

    outcomes = {
        o.keyword_id: o
        for o in _service(session).score_ready([complete.id, incomplete.id])
    }
    assert outcomes[complete.id].status == "scored"
    assert outcomes[complete.id].total_score == 60.0  # 全 60 -> weighted mean 60
    assert outcomes[incomplete.id].status == "incomplete"
    assert outcomes[incomplete.id].total_score is None

    session.expire_all()
    assert session.get(Keyword, complete.id).opportunity_score == 60.0


def test_score_ready_reuses_existing_score(session: Session) -> None:
    keyword = _kw(session, "kw complete")
    for component in (
        "search_demand",
        "commercial_intent",
        "trend",
        "site_relevance",
        "affiliate_opportunity",
        "originality",
        "competition_ease",
    ):
        _add_signal(session, keyword.id, component, value=60.0)
    service = _service(session)
    first = service.score_ready([keyword.id])[0]
    assert first.status == "scored"
    second = service.score_ready([keyword.id])[0]
    assert second.status == "reused"
    assert second.total_score == 60.0


# -- ranking ------------------------------------------------
def test_ranking_order_and_columns(session: Session) -> None:
    low = _kw(session, "kw low")
    high = _kw(session, "kw high")
    incomplete = _kw(session, "kw incomplete")
    for kw, val in ((low, 30.0), (high, 90.0)):
        for component in (
            "search_demand",
            "commercial_intent",
            "trend",
            "site_relevance",
            "affiliate_opportunity",
            "originality",
            "competition_ease",
        ):
            _add_signal(session, kw.id, component, value=val)
    _add_signal(session, incomplete.id, "search_demand", value=99.0)

    service = _service(session)
    service.score_ready([low.id, high.id, incomplete.id])
    rows = service.ranking_rows([low.id, high.id, incomplete.id])

    assert [r.keyword for r in rows] == ["kw high", "kw low", "kw incomplete"]
    assert rows[0].opportunity_score == 90.0
    assert rows[2].analysis_status == "incomplete"
    assert rows[2].opportunity_score is None
    assert "competition_ease" in rows[2].missing_components
    assert rows[0].component_values["search_demand"] == 90.0


def test_ranking_deterministic_keyword_asc_within_same_score(session: Session) -> None:
    b = _kw(session, "bbb")
    a = _kw(session, "aaa")
    for kw in (a, b):
        for component in (
            "search_demand",
            "commercial_intent",
            "trend",
            "site_relevance",
            "affiliate_opportunity",
            "originality",
            "competition_ease",
        ):
            _add_signal(session, kw.id, component, value=50.0)
    service = _service(session)
    service.score_ready([a.id, b.id])
    rows = service.ranking_rows([b.id, a.id])
    assert [r.keyword for r in rows] == ["aaa", "bbb"]


def test_competition_ease_missing_helper(session: Session) -> None:
    with_ce = _kw(session, "kw has ce")
    without_ce = _kw(session, "kw no ce")
    KeywordSignalService(session).derive_competition_ease_manual(
        with_ce.id,
        CompetitionEaseManualCreate(keyword_difficulty=30, source_name="t"),
    )
    missing = _service(session).competition_ease_missing([with_ce.id, without_ce.id])
    assert [k.keyword for k in missing] == ["kw no ce"]
