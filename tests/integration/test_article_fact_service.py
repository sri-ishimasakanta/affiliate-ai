"""ArticleFactService の検証: validation / immutable history / idempotency。"""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.article.schemas import ArticleFactCreate
from app.exceptions import EntityNotFoundError, FactValidationError
from app.models import AffiliateProgram, Article, ArticleFact, Source
from app.models.enums import AffiliateProgramStatus
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.services.article_fact_service import ArticleFactService

NOW = datetime.now(UTC)
T1 = NOW - timedelta(days=10)
T2 = NOW - timedelta(days=2)


def _article(session: Session) -> Article:
    a = Article(title="t", slug="a", keyword_id=None)
    session.add(a)
    session.flush()
    session.commit()
    return a


def _source(session: Session, article_id: int, stype: str = "official_pricing") -> Source:
    s = Source(
        article_id=article_id, source_type=stype,
        source_url=f"https://example.test/{stype}", title="x", checked_at=T1,
    )
    session.add(s)
    session.flush()
    session.commit()
    return s


def _program(session: Session, name: str) -> AffiliateProgram:
    return AffiliateProgramRepository(session).create(
        name=name, provider="direct", status=AffiliateProgramStatus.ACTIVE
    )


def _fact(**over) -> ArticleFactCreate:
    base = dict(
        subject_ref="Make",
        affiliate_program_id=None,
        fact_key="key_features",
        fact_value=["a", "b"],
        value_status="verified",
        unknown_reason=None,
        source_id=None,
        checked_at=T1,
    )
    base.update(over)
    return ArticleFactCreate(**base)


# -- validation ----------------------------------------------------
def test_verified_without_value_rejected(session: Session) -> None:
    art = _article(session)
    src = _source(session, art.id)
    with pytest.raises(FactValidationError, match="requires fact_value"):
        ArticleFactService(session).create_fact(
            art.id, _fact(fact_value=None, source_id=src.id)
        )


def test_verified_without_source_rejected(session: Session) -> None:
    art = _article(session)
    with pytest.raises(FactValidationError, match="requires a source"):
        ArticleFactService(session).create_fact(art.id, _fact(source_id=None))


def test_verified_with_secondary_source_rejected(session: Session) -> None:
    art = _article(session)
    sec = _source(session, art.id, "secondary")
    with pytest.raises(FactValidationError, match="official_"):
        ArticleFactService(session).create_fact(art.id, _fact(source_id=sec.id))


def test_unknown_with_value_rejected(session: Session) -> None:
    art = _article(session)
    src = _source(session, art.id)
    with pytest.raises(FactValidationError, match="must not carry a value"):
        ArticleFactService(session).create_fact(
            art.id,
            _fact(value_status="unknown", fact_value=["x"], source_id=src.id,
                  unknown_reason="r", fact_key="ai_features"),
        )


def test_unknown_without_source_rejected(session: Session) -> None:
    art = _article(session)
    with pytest.raises(FactValidationError, match="requires a source"):
        ArticleFactService(session).create_fact(
            art.id,
            _fact(value_status="unknown", fact_value=None, unknown_reason="r",
                  fact_key="ai_features"),
        )


def test_unknown_without_reason_rejected(session: Session) -> None:
    art = _article(session)
    src = _source(session, art.id)
    with pytest.raises(FactValidationError, match="unknown_reason"):
        ArticleFactService(session).create_fact(
            art.id,
            _fact(value_status="unknown", fact_value=None, source_id=src.id,
                  unknown_reason="  ", fact_key="ai_features"),
        )


def test_not_applicable_with_value_rejected(session: Session) -> None:
    art = _article(session)
    with pytest.raises(FactValidationError, match="must not carry a value"):
        ArticleFactService(session).create_fact(
            art.id,
            _fact(value_status="not_applicable", fact_value=["x"], unknown_reason="n/a",
                  fact_key="ai_features"),
        )


def test_wrong_value_type_rejected(session: Session) -> None:
    art = _article(session)
    src = _source(session, art.id)
    with pytest.raises(FactValidationError, match="boolean"):
        ArticleFactService(session).create_fact(
            art.id,
            _fact(fact_key="free_plan_available", fact_value="yes", source_id=src.id),
        )
    with pytest.raises(FactValidationError, match="non-empty string"):
        ArticleFactService(session).create_fact(
            art.id,
            _fact(fact_key="official_product_name", fact_value=["Make"], source_id=src.id),
        )


def test_list_is_normalized_on_store(session: Session) -> None:
    art = _article(session)
    src = _source(session, art.id)
    read = ArticleFactService(session).create_fact(
        art.id,
        _fact(fact_value=["  b ", "a", "b", ""], source_id=src.id),
    )
    assert read.fact_value == ["b", "a"]


def test_future_checked_at_rejected(session: Session) -> None:
    art = _article(session)
    src = _source(session, art.id)
    with pytest.raises(FactValidationError, match="future"):
        ArticleFactService(session).create_fact(
            art.id, _fact(source_id=src.id, checked_at=NOW + timedelta(days=1))
        )


def test_cross_article_source_rejected(session: Session) -> None:
    a1 = _article(session)
    a2 = Article(title="t2", slug="a2", keyword_id=None)
    session.add(a2)
    session.flush()
    session.commit()
    s2 = _source(session, a2.id)
    with pytest.raises(FactValidationError, match="different Article"):
        ArticleFactService(session).create_fact(a1.id, _fact(source_id=s2.id))


