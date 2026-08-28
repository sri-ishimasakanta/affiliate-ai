"""KeywordSignal / KeywordScoreSignal のモデル・Repository を検証する。"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Keyword, KeywordScore, KeywordScoreSignal, KeywordSignal
from app.models.enums import KeywordSignalComponent
from app.repositories.keyword_score_signal_repository import KeywordScoreSignalRepository
from app.repositories.keyword_signal_repository import KeywordSignalRepository

_OBSERVED = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _make_keyword(session: Session, keyword: str = "kw") -> Keyword:
    entity = Keyword(keyword=keyword)
    session.add(entity)
    session.flush()
    return entity


def _create_signal(
    repo: KeywordSignalRepository,
    keyword_id: int,
    *,
    component: KeywordSignalComponent = KeywordSignalComponent.TREND,
    normalized_value: float = 50.0,
    provider: str = "manual",
    raw_data: object = None,
    observed_at: datetime = _OBSERVED,
) -> KeywordSignal:
    return repo.create(
        keyword_id=keyword_id,
        component=component,
        normalized_value=normalized_value,
        provider=provider,
        observed_at=observed_at,
        raw_data=raw_data,
    )


def test_create_flushes_but_does_not_commit(session: Session) -> None:
    keyword = _make_keyword(session)
    repo = KeywordSignalRepository(session)

    created = _create_signal(repo, keyword.id)

    assert created.id is not None
    assert created.component == "trend"

    session.rollback()
    assert session.scalars(select(KeywordSignal)).all() == []


def test_raw_data_json_roundtrip(session: Session) -> None:
    keyword = _make_keyword(session)
    repo = KeywordSignalRepository(session)
    payload = {
        "avg_monthly_searches": 1200,
        "monthly_search_volumes": [100, 110, 90],
        "competition": "LOW",
        "nested": {"a": 1, "b": [True, None, "x"]},
    }

    created = _create_signal(repo, keyword.id, raw_data=payload)
    session.commit()
    session.expire_all()

    reloaded = repo.get_by_id(created.id)
    assert reloaded is not None
    assert reloaded.raw_data == payload


def test_get_latest_is_per_component(session: Session) -> None:
    keyword = _make_keyword(session)
    repo = KeywordSignalRepository(session)
    _create_signal(repo, keyword.id, component=KeywordSignalComponent.TREND, normalized_value=10)
    _create_signal(repo, keyword.id, component=KeywordSignalComponent.TREND, normalized_value=20)
    newest_trend = _create_signal(
        repo, keyword.id, component=KeywordSignalComponent.TREND, normalized_value=30
    )
    demand = _create_signal(
        repo, keyword.id, component=KeywordSignalComponent.SEARCH_DEMAND, normalized_value=77
    )

    assert repo.get_latest(keyword.id, KeywordSignalComponent.TREND).id == newest_trend.id
    assert repo.get_latest(keyword.id, KeywordSignalComponent.SEARCH_DEMAND).id == demand.id
    assert repo.get_latest(keyword.id, KeywordSignalComponent.ORIGINALITY) is None


def test_list_by_component_and_by_keyword_newest_first(session: Session) -> None:
    keyword = _make_keyword(session)
    repo = KeywordSignalRepository(session)
    trend = [
        _create_signal(repo, keyword.id, component=KeywordSignalComponent.TREND)
        for _ in range(3)
    ]
    _create_signal(repo, keyword.id, component=KeywordSignalComponent.ORIGINALITY)

    by_component = repo.list_by_component(keyword.id, KeywordSignalComponent.TREND)
    assert [s.id for s in by_component] == [trend[2].id, trend[1].id, trend[0].id]

    all_signals = repo.list_by_keyword(keyword.id)
    assert len(all_signals) == 4
    assert all_signals[0].id == max(s.id for s in all_signals)


def test_list_pagination(session: Session) -> None:
    keyword = _make_keyword(session)
    repo = KeywordSignalRepository(session)
    created = [_create_signal(repo, keyword.id) for _ in range(5)]

    page = repo.list_by_keyword(keyword.id, limit=2, offset=1)
    assert [s.id for s in page] == [created[3].id, created[2].id]


# -- latest = observed_at DESC, id DESC (Phase 2B-2 regression) ---------
def test_latest_uses_observed_at_not_insertion_order(session: Session) -> None:
    keyword = _make_keyword(session)
    repo = KeywordSignalRepository(session)

    _create_signal(
        repo, keyword.id, normalized_value=10,
        observed_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    newest = _create_signal(
        repo, keyword.id, normalized_value=20,
        observed_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    # 後から挿入するが観測日時は古い (バックフィル)
    _create_signal(
        repo, keyword.id, normalized_value=30,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    latest = repo.get_latest(keyword.id, KeywordSignalComponent.TREND)
    assert latest is not None
    assert latest.id == newest.id
    assert latest.normalized_value == 20


def test_latest_breaks_tie_with_id_desc(session: Session) -> None:
    keyword = _make_keyword(session)
    repo = KeywordSignalRepository(session)
    same = datetime(2026, 7, 15, tzinfo=UTC)

    _create_signal(repo, keyword.id, observed_at=same)
    _create_signal(repo, keyword.id, observed_at=same)
    highest_id = _create_signal(repo, keyword.id, observed_at=same)

    latest = repo.get_latest(keyword.id, KeywordSignalComponent.TREND)
    assert latest.id == highest_id.id


def test_list_order_is_observed_at_desc_then_id_desc(session: Session) -> None:
    keyword = _make_keyword(session)
    repo = KeywordSignalRepository(session)
    same = datetime(2026, 7, 1, tzinfo=UTC)

    a_old = _create_signal(repo, keyword.id, observed_at=datetime(2026, 5, 1, tzinfo=UTC))
    b_tie = _create_signal(repo, keyword.id, observed_at=same)
    c_tie = _create_signal(repo, keyword.id, observed_at=same)
    d_new = _create_signal(repo, keyword.id, observed_at=datetime(2026, 9, 1, tzinfo=UTC))

    ordered = repo.list_by_keyword(keyword.id)
    assert [s.id for s in ordered] == [d_new.id, c_tie.id, b_tie.id, a_old.id]

    by_component = repo.list_by_component(keyword.id, KeywordSignalComponent.TREND)
    assert [s.id for s in by_component] == [d_new.id, c_tie.id, b_tie.id, a_old.id]


def test_backfill_pagination_still_ordered_by_observed_at(session: Session) -> None:
    keyword = _make_keyword(session)
    repo = KeywordSignalRepository(session)
    # 観測日時 2026-01..2026-05、挿入順はシャッフル
    for month in (3, 1, 5, 2, 4):
        _create_signal(
            repo, keyword.id, normalized_value=float(month),
            observed_at=datetime(2026, month, 1, tzinfo=UTC),
        )

    page = repo.list_by_keyword(keyword.id, limit=2, offset=1)
    # newest first: month 5, 4, 3, 2, 1 -> offset 1 -> [4, 3]
    assert [s.normalized_value for s in page] == [4.0, 3.0]


@pytest.mark.parametrize("bad_value", [-0.5, 100.5])
def test_normalized_value_db_check_constraint(session: Session, bad_value: float) -> None:
    keyword = _make_keyword(session)
    repo = KeywordSignalRepository(session)

    with pytest.raises(IntegrityError):
        _create_signal(repo, keyword.id, normalized_value=bad_value)

    session.rollback()


def test_deleting_keyword_cascades_to_signals(session: Session) -> None:
    keyword = _make_keyword(session)
    repo = KeywordSignalRepository(session)
    _create_signal(repo, keyword.id)
    _create_signal(repo, keyword.id, component=KeywordSignalComponent.ORIGINALITY)
    session.commit()

    session.delete(keyword)
    session.commit()

    assert session.scalars(select(KeywordSignal)).all() == []


def test_keyword_score_signal_association(session: Session) -> None:
    keyword = _make_keyword(session)
    signal_repo = KeywordSignalRepository(session)
    signal = _create_signal(signal_repo, keyword.id)
    score = KeywordScore(
        keyword_id=keyword.id,
        search_demand=1,
        commercial_intent=1,
        affiliate_opportunity=1,
        competition_ease=1,
        trend=1,
        originality=1,
        site_relevance=1,
        total_score=1.0,
        score_version="v1",
        input_source="signals",
    )
    session.add(score)
    session.flush()

    link_repo = KeywordScoreSignalRepository(session)
    link_repo.create(keyword_score_id=score.id, keyword_signal_id=signal.id)
    session.commit()

    linked = link_repo.list_signals_for_score(score.id)
    assert [s.id for s in linked] == [signal.id]


def test_duplicate_association_is_rejected(session: Session) -> None:
    keyword = _make_keyword(session)
    signal = _create_signal(KeywordSignalRepository(session), keyword.id)
    score = KeywordScore(
        keyword_id=keyword.id,
        search_demand=1,
        commercial_intent=1,
        affiliate_opportunity=1,
        competition_ease=1,
        trend=1,
        originality=1,
        site_relevance=1,
        total_score=1.0,
        score_version="v1",
        input_source="signals",
    )
    session.add(score)
    session.flush()
    link_repo = KeywordScoreSignalRepository(session)
    link_repo.create(keyword_score_id=score.id, keyword_signal_id=signal.id)
    session.commit()

    # flush 時点で UNIQUE 制約違反となる
    with pytest.raises(IntegrityError):
        link_repo.create(keyword_score_id=score.id, keyword_signal_id=signal.id)

    session.rollback()


def test_signal_history_has_no_updated_at() -> None:
    columns = set(inspect(KeywordSignal).columns.keys())
    assert "created_at" in columns
    assert "updated_at" not in columns
    link_columns = set(inspect(KeywordScoreSignal).columns.keys())
    assert "updated_at" not in link_columns
