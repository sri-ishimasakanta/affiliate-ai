"""app/services/content_queue_service.py — DB から入力を読み read-only で制作キューを組む。

DB write 0 / HTTP 0 / LLM 0 を、実際に発行された SQL と session 状態で検証する。
"""

from __future__ import annotations

import socket
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import Engine, event, func, select
from sqlalchemy.orm import Session

from app.article.cluster_plan import ClusterConfigError, parse_cluster_config
from app.models import (
    AffiliateProgram,
    Article,
    ArticleFact,
    Keyword,
    KeywordScore,
    KeywordSignal,
)
from app.models.enums import AffiliateProgramStatus
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.repositories.article_fact_repository import ArticleFactRepository
from app.repositories.article_repository import ArticleRepository
from app.repositories.keyword_repository import KeywordRepository
from app.repositories.keyword_score_repository import KeywordScoreRepository
from app.repositories.keyword_signal_repository import KeywordSignalRepository
from app.services.content_queue_service import (
    ContentQueueReadOnlyViolationError,
    ContentQueueService,
)

_TRACKING = "https://track.example.invalid/secret-tracking-token"
_CONFIG = {
    "version": 1,
    "clusters": [
        {
            "id": "B",
            "name": "productivity",
            "priority": 1,
            "keywords": [
                {"keyword": "業務効率化 ツール おすすめ", "role": "pillar"},
                {"keyword": "業務効率化 ツール 比較", "role": "supporting"},
                {"keyword": "業務効率化 ツール 無料", "role": "supporting"},
            ],
        },
        {
            "id": "C",
            "name": "automation",
            "priority": 2,
            "keywords": [
                {"keyword": "RPA おすすめ", "role": "pillar"},
                {"keyword": "RPA 比較", "role": "supporting"},
                {"keyword": "Make 料金", "role": "supporting"},
            ],
        },
    ],
    "vocabulary": {
        "product_terms": ["Make"],
        "generic_theme_tokens": ["ツール"],
        "theme_aliases": {},
    },
}


def _seed_keyword(session: Session, text: str, *, status: str = "analyzed", score=None) -> Keyword:
    keyword = KeywordRepository(session).create(keyword=text)
    keyword.status = status
    if score is not None:
        KeywordScoreRepository(session).create(
            keyword_id=keyword.id,
            search_demand=10.0,
            commercial_intent=20.0,
            affiliate_opportunity=30.0,
            competition_ease=40.0,
            trend=50.0,
            originality=60.0,
            site_relevance=70.0,
            total_score=score,
            score_version="v1",
            input_source="signals",
        )
    return keyword


def _seed_article(
    session: Session, keyword: Keyword | None, status: str, slug: str, title: str = "t"
) -> Article:
    article = ArticleRepository(session).create(
        title=title, slug=slug, keyword_id=keyword.id if keyword else None
    )
    article.status = status
    return article


@pytest.fixture
def seeded(session: Session) -> dict[str, Keyword]:
    keywords = {
        text: _seed_keyword(session, text, score=score)
        for text, score in {
            "業務効率化 ツール おすすめ": 68.81,
            "業務効率化 ツール 比較": None,
            "業務効率化 ツール 無料": 60.39,
            "RPA おすすめ": 50.94,
            "RPA 比較": 50.47,
            "Make 料金": None,
            "ChatGPT 料金": 53.13,
        }.items()
    }
    programs = AffiliateProgramRepository(session)
    programs.create(
        name="Make",
        provider="make",
        match_terms=["Make"],
        tracking_url=_TRACKING,
        landing_page_url="https://www.example.invalid/make",
    )
    programs.create(
        name="Paused RPA Tool",
        provider="direct",
        match_terms=["RPA"],
        status=AffiliateProgramStatus.PAUSED,
    )
    session.commit()
    return keywords


def _table_counts(session: Session) -> dict[str, int]:
    return {
        model.__tablename__: session.scalar(select(func.count()).select_from(model))
        for model in (Keyword, KeywordScore, KeywordSignal, Article, ArticleFact, AffiliateProgram)
    }


def _by_kw(queue) -> dict:
    return {e.keyword: e for e in (*queue.slots, *queue.merged, *queue.blocked)}


def _service(session: Session) -> tuple[ContentQueueService, object]:
    return ContentQueueService(session), parse_cluster_config(_CONFIG)


