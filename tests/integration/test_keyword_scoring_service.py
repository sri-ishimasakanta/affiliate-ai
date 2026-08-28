"""KeywordScoringService のトランザクション/ステータス/キャッシュ更新を検証する。"""

import pytest
from sqlalchemy.orm import Session

from app.exceptions import EntityNotFoundError
from app.keyword.schemas import KeywordScoreCreate
from app.models import Keyword
from app.models.enums import KeywordStatus
from app.repositories.keyword_repository import KeywordRepository
from app.services.keyword_scoring_service import KeywordScoringService

# total_score = 82.25 になる既知の入力
_KNOWN_PAYLOAD = KeywordScoreCreate(
    search_demand=75,
    commercial_intent=95,
    affiliate_opportunity=90,
    competition_ease=55,
    trend=90,
    originality=80,
    site_relevance=100,
)
_OTHER_PAYLOAD = KeywordScoreCreate(
    search_demand=10,
    commercial_intent=10,
    affiliate_opportunity=10,
    competition_ease=10,
    trend=10,
    originality=10,
    site_relevance=10,
)


def _make_keyword(session: Session, *, status: KeywordStatus | None = None) -> Keyword:
    entity = Keyword(keyword="kw")
    if status is not None:
        entity.status = status
    session.add(entity)
    session.flush()
    session.commit()
    return entity


def test_score_keyword_creates_history_and_updates_cache(session: Session) -> None:
    keyword = _make_keyword(session)
    service = KeywordScoringService(session)

    read = service.score_keyword(keyword.id, _KNOWN_PAYLOAD)

    assert read.id is not None
    assert read.keyword_id == keyword.id
    assert read.total_score == 82.25
    assert read.score_version == "v1"
    assert read.input_source == "manual"

    refreshed = KeywordRepository(session).get_by_id(keyword.id)
    assert refreshed.opportunity_score == 82.25


def test_discovered_moves_to_analyzed(session: Session) -> None:
    keyword = _make_keyword(session, status=KeywordStatus.DISCOVERED)
    service = KeywordScoringService(session)

    service.score_keyword(keyword.id, _KNOWN_PAYLOAD)

    assert KeywordRepository(session).get_by_id(keyword.id).status == KeywordStatus.ANALYZED


@pytest.mark.parametrize(
    "status",
    [
        KeywordStatus.ANALYZED,
        KeywordStatus.SELECTED,
        KeywordStatus.ASSIGNED,
        KeywordStatus.REJECTED,
    ],
)
def test_rescore_keeps_non_discovered_status(session: Session, status: KeywordStatus) -> None:
    keyword = _make_keyword(session, status=status)
    service = KeywordScoringService(session)

    service.score_keyword(keyword.id, _KNOWN_PAYLOAD)

    assert KeywordRepository(session).get_by_id(keyword.id).status == status


def test_rescore_appends_history_and_updates_latest_and_cache(session: Session) -> None:
    keyword = _make_keyword(session)
    service = KeywordScoringService(session)

    service.score_keyword(keyword.id, _KNOWN_PAYLOAD)  # 82.25
    second = service.score_keyword(keyword.id, _OTHER_PAYLOAD)  # 10.0

    history = service.list_score_history(keyword.id)
    assert len(history) == 2
    assert history[0].id == second.id  # newest first
    assert history[0].total_score == 10.0

    latest = service.get_latest_score(keyword.id)
    assert latest.id == second.id
    assert latest.total_score == 10.0

    assert KeywordRepository(session).get_by_id(keyword.id).opportunity_score == 10.0


def test_get_latest_score_history_pagination(session: Session) -> None:
    keyword = _make_keyword(session)
    service = KeywordScoringService(session)
    for _ in range(4):
        service.score_keyword(keyword.id, _OTHER_PAYLOAD)

    page = service.list_score_history(keyword.id, limit=2, offset=1)
    assert len(page) == 2


def test_score_keyword_nonexistent_keyword_raises(session: Session) -> None:
    service = KeywordScoringService(session)
    with pytest.raises(EntityNotFoundError):
        service.score_keyword(999999, _KNOWN_PAYLOAD)


def test_get_latest_score_nonexistent_keyword_raises(session: Session) -> None:
    service = KeywordScoringService(session)
    with pytest.raises(EntityNotFoundError):
        service.get_latest_score(999999)


def test_get_latest_score_without_any_score_raises(session: Session) -> None:
    keyword = _make_keyword(session)
    service = KeywordScoringService(session)

    with pytest.raises(EntityNotFoundError) as exc_info:
        service.get_latest_score(keyword.id)

    assert exc_info.value.entity == "KeywordScore"


def test_failure_rolls_back_whole_transaction(session: Session, monkeypatch) -> None:
    keyword = _make_keyword(session, status=KeywordStatus.DISCOVERED)
    service = KeywordScoringService(session)

    def _boom() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(session, "commit", _boom)

    with pytest.raises(RuntimeError):
        service.score_keyword(keyword.id, _KNOWN_PAYLOAD)

    # rollback により履歴もキャッシュも status も変わっていない
    monkeypatch.undo()
    fresh = KeywordRepository(session).get_by_id(keyword.id)
    assert fresh.opportunity_score is None
    assert fresh.status == KeywordStatus.DISCOVERED
    assert service.list_score_history(keyword.id) == []
