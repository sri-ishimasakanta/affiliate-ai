"""KeywordSignalService.derive_site_relevance の検証 (完全ローカル・外部通信なし)。"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.exceptions import EntityNotFoundError
from app.models import Keyword
from app.models.enums import KeywordSignalComponent
from app.repositories.keyword_signal_repository import KeywordSignalRepository
from app.services.keyword_signal_service import KeywordSignalService


def _make_keyword(session: Session, text: str = "AI 議事録 おすすめ") -> Keyword:
    entity = Keyword(keyword=text)
    session.add(entity)
    session.flush()
    session.commit()
    return entity


def _naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def test_derive_creates_site_relevance_signal(session: Session) -> None:
    keyword = _make_keyword(session, "AI 議事録 おすすめ")
    service = KeywordSignalService(session)

    before = datetime.now(UTC).replace(tzinfo=None)
    read = service.derive_site_relevance(keyword.id)
    after = datetime.now(UTC).replace(tzinfo=None)

    assert read.component == KeywordSignalComponent.SITE_RELEVANCE
    assert read.provider == "site_profile"
    assert read.normalized_value == 90.0  # CORE_THEME + ADJACENT_USE_CASE
    assert read.source_reference == "site-profile:ai-business-automation:v1"
    assert before <= _naive(read.observed_at) <= after
    # 静的評価なので時系列フィールドは None
    assert read.period_start is None
    assert read.period_end is None

    raw = read.raw_data
    assert raw["base_score"] == 80.0
    assert set(raw["matched_groups"]) == {"CORE_THEME", "ADJACENT_USE_CASE"}
    assert "ai" in raw["matched_terms"]
    assert "議事録" in raw["matched_terms"]
    assert raw["business_context_terms"] == []
    assert raw["out_of_scope_terms"] == []
    assert raw["multi_group_bonus"] == 10.0
    assert raw["business_context_bonus"] == 0.0
    assert raw["profile_name"] == "ai_business_automation"
    assert raw["profile_version"] == "v1"
    assert raw["normalizer_version"] == "v1"
    assert raw["normalizer"] == {"name": "site_relevance", "version": "v1"}
    # secret / customer id 等は入らない
    assert not any("token" in k or "customer" in k or "secret" in k for k in raw)


def test_derive_passes_keyword_text_to_normalizer(session: Session) -> None:
    keyword = _make_keyword(session, "鶏肉 レシピ")
    read = KeywordSignalService(session).derive_site_relevance(keyword.id)
    assert read.normalized_value == 0.0  # out-of-scope
    assert read.raw_data["out_of_scope_terms"] == ["レシピ"]


def test_derive_persists_and_is_retrievable(session: Session) -> None:
    keyword = _make_keyword(session, "AI 業務効率化")
    service = KeywordSignalService(session)

    read = service.derive_site_relevance(keyword.id)
    session.rollback()  # commit 済みなら残る

    stored = KeywordSignalRepository(session).get_by_id(read.id)
    assert stored is not None
    assert stored.component == "site_relevance"
    assert stored.provider == "site_profile"
    assert stored.normalized_value == 100.0


def test_derive_keyword_not_found_raises(session: Session) -> None:
    with pytest.raises(EntityNotFoundError):
        KeywordSignalService(session).derive_site_relevance(999999)


def test_derive_does_not_change_keyword_status(session: Session) -> None:
    keyword = Keyword(keyword="AI 議事録 おすすめ")
    keyword.status = "discovered"
    session.add(keyword)
    session.flush()
    session.commit()

    KeywordSignalService(session).derive_site_relevance(keyword.id)

    session.expire_all()
    assert session.get(Keyword, keyword.id).status == "discovered"


def test_derive_commit_failure_rolls_back(session: Session, monkeypatch) -> None:
    keyword = _make_keyword(session, "AI 議事録 おすすめ")
    service = KeywordSignalService(session)

    def _boom() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(session, "commit", _boom)
    with pytest.raises(RuntimeError):
        service.derive_site_relevance(keyword.id)

    monkeypatch.undo()
    assert KeywordSignalRepository(session).list_by_keyword(keyword.id) == []


def test_repeated_derivation_appends_immutable_history(session: Session) -> None:
    keyword = _make_keyword(session, "AI 議事録 おすすめ")
    service = KeywordSignalService(session)

    first = service.derive_site_relevance(keyword.id)
    second = service.derive_site_relevance(keyword.id)

    assert first.id != second.id
    history = KeywordSignalRepository(session).list_by_component(
        keyword.id, "site_relevance"
    )
    assert len(history) == 2
    latest = KeywordSignalRepository(session).get_latest(keyword.id, "site_relevance")
    assert latest.id == second.id


def test_derive_coexists_with_manual_signals(session: Session) -> None:
    from app.keyword.schemas import KeywordSignalCreate

    keyword = _make_keyword(session, "AI 議事録 おすすめ")
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
    site = service.derive_site_relevance(keyword.id)

    repo = KeywordSignalRepository(session)
    assert repo.get_latest(keyword.id, "search_demand").id == manual.id
    assert repo.get_latest(keyword.id, "site_relevance").id == site.id
    assert len(repo.list_by_keyword(keyword.id)) == 2
    # competition_ease / affiliate_opportunity / originality は作られない
    for absent in ("competition_ease", "affiliate_opportunity", "originality"):
        assert repo.get_latest(keyword.id, absent) is None
