"""KeywordScoreRepository の責務 (DB アクセスのみ / commit しない) を検証する。"""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Keyword, KeywordScore
from app.repositories.keyword_score_repository import KeywordScoreRepository

_COMPONENTS = {
    "search_demand": 10.0,
    "commercial_intent": 20.0,
    "affiliate_opportunity": 30.0,
    "competition_ease": 40.0,
    "trend": 50.0,
    "originality": 60.0,
    "site_relevance": 70.0,
}


def _make_keyword(session: Session, keyword: str = "kw") -> Keyword:
    entity = Keyword(keyword=keyword)
    session.add(entity)
    session.flush()
    return entity


def _create_score(
    repo: KeywordScoreRepository,
    keyword_id: int,
    *,
    total: float = 33.0,
    source: str = "manual",
    **component_overrides: float,
) -> KeywordScore:
    return repo.create(
        keyword_id=keyword_id,
        total_score=total,
        score_version="v1",
        input_source=source,
        **{**_COMPONENTS, **component_overrides},
    )


def test_create_flushes_but_does_not_commit(session: Session) -> None:
    keyword = _make_keyword(session)
    repo = KeywordScoreRepository(session)

    created = _create_score(repo, keyword.id)

    assert created.id is not None
    assert created.score_version == "v1"

    session.rollback()
    assert session.scalars(select(KeywordScore)).all() == []


def test_get_latest_returns_most_recent(session: Session) -> None:
    keyword = _make_keyword(session)
    repo = KeywordScoreRepository(session)

    _create_score(repo, keyword.id, total=10.0)
    _create_score(repo, keyword.id, total=20.0)
    newest = _create_score(repo, keyword.id, total=30.0)

    latest = repo.get_latest(keyword.id)
    assert latest is not None
    assert latest.id == newest.id
    assert latest.total_score == 30.0


def test_get_latest_returns_none_without_scores(session: Session) -> None:
    keyword = _make_keyword(session)
    repo = KeywordScoreRepository(session)

    assert repo.get_latest(keyword.id) is None


def test_list_by_keyword_pagination_newest_first(session: Session) -> None:
    keyword = _make_keyword(session)
    repo = KeywordScoreRepository(session)
    created = [_create_score(repo, keyword.id, total=float(index)) for index in range(5)]

    page = repo.list_by_keyword(keyword.id, limit=2, offset=1)

    # newest first: created[4], created[3], created[2] ... offset=1 -> [3], [2]
    assert [row.id for row in page] == [created[3].id, created[2].id]


def test_list_by_keyword_scopes_to_keyword(session: Session) -> None:
    kw_a = _make_keyword(session, "a")
    kw_b = _make_keyword(session, "b")
    repo = KeywordScoreRepository(session)
    _create_score(repo, kw_a.id)
    _create_score(repo, kw_a.id)
    _create_score(repo, kw_b.id)

    assert len(repo.list_by_keyword(kw_a.id)) == 2
    assert len(repo.list_by_keyword(kw_b.id)) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"search_demand": 100.01},
        {"trend": -0.5},
        {"site_relevance": -1},
        {"total": 100.5},
        {"total": -1.0},
    ],
)
def test_db_check_constraint_rejects_out_of_range(
    session: Session, kwargs: dict[str, float]
) -> None:
    keyword = _make_keyword(session)
    repo = KeywordScoreRepository(session)

    with pytest.raises(IntegrityError):
        _create_score(repo, keyword.id, **kwargs)

    session.rollback()


def test_deleting_keyword_cascades_to_scores(session: Session) -> None:
    keyword = _make_keyword(session)
    repo = KeywordScoreRepository(session)
    _create_score(repo, keyword.id)
    _create_score(repo, keyword.id)
    session.commit()

    session.delete(keyword)
    session.commit()

    assert session.scalars(select(KeywordScore)).all() == []
