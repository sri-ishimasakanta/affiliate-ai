"""KeywordSignalService.derive_competition_ease_manual の検証 (外部通信なし)。"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.exceptions import EntityNotFoundError
from app.keyword.schemas import CompetitionEaseManualCreate
from app.models import Keyword
from app.models.enums import KeywordSignalComponent
from app.repositories.keyword_signal_repository import KeywordSignalRepository
from app.services.keyword_signal_service import KeywordSignalService


def _kw(session: Session, text: str = "AI 議事録 おすすめ") -> Keyword:
    entity = Keyword(keyword=text)
    session.add(entity)
    session.flush()
    session.commit()
    return entity


def _payload(**kw: object) -> CompetitionEaseManualCreate:
    base = {"keyword_difficulty": 32, "source_name": "example_free_seo_tool"}
    base.update(kw)
    return CompetitionEaseManualCreate(**base)


def _naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def test_valid_input_creates_signal(session: Session) -> None:
    keyword = _kw(session)
    before = datetime.now(UTC).replace(tzinfo=None)
    read = KeywordSignalService(session).derive_competition_ease_manual(
        keyword.id, _payload(keyword_difficulty=32)
    )
    after = datetime.now(UTC).replace(tzinfo=None)

    assert read.component == KeywordSignalComponent.COMPETITION_EASE
    assert read.provider == "manual_keyword_difficulty"
    assert read.normalized_value == 68.0  # 100 - 32
    assert read.source_reference == "manual-keyword-difficulty:v1"  # 入力なし -> default
    assert read.period_start is None and read.period_end is None
    assert before <= _naive(read.observed_at) <= after

    raw = read.raw_data
    assert raw["keyword_difficulty"] == 32.0
    assert raw["competition_ease"] == 68.0
    assert raw["difficulty_scale"] == "0_easy_100_hard"
    assert raw["source_name"] == "example_free_seo_tool"
    assert raw["evidence_available"] is True
    assert raw["evidence_coverage"] == 1.0
    assert raw["collection_method"] == "manual"
    assert raw["normalizer"] == {"name": "competition_ease", "version": "v1"}
    # Google Ads competition などは入らない
    assert "competition_index" not in raw
    assert "competition" not in raw


@pytest.mark.parametrize(
    ("difficulty", "ease"), [(0, 100.0), (100, 0.0), (32.45, 67.55)]
)
def test_boundary_and_decimal(session: Session, difficulty: float, ease: float) -> None:
    keyword = _kw(session)
    read = KeywordSignalService(session).derive_competition_ease_manual(
        keyword.id, _payload(keyword_difficulty=difficulty)
    )
    assert read.normalized_value == ease


def test_input_source_reference_and_observed_at_used(session: Session) -> None:
    keyword = _kw(session)
    ts = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)
    read = KeywordSignalService(session).derive_competition_ease_manual(
        keyword.id,
        _payload(source_reference="research-batch-2026-08", observed_at=ts),
    )
    assert read.source_reference == "research-batch-2026-08"
    assert _naive(read.observed_at) == datetime(2026, 8, 28, 9, 0)


def test_persist_and_immutable_history(session: Session) -> None:
    keyword = _kw(session)
    service = KeywordSignalService(session)

    first = service.derive_competition_ease_manual(keyword.id, _payload(keyword_difficulty=20))
    session.rollback()
    assert KeywordSignalRepository(session).get_by_id(first.id) is not None

    second = service.derive_competition_ease_manual(
        keyword.id, _payload(keyword_difficulty=40)
    )
    assert first.id != second.id
    history = KeywordSignalRepository(session).list_by_component(
        keyword.id, "competition_ease"
    )
    assert len(history) == 2
    latest = KeywordSignalRepository(session).get_latest(keyword.id, "competition_ease")
    assert latest.id == second.id
    assert latest.normalized_value == 60.0


def test_keyword_not_found(session: Session) -> None:
    with pytest.raises(EntityNotFoundError):
        KeywordSignalService(session).derive_competition_ease_manual(999999, _payload())


def test_commit_failure_rolls_back(session: Session, monkeypatch) -> None:
    keyword = _kw(session)
    service = KeywordSignalService(session)

    def _boom() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(session, "commit", _boom)
    with pytest.raises(RuntimeError):
        service.derive_competition_ease_manual(keyword.id, _payload())

    monkeypatch.undo()
    assert KeywordSignalRepository(session).list_by_keyword(keyword.id) == []


def test_does_not_disturb_other_signals(session: Session) -> None:
    from app.keyword.schemas import KeywordSignalCreate

    keyword = _kw(session)
    service = KeywordSignalService(session)
    manual = service.create_signal(
        keyword.id,
        KeywordSignalCreate(
            component="search_demand",
            normalized_value=42.0,
            provider="manual",
            observed_at=datetime(2020, 1, 1, tzinfo=UTC),
        ),
    )
    ce = service.derive_competition_ease_manual(keyword.id, _payload())

    repo = KeywordSignalRepository(session)
    assert repo.get_latest(keyword.id, "search_demand").id == manual.id
    assert repo.get_latest(keyword.id, "competition_ease").id == ce.id
    assert len(repo.list_by_keyword(keyword.id)) == 2
