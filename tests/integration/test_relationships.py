from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    AffiliateProgram,
    Article,
    ArticleAffiliateProgram,
    Source,
)


def _make_article(session: Session, slug: str = "article") -> Article:
    article = Article(title="タイトル", slug=slug)
    session.add(article)
    session.flush()
    return article


def test_article_has_many_sources(session: Session) -> None:
    article = _make_article(session)
    article.sources.append(
        Source(source_type="news", source_url="https://example.com/a", title="記事A")
    )
    article.sources.append(
        Source(
            source_type="research",
            source_url="https://example.com/b",
            title="論文B",
            checked_at=datetime(2026, 8, 27, tzinfo=UTC),
        )
    )
    session.commit()

    stored = session.scalars(select(Article)).one()
    assert len(stored.sources) == 2
    assert {s.source_type for s in stored.sources} == {"news", "research"}
    assert all(s.article_id == stored.id for s in stored.sources)


def test_deleting_article_cascades_to_sources(session: Session) -> None:
    article = _make_article(session)
    article.sources.append(Source(source_type="news", source_url="https://example.com/a"))
    session.commit()

    session.delete(article)
    session.commit()

    assert session.scalar(select(func.count()).select_from(Source)) == 0


def test_article_and_affiliate_program_are_many_to_many(session: Session) -> None:
    article_a = _make_article(session, slug="a")
    article_b = _make_article(session, slug="b")
    program_x = AffiliateProgram(name="X")
    program_y = AffiliateProgram(name="Y")
    session.add_all([program_x, program_y])
    session.flush()

    session.add_all(
        [
            ArticleAffiliateProgram(
                article=article_a, affiliate_program=program_x, is_primary=True
            ),
            ArticleAffiliateProgram(article=article_a, affiliate_program=program_y),
            ArticleAffiliateProgram(article=article_b, affiliate_program=program_x),
        ]
    )
    session.commit()

    session.expire_all()

    assert {p.name for p in article_a.affiliate_programs} == {"X", "Y"}
    assert {a.slug for a in program_x.articles} == {"a", "b"}

    primary_links = [link for link in article_a.affiliate_program_links if link.is_primary]
    assert len(primary_links) == 1
    assert primary_links[0].affiliate_program.name == "X"


def test_duplicate_article_program_pair_is_rejected(session: Session) -> None:
    article = _make_article(session)
    program = AffiliateProgram(name="X")
    session.add(program)
    session.flush()

    session.add(ArticleAffiliateProgram(article=article, affiliate_program=program))
    session.commit()

    session.add(ArticleAffiliateProgram(article=article, affiliate_program=program))
    with pytest.raises(IntegrityError):
        session.commit()


def test_deleting_article_cascades_to_association_rows(session: Session) -> None:
    article = _make_article(session)
    program = AffiliateProgram(name="X")
    session.add(program)
    session.flush()
    session.add(ArticleAffiliateProgram(article=article, affiliate_program=program))
    session.commit()

    session.delete(article)
    session.commit()

    assert session.scalar(select(func.count()).select_from(ArticleAffiliateProgram)) == 0
    # プログラム自体は残る
    assert session.scalar(select(func.count()).select_from(AffiliateProgram)) == 1
