"""originality 用 read-only projection (Keyword / Article repository) の検証。"""

from sqlalchemy.orm import Session

from app.models import Article, Keyword
from app.repositories.article_repository import ArticleRepository
from app.repositories.keyword_repository import KeywordRepository

_KW_STATUSES = ("analyzed", "selected", "assigned")
_ART_STATUSES = ("approved", "published", "rewrite")


def _kw(session: Session, text: str, status: str) -> Keyword:
    entity = Keyword(keyword=text)
    entity.status = status
    session.add(entity)
    session.flush()
    return entity


def _art(
    session: Session, *, slug: str, title: str, status: str, keyword_id: int | None
) -> Article:
    entity = Article(title=title, slug=slug, keyword_id=keyword_id)
    entity.status = status
    session.add(entity)
    session.flush()
    return entity


# -- Keyword projection --------------------------------------------
def test_keyword_status_filter_and_self_exclusion(session: Session) -> None:
    current = _kw(session, "current kw", "analyzed")
    _kw(session, "included analyzed", "analyzed")
    _kw(session, "included selected", "selected")
    _kw(session, "included assigned", "assigned")
    _kw(session, "excluded discovered", "discovered")
    _kw(session, "excluded rejected", "rejected")
    session.commit()

    rows = KeywordRepository(session).list_originality_candidates(
        exclude_id=current.id, statuses=_KW_STATUSES
    )
    texts = {text for _id, text in rows}
    assert texts == {"included analyzed", "included selected", "included assigned"}
    assert all(isinstance(_id, int) and isinstance(text, str) for _id, text in rows)
    assert current.id not in {r[0] for r in rows}


def test_keyword_count(session: Session) -> None:
    _kw(session, "a", "analyzed")
    _kw(session, "b", "discovered")
    session.commit()
    assert KeywordRepository(session).count() == 2


# -- Article projection --------------------------------------------
def test_article_status_filter(session: Session) -> None:
    kw = _kw(session, "topic kw", "selected")
    session.commit()
    for i, st in enumerate(
        ["idea", "planned", "drafting", "review", "approved", "published", "rewrite", "archived"]
    ):
        _art(session, slug=f"s{i}", title=f"title {st}", status=st, keyword_id=None)
    session.commit()

    rows = ArticleRepository(session).list_originality_candidates(
        exclude_keyword_id=kw.id, statuses=_ART_STATUSES
    )
    titles = {title for _aid, _kid, _kt, title in rows}
    assert titles == {"title approved", "title published", "title rewrite"}


def test_article_excludes_self_keyword_and_joins_linked_keyword(session: Session) -> None:
    current = _kw(session, "current kw", "analyzed")
    other = _kw(session, "other kw", "selected")
    session.commit()

    _art(session, slug="self", title="self article", status="published", keyword_id=current.id)
    _art(session, slug="other", title="other article", status="published", keyword_id=other.id)
    _art(session, slug="orphan", title="orphan article", status="approved", keyword_id=None)
    session.commit()

    rows = ArticleRepository(session).list_originality_candidates(
        exclude_keyword_id=current.id, statuses=_ART_STATUSES
    )
    by_title = {title: (kid, ktext) for _aid, kid, ktext, title in rows}
    assert set(by_title) == {"other article", "orphan article"}  # self 除外
    assert by_title["other article"] == (other.id, "other kw")   # JOIN で keyword text
    assert by_title["orphan article"] == (None, None)            # 未リンクは None


def test_article_count_total_and_by_keyword(session: Session) -> None:
    kw = _kw(session, "k", "analyzed")
    session.commit()
    _art(session, slug="a1", title="t1", status="published", keyword_id=kw.id)
    _art(session, slug="a2", title="t2", status="idea", keyword_id=kw.id)
    _art(session, slug="a3", title="t3", status="published", keyword_id=None)
    session.commit()

    repo = ArticleRepository(session)
    assert repo.count() == 3
    assert repo.count(keyword_id=kw.id) == 2
    assert repo.count(keyword_id=999999) == 0