def test_subject_ref_must_match_linked_program(session: Session) -> None:
    art = _article(session)
    src = _source(session, art.id)
    p = _program(session, "HubSpot")
    with pytest.raises(FactValidationError, match="must match the linked"):
        ArticleFactService(session).create_fact(
            art.id, _fact(subject_ref="Make", affiliate_program_id=p.id, source_id=src.id)
        )
    # 一致すれば OK
    ok = ArticleFactService(session).create_fact(
        art.id, _fact(subject_ref="HubSpot", affiliate_program_id=p.id, source_id=src.id)
    )
    assert ok.affiliate_program_id == p.id


def test_unlinked_subject_allowed(session: Session) -> None:
    art = _article(session)
    src = _source(session, art.id)
    read = ArticleFactService(session).create_fact(
        art.id, _fact(subject_ref="SomeFreeTool", affiliate_program_id=None, source_id=src.id)
    )
    assert read.affiliate_program_id is None


def test_article_not_found(session: Session) -> None:
    with pytest.raises(EntityNotFoundError):
        ArticleFactService(session).create_fact(999, _fact())


# -- immutable history --------------------------------------------
def test_append_history_and_latest(session: Session) -> None:
    art = _article(session)
    src = _source(session, art.id)
    svc = ArticleFactService(session)
    f1 = svc.create_fact(art.id, _fact(fact_value=["a", "b"], source_id=src.id, checked_at=T1))
    f2 = svc.create_fact(art.id, _fact(fact_value=["a", "b", "c"], source_id=src.id, checked_at=T2))

    hist = svc.list_facts(art.id, subject_ref="Make", fact_key="key_features")
    assert [h.id for h in hist] == [f1.id, f2.id]
    latest = svc.list_facts(art.id, subject_ref="Make", fact_key="key_features", latest=True)
    assert [x.id for x in latest] == [f2.id]
    # old fact は変更されない
    session.expire_all()
    assert session.get(ArticleFact, f1.id).fact_value == ["a", "b"]


def test_same_checked_at_latest_is_higher_id(session: Session) -> None:
    art = _article(session)
    src = _source(session, art.id)
    svc = ArticleFactService(session)
    svc.create_fact(
        art.id,
        _fact(fact_key="category", fact_value="A", source_id=src.id, checked_at=T1),
    )
    b = svc.create_fact(
        art.id,
        _fact(fact_key="category", fact_value="B", source_id=src.id, checked_at=T1),
    )
    latest = svc.list_facts(art.id, subject_ref="Make", fact_key="category", latest=True)
    assert latest[0].id == b.id and latest[0].fact_value == "B"


def test_exact_duplicate_is_skipped(session: Session) -> None:
    art = _article(session)
    src = _source(session, art.id)
    svc = ArticleFactService(session)
    a = svc.create_fact(art.id, _fact(source_id=src.id))
    b = svc.create_fact(art.id, _fact(source_id=src.id))  # 完全一致
    assert a.id == b.id
    assert session.scalar(select(func.count()).select_from(ArticleFact)) == 1


# -- timezone semantics (Phase 3B-3B.1) --------------------------
_JST = timezone(timedelta(hours=9))


def test_checked_at_stored_as_utc_instant_roundtrip(session: Session) -> None:
    art = _article(session)
    src = _source(session, art.id)
    jst_input = datetime(2026, 8, 28, 14, 12, tzinfo=_JST)  # = 05:12 UTC
    read = ArticleFactService(session).create_fact(
        art.id, _fact(fact_key="category", fact_value="A", source_id=src.id,
                      checked_at=jst_input)
    )
    session.expire_all()
    stored = session.get(ArticleFact, read.id).checked_at
    # SQLite は naive を返す。UTC として解釈すると同一 instant になること。
    interpreted = stored if stored.tzinfo else stored.replace(tzinfo=UTC)
    assert interpreted == jst_input.astimezone(UTC)
    assert interpreted == datetime(2026, 8, 28, 5, 12, tzinfo=UTC)


def test_future_checked_at_rejected_across_offsets(session: Session) -> None:
    art = _article(session)
    src = _source(session, art.id)
    # -05:00 表記でも UTC instant で未来判定される
    future_est = datetime.now(UTC).astimezone(timezone(timedelta(hours=-5))) + timedelta(hours=2)
    with pytest.raises(FactValidationError, match="future"):
        ArticleFactService(session).create_fact(
            art.id, _fact(fact_key="category", fact_value="A", source_id=src.id,
                          checked_at=future_est)
        )


def test_past_jst_checked_at_accepted(session: Session) -> None:
    art = _article(session)
    src = _source(session, art.id)
    past_jst = datetime.now(UTC).astimezone(_JST) - timedelta(days=1)
    read = ArticleFactService(session).create_fact(
        art.id, _fact(fact_key="category", fact_value="A", source_id=src.id,
                      checked_at=past_jst)
    )
    assert read.id is not None


def test_latest_ordering_uses_utc_instant(session: Session) -> None:
    art = _article(session)
    src = _source(session, art.id)
    svc = ArticleFactService(session)
    # JST 23:00 (= 14:00 UTC) を先に、UTC 15:00 を後に投入。
    # wall-clock だけ見ると JST 23:00 が「新しそう」に見えるが instant は 15:00 UTC が新しい。
    older = svc.create_fact(
        art.id, _fact(fact_key="category", fact_value="JST2300", source_id=src.id,
                      checked_at=datetime(2026, 8, 20, 23, 0, tzinfo=_JST))
    )
    newer = svc.create_fact(
        art.id, _fact(fact_key="category", fact_value="UTC1500", source_id=src.id,
                      checked_at=datetime(2026, 8, 20, 15, 0, tzinfo=UTC))
    )
    latest = svc.list_facts(art.id, subject_ref="Make", fact_key="category", latest=True)
    assert len(latest) == 1
    assert latest[0].id == newer.id and latest[0].fact_value == "UTC1500"
    assert older.id != newer.id
