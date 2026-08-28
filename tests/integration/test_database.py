from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config.database import build_engine, check_database_connection
from app.models import (
    AffiliateProgram,
    Article,
    ArticleAffiliateProgram,
    ArticleMetric,
    ArticleStatus,
    Keyword,
    Source,
)


def test_check_database_connection() -> None:
    test_engine = build_engine("sqlite:///:memory:")

    try:
        assert check_database_connection(test_engine) is True
    finally:
        test_engine.dispose()


def test_persist_and_query_full_graph(session: Session) -> None:
    program = AffiliateProgram(
        name="サンプル案件",
        provider="a8",
        commission_type="percentage",
        commission_value=3.5,
    )
    keyword = Keyword(
        keyword="ふるさと納税 おすすめ",
        search_volume=12000,
        difficulty=42.0,
        intent="commercial",
    )
    article = Article(
        title="ふるさと納税おすすめ返礼品10選",
        slug="furusato-nozei-osusume",
        keyword=keyword,
    )
    article.sources.append(
        Source(
            source_type="official",
            source_url="https://www.soumu.go.jp/",
            title="総務省 ふるさと納税ポータル",
        )
    )
    article.affiliate_program_links.append(
        ArticleAffiliateProgram(affiliate_program=program, is_primary=True)
    )
    article.metrics.append(
        ArticleMetric(
            metric_date=date(2026, 8, 27),
            provider="search_console",
            impressions=1000,
            clicks=80,
            average_position=8.4,
        )
    )

    session.add(article)
    session.commit()

    stored = session.scalars(select(Article)).one()

    assert stored.id is not None
    assert stored.created_at is not None
    assert stored.status == ArticleStatus.IDEA
    assert stored.keyword.keyword == "ふるさと納税 おすすめ"
    assert len(stored.sources) == 1
    assert stored.sources[0].source_type == "official"
    assert len(stored.affiliate_program_links) == 1
    assert stored.affiliate_program_links[0].is_primary is True
    assert stored.affiliate_programs[0].provider == "a8"
    assert len(stored.metrics) == 1
    assert stored.metrics[0].clicks == 80


def test_metric_unique_constraint(session: Session) -> None:
    article = Article(title="記事", slug="kiji")
    session.add(article)
    session.commit()

    session.add(
        ArticleMetric(article_id=article.id, metric_date=date(2026, 8, 27), provider="ga4")
    )
    session.commit()

    session.add(
        ArticleMetric(article_id=article.id, metric_date=date(2026, 8, 27), provider="ga4")
    )

    with pytest.raises(IntegrityError):
        session.commit()
