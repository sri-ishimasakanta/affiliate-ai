"""KeywordScoringService.score_keyword_from_latest_signals の検証。"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.exceptions import EntityNotFoundError, IncompleteSignalSetError
from app.keyword.schemas import KeywordScoreCreate
from app.keyword.scoring import COMPONENT_NAMES
from app.models import Keyword, KeywordScore, KeywordScoreSignal
from app.models.enums import KeywordStatus
from app.repositories.keyword_repository import KeywordRepository
from app.repositories.keyword_signal_repository import KeywordSignalRepository
from app.services.keyword_scoring_service import KeywordScoringService

_OBSERVED = datetime(2026, 8, 1, tzinfo=UTC)

# total_score = 82.25 になる既知の component 値
_KNOWN_VALUES = {
    "search_demand": 75.0,
    "commercial_intent": 95.0,
    "affiliate_opportunity": 90.0,
    "competition_ease": 55.0,
    "trend": 90.0,
    "originality": 80.0,
    "site_relevance": 100.0,
}
_EXPECTED_TOTAL = 82.25


def _make_keyword(session: Session, *, status: KeywordStatus | None = None) -> Keyword:
    entity = Keyword(keyword="kw")
    if status is not None:
        entity.status = status
    session.add(entity)
    session.flush()
    session.commit()
    return entity


def _seed_signals(
    session: Session,
    keyword_id: int,
    values: dict[str, float],
    *,
    provider: str = "manual",
) -> dict[str, int]:
    repo = KeywordSignalRepository(session)
    signal_ids: dict[str, int] = {}
    for component, value in values.items():
        signal = repo.create(
            keyword_id=keyword_id,
            component=component,
            normalized_value=value,
            provider=provider,
            observed_at=_OBSERVED,
        )
        signal_ids[component] = signal.id
    session.commit()
    return signal_ids


def test_score_from_full_signal_set(session: Session) -> None:
    keyword = _make_keyword(session)
    signal_ids = _seed_signals(session, keyword.id, _KNOWN_VALUES)
    service = KeywordScoringService(session)

    read = service.score_keyword_from_latest_signals(keyword.id)

    assert read.total_score == _EXPECTED_TOTAL
    assert read.score_version == "v1"
    assert read.input_source == "signals"
    assert read.search_demand == 75.0
    assert read.site_relevance == 100.0

    refreshed = KeywordRepository(session).get_by_id(keyword.id)
    assert refreshed.opportunity_score == _EXPECTED_TOTAL
    assert refreshed.status == KeywordStatus.ANALYZED

    # provenance: 7 件、正しい signal id
    links = session.scalars(
        select(KeywordScoreSignal).where(KeywordScoreSignal.keyword_score_id == read.id)
    ).all()
    assert {link.keyword_signal_id for link in links} == set(signal_ids.values())
    assert len(links) == 7


@pytest.mark.parametrize(
    "status",
    [
        KeywordStatus.ANALYZED,
        KeywordStatus.SELECTED,
        KeywordStatus.ASSIGNED,
        KeywordStatus.REJECTED,
    ],
)
def test_status_preserved_for_non_discovered(
    session: Session, status: KeywordStatus
) -> None:
    keyword = _make_keyword(session, status=status)
    _seed_signals(session, keyword.id, _KNOWN_VALUES)
    service = KeywordScoringService(session)

    service.score_keyword_from_latest_signals(keyword.id)

    assert KeywordRepository(session).get_by_id(keyword.id).status == status


def test_uses_latest_signal_per_component_and_keeps_history(session: Session) -> None:
    keyword = _make_keyword(session)
    _seed_signals(session, keyword.id, _KNOWN_VALUES)
    # trend を再収集 (新しい値)
    KeywordSignalRepository(session).create(
        keyword_id=keyword.id,
        component="trend",
        normalized_value=0.0,
        provider="google_trends",
        observed_at=_OBSERVED,
    )
    session.commit()
    service = KeywordScoringService(session)

    read = service.score_keyword_from_latest_signals(keyword.id)

    # trend が 90 -> 0 になったぶん total が 9.0 下がる
    assert read.trend == 0.0
    assert read.total_score == round(_EXPECTED_TOTAL - 9.0, 2)
    # 古い trend signal も履歴として残っている (trend は 2 件)
    assert len(KeywordSignalRepository(session).list_by_component(keyword.id, "trend")) == 2


def test_missing_one_component_raises_and_creates_nothing(session: Session) -> None:
    keyword = _make_keyword(session)
    partial = {k: v for k, v in _KNOWN_VALUES.items() if k != "site_relevance"}
    _seed_signals(session, keyword.id, partial)
    service = KeywordScoringService(session)

    with pytest.raises(IncompleteSignalSetError) as exc_info:
        service.score_keyword_from_latest_signals(keyword.id)

    assert exc_info.value.missing_components == ["site_relevance"]
    assert exc_info.value.keyword_id == keyword.id

    session.rollback()
    assert session.scalars(select(KeywordScore)).all() == []
    refreshed = KeywordRepository(session).get_by_id(keyword.id)
    assert refreshed.opportunity_score is None
    assert refreshed.status == KeywordStatus.DISCOVERED


def test_missing_multiple_components(session: Session) -> None:
    keyword = _make_keyword(session)
    _seed_signals(session, keyword.id, {"search_demand": 10.0, "trend": 20.0})
    service = KeywordScoringService(session)

    with pytest.raises(IncompleteSignalSetError) as exc_info:
        service.score_keyword_from_latest_signals(keyword.id)

    missing = set(exc_info.value.missing_components)
    assert missing == set(COMPONENT_NAMES) - {"search_demand", "trend"}
    assert len(missing) == 5


def test_nonexistent_keyword_raises(session: Session) -> None:
    service = KeywordScoringService(session)
    with pytest.raises(EntityNotFoundError):
        service.score_keyword_from_latest_signals(999999)


def test_transaction_failure_rolls_back(session: Session, monkeypatch) -> None:
    keyword = _make_keyword(session)
    _seed_signals(session, keyword.id, _KNOWN_VALUES)
    service = KeywordScoringService(session)

    def _boom() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(session, "commit", _boom)
    with pytest.raises(RuntimeError):
        service.score_keyword_from_latest_signals(keyword.id)

    monkeypatch.undo()
    assert session.scalars(select(KeywordScore)).all() == []
    assert session.scalars(select(KeywordScoreSignal)).all() == []
    refreshed = KeywordRepository(session).get_by_id(keyword.id)
    assert refreshed.opportunity_score is None
    assert refreshed.status == KeywordStatus.DISCOVERED


def test_list_score_signals_provenance(session: Session) -> None:
    keyword = _make_keyword(session)
    _seed_signals(session, keyword.id, _KNOWN_VALUES)
    service = KeywordScoringService(session)
    read = service.score_keyword_from_latest_signals(keyword.id)

    signals = service.list_score_signals(keyword.id, read.id)
    assert len(signals) == 7
    assert {s.component for s in signals} == set(COMPONENT_NAMES)


def test_list_score_signals_manual_score_returns_empty(session: Session) -> None:
    keyword = _make_keyword(session)
    service = KeywordScoringService(session)
    read = service.score_keyword(
        keyword.id, KeywordScoreCreate(**_KNOWN_VALUES)
    )

    assert service.list_score_signals(keyword.id, read.id) == []


def test_list_score_signals_wrong_keyword_raises(session: Session) -> None:
    kw_a = _make_keyword(session)
    _seed_signals(session, kw_a.id, _KNOWN_VALUES)
    service = KeywordScoringService(session)
    read = service.score_keyword_from_latest_signals(kw_a.id)

    other = Keyword(keyword="other")
    session.add(other)
    session.commit()

    with pytest.raises(EntityNotFoundError):
        service.list_score_signals(other.id, read.id)


def test_list_score_signals_missing_score_raises(session: Session) -> None:
    keyword = _make_keyword(session)
    service = KeywordScoringService(session)
    with pytest.raises(EntityNotFoundError):
        service.list_score_signals(keyword.id, 999999)