# ============================================================= reading inputs
def test_build_reads_scores_components_missing_signals_and_affiliate_coverage(
    session: Session, seeded: dict[str, Keyword]
) -> None:
    KeywordSignalRepository(session).create(
        keyword_id=seeded["Make 料金"].id,
        component="search_demand",
        normalized_value=39.18,
        provider="google_ads",
        observed_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    session.commit()

    service, config = _service(session)
    by = _by_kw(service.build(config))

    scored = by["RPA おすすめ"]
    assert scored.opportunity_score == 50.94
    assert scored.components == {
        "search_demand": 10.0,
        "commercial_intent": 20.0,
        "affiliate_opportunity": 30.0,
        "competition_ease": 40.0,
        "trend": 50.0,
        "originality": 60.0,
        "site_relevance": 70.0,
    }

    unscored = by["Make 料金"]
    assert unscored.opportunity_score is None
    assert "search_demand" not in unscored.missing_components  # signal は存在する
    assert "competition_ease" in unscored.missing_components
    assert unscored.prerequisites[0].startswith("signals_incomplete:")

    # active な Make のみが match する。paused の "RPA" 案件は数えない。
    assert unscored.affiliate.level == "single"
    assert unscored.affiliate.providers == ("make",)
    assert unscored.affiliate.program_names == ("Make",)
    assert by["RPA おすすめ"].affiliate.level == "none"


def test_non_archived_in_flight_articles_block_and_archived_articles_do_not(
    session: Session, seeded: dict[str, Keyword]
) -> None:
    _seed_article(session, seeded["業務効率化 ツール おすすめ"], "drafting", "pillar-slug")
    _seed_article(session, seeded["RPA おすすめ"], "archived", "archived-rpa")
    session.commit()

    service, config = _service(session)
    by = _by_kw(service.build(config))

    assert by["業務効率化 ツール おすすめ"].reason_code == "already_has_article"
    assert by["業務効率化 ツール おすすめ"].existing_article_status == "drafting"
    assert by["業務効率化 ツール 比較"].reason_code == "overlaps_existing_article"
    assert by["業務効率化 ツール 比較"].overlaps_article_status == "drafting"
    # archived の RPA 記事は competitor ではない: RPA おすすめ は独立 slot のまま。
    assert by["RPA おすすめ"].decision == "ok"
    assert by["RPA 比較"].decision == "merge"


@pytest.mark.parametrize("status", ["idea", "planned", "drafting", "review"])
def test_pre_publication_statuses_count_as_cannibalization_competitors(
    session: Session, seeded: dict[str, Keyword], status: str
) -> None:
    _seed_article(session, seeded["RPA おすすめ"], status, f"rpa-{status}")
    session.commit()

    service, config = _service(session)
    by = _by_kw(service.build(config))
    assert by["RPA おすすめ"].reason_code == "already_has_article"
    assert by["RPA 比較"].reason_code == "overlaps_existing_article"
    assert by["RPA 比較"].overlaps_article_status == status


def test_article_fact_counts_are_read_for_existing_articles(
    session: Session, seeded: dict[str, Keyword]
) -> None:
    article = _seed_article(session, seeded["業務効率化 ツール おすすめ"], "published", "pillar")
    session.flush()
    facts = ArticleFactRepository(session)
    for key in ("official_url", "category"):
        facts.append(
            article_id=article.id,
            subject_ref="Make",
            affiliate_program_id=None,
            fact_key=key,
            fact_value="https://www.example.invalid/",
            value_status="verified",
            unknown_reason=None,
            source_id=None,
            checked_at=datetime(2026, 9, 1, tzinfo=UTC),
        )
    session.commit()

    service, config = _service(session)
    entry = _by_kw(service.build(config))["業務効率化 ツール おすすめ"]
    assert entry.fact_research.existing_fact_count == 2
    assert entry.fact_research.status == "present"


def test_unknown_keyword_in_config_is_rejected(session: Session, seeded) -> None:
    raw = {**_CONFIG, "clusters": [dict(_CONFIG["clusters"][1])]}
    raw["clusters"][0]["keywords"] = [
        {"keyword": "RPA おすすめ", "role": "pillar"},
        {"keyword": "存在しない keyword", "role": "supporting"},
    ]
    with pytest.raises(ClusterConfigError, match="unknown keyword"):
        ContentQueueService(session).build(parse_cluster_config(raw))


def test_keywords_outside_every_cluster_are_reported_unassigned(
    session: Session, seeded: dict[str, Keyword]
) -> None:
    service, config = _service(session)
    queue = service.build(config)
    assert [u.keyword for u in queue.unassigned] == ["ChatGPT 料金"]
    assert queue.unassigned[0].opportunity_score == 53.13


# ================================================================ read-only
def test_build_issues_only_select_statements_and_changes_no_rows(
    engine: Engine, session: Session, seeded: dict[str, Keyword]
) -> None:
    _seed_article(session, seeded["業務効率化 ツール おすすめ"], "review", "pillar")
    session.commit()
    before = _table_counts(session)

    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _spy(_conn, _cursor, statement, _params, _context, _executemany) -> None:
        statements.append(statement)

    commits: list[int] = []
    event.listen(session, "after_commit", lambda _s: commits.append(1))

    try:
        service, config = _service(session)
        service.build(config)
    finally:
        event.remove(engine, "before_cursor_execute", _spy)

    assert statements, "the service should have read from the DB"
    writes = [s for s in statements if s.lstrip().split(None, 1)[0].upper() != "SELECT"]
    assert writes == []
    assert commits == []
    assert not session.new and not session.dirty and not session.deleted
    assert _table_counts(session) == before


def test_read_only_guard_stops_any_pending_write(session: Session, seeded) -> None:
    class _Violating(ContentQueueService):
        def _load_inputs(self):
            self._session.add(Keyword(keyword="should never be written"))
            self._session.flush()
            return super()._load_inputs()

    with pytest.raises(ContentQueueReadOnlyViolationError):
        _Violating(session).build(parse_cluster_config(_CONFIG))
    session.rollback()
    assert (
        session.scalar(select(Keyword).where(Keyword.keyword == "should never be written")) is None
    )


def test_guard_listener_is_removed_after_build(session: Session, seeded) -> None:
    service, config = _service(session)
    service.build(config)
    # listener が残っていれば、通常の書き込み flush がここで失敗する。
    session.add(Keyword(keyword="normal write after build"))
    session.flush()
    session.rollback()


def test_build_makes_no_http_or_socket_calls(
    session: Session, seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a, **_kw):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(httpx.Client, "send", _boom)
    monkeypatch.setattr(httpx.AsyncClient, "send", _boom)
    monkeypatch.setattr(socket.socket, "connect", _boom)

    service, config = _service(session)
    assert service.build(config).summary["assigned_keywords"] == 6


def test_output_never_contains_tracking_urls_or_landing_pages(session: Session, seeded) -> None:
    service, config = _service(session)
    payload = str(service.build(config).to_dict())
    assert _TRACKING not in payload
    assert "example.invalid" not in payload
