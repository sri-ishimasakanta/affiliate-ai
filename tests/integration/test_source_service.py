"""SourceService の検証 (in-memory DB)。"""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.article.schemas import SourceCreate
from app.exceptions import (
    DuplicateEntityError,
    EntityInUseError,
    EntityNotFoundError,
    FactValidationError,
)
from app.models import Article, ArticleFact, Source
from app.repositories.article_fact_repository import ArticleFactRepository
from app.services.source_service import SourceService

NOW = datetime.now(UTC)


def _article(session: Session, slug: str = "a") -> Article:
    entity = Article(title="t", slug=slug, keyword_id=None)
    session.add(entity)
    session.flush()
    session.commit()
    return entity


def _payload(**over) -> SourceCreate:
    base = dict(
        source_type="official_pricing",
        source_url="https://www.make.com/en/pricing",
        title="Make Pricing",
        checked_at=NOW - timedelta(days=1),
    )
    base.update(over)
    return SourceCreate(**base)


def test_create_and_list(session: Session) -> None:
    art = _article(session)
    svc = SourceService(session)
    read = svc.create(art.id, _payload())
    assert read.source_type == "official_pricing"
    assert read.source_url == "https://www.make.com/en/pricing"
    assert [s.id for s in svc.list_by_article(art.id)] == [read.id]


def test_create_canonicalizes_and_strips_tracking(session: Session) -> None:
    art = _article(session)
    read = SourceService(session).create(
        art.id,
        _payload(source_url="https://www.make.com/en/pricing?utm_source=x&plan=team"),
    )
    assert read.source_url == "https://www.make.com/en/pricing?plan=team"


def test_create_rejects_unsafe_url(session: Session) -> None:
    art = _article(session)
    with pytest.raises(FactValidationError):
        SourceService(session).create(
            art.id, _payload(source_url="http://www.make.com/pricing")
        )
    with pytest.raises(FactValidationError):
        SourceService(session).create(
            art.id, _payload(source_url="https://make.com/?token=SECRET")
        )


def test_create_rejects_naive_and_future_checked_at(session: Session) -> None:
    art = _article(session)
    with pytest.raises(FactValidationError):
        SourceService(session).create(
            art.id, _payload(checked_at=datetime(2026, 1, 1))  # naive
        )
    with pytest.raises(FactValidationError):
        SourceService(session).create(
            art.id, _payload(checked_at=NOW + timedelta(days=2))
        )


def test_same_observation_is_duplicate(session: Session) -> None:
    art = _article(session)
    svc = SourceService(session)
    svc.create(art.id, _payload())
    with pytest.raises(DuplicateEntityError):
        svc.create(art.id, _payload())
    # 別 checked_at なら別 observation として許可
    svc.create(art.id, _payload(checked_at=NOW - timedelta(days=2)))
    assert len(svc.list_by_article(art.id)) == 2


def test_article_not_found(session: Session) -> None:
    with pytest.raises(EntityNotFoundError):
        SourceService(session).create(999, _payload())


def test_delete_standalone_source(session: Session) -> None:
    art = _article(session)
    svc = SourceService(session)
    read = svc.create(art.id, _payload())
    svc.delete(art.id, read.id)
    assert svc.list_by_article(art.id) == []


def test_delete_rejected_when_referenced_by_fact(session: Session) -> None:
    art = _article(session)
    svc = SourceService(session)
    src = svc.create(art.id, _payload())
    ArticleFactRepository(session).append(
        article_id=art.id, subject_ref="Make", affiliate_program_id=None,
        fact_key="official_url", fact_value="https://www.make.com/",
        value_status="verified", unknown_reason=None, source_id=src.id,
        checked_at=NOW - timedelta(days=1),
    )
    session.commit()
    with pytest.raises(EntityInUseError):
        svc.delete(art.id, src.id)
    assert SourceService(session).get(art.id, src.id).id == src.id  # 残っている


def test_deleting_article_cascades_source_and_fact(session: Session) -> None:
    art = _article(session)
    src = SourceService(session).create(art.id, _payload())
    ArticleFactRepository(session).append(
        article_id=art.id, subject_ref="Make", affiliate_program_id=None,
        fact_key="official_url", fact_value="https://www.make.com/",
        value_status="verified", unknown_reason=None, source_id=src.id,
        checked_at=NOW - timedelta(days=1),
    )
    session.commit()
    session.delete(session.get(Article, art.id))
    session.commit()
    assert session.scalar(select(func.count()).select_from(Source)) == 0
    assert session.scalar(select(func.count()).select_from(ArticleFact)) == 0


def test_cross_article_source_not_listed(session: Session) -> None:
    a1, a2 = _article(session, "a1"), _article(session, "a2")
    s1 = SourceService(session).create(a1.id, _payload())
    with pytest.raises(EntityNotFoundError):
        SourceService(session).get(a2.id, s1.id)


def test_source_checked_at_stored_as_utc_instant(session: Session) -> None:
    art = _article(session)
    jst = timezone(timedelta(hours=9))
    jst_input = datetime(2026, 8, 28, 14, 12, tzinfo=jst)  # = 05:12 UTC
    read = SourceService(session).create(art.id, _payload(checked_at=jst_input))
    session.expire_all()
    stored = session.get(Source, read.id).checked_at
    interpreted = stored if stored.tzinfo else stored.replace(tzinfo=UTC)
    assert interpreted == jst_input.astimezone(UTC)
    assert interpreted == datetime(2026, 8, 28, 5, 12, tzinfo=UTC)


def test_source_same_observation_matches_after_utc_normalization(session: Session) -> None:
    art = _article(session)
    jst = timezone(timedelta(hours=9))
    svc = SourceService(session)
    svc.create(art.id, _payload(checked_at=datetime(2026, 8, 28, 14, 12, tzinfo=jst)))
    # 同一 instant を UTC 表記で渡しても重複扱い
    with pytest.raises(DuplicateEntityError):
        svc.create(art.id, _payload(checked_at=datetime(2026, 8, 28, 5, 12, tzinfo=UTC)))
