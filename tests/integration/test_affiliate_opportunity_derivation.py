"""KeywordSignalService.derive_affiliate_opportunity の検証 (独立した in-memory DB)。

実 catalog (dev DB の 19 件) には一切触れない。fixture の in-memory session に
テスト用 program を投入する。
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.exceptions import EntityNotFoundError
from app.models import Keyword
from app.models.enums import AffiliateProgramStatus, KeywordSignalComponent
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.repositories.keyword_signal_repository import KeywordSignalRepository
from app.services.keyword_signal_service import KeywordSignalService

_TRACKING = "https://aff.example.test/redirect?token=SUPER_SECRET_TRACK_ID"
_LANDING = "https://lp.example.test/secret-landing"


def _make_keyword(session: Session, text: str) -> Keyword:
    entity = Keyword(keyword=text)
    session.add(entity)
    session.flush()
    session.commit()
    return entity


def _seed_catalog(session: Session) -> None:
    repo = AffiliateProgramRepository(session)
    repo.create(
        name="Meeting AI A",
        provider="PartnerStack",
        category="ai_meeting",
        commission_type="fixed",
        commission_value=25,
        currency="USD",
        match_terms=["議事録", "AI 議事録", "文字起こし"],
        tracking_url=_TRACKING,
        landing_page_url=_LANDING,
        status=AffiliateProgramStatus.ACTIVE,
    )
    repo.create(
        name="Meeting AI B",
        provider="direct",
        category="ai_meeting",
        commission_type="percentage",
        commission_value=30,
        match_terms=["議事録", "AI 議事録"],
        status=AffiliateProgramStatus.ACTIVE,
    )
    repo.create(
        name="Meeting AI C",
        provider="Impact",
        category="ai_meeting",
        commission_type="percentage",
        commission_value=10,
        match_terms=["議事録"],
        status=AffiliateProgramStatus.ACTIVE,
    )
    repo.create(
        name="Paused Notion",
        provider="PartnerStack",
        commission_type="percentage",
        commission_value=20,
        match_terms=["Notion", "Notion AI", "議事録"],
        status=AffiliateProgramStatus.PAUSED,
    )
    repo.create(
        name="Unknown Jasper",
        provider="FirstPromoter",
        commission_type="percentage",
        commission_value=25,
        match_terms=["生成AI", "議事録"],
        status=AffiliateProgramStatus.UNKNOWN,
    )
    session.commit()


def _naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def test_derive_creates_signal_from_active_catalog(session: Session) -> None:
    _seed_catalog(session)
    keyword = _make_keyword(session, "AI 議事録 おすすめ")
    service = KeywordSignalService(session)

    before = datetime.now(UTC).replace(tzinfo=None)
    read = service.derive_affiliate_opportunity(keyword.id)
    after = datetime.now(UTC).replace(tzinfo=None)

    assert read.component == KeywordSignalComponent.AFFILIATE_OPPORTUNITY
    assert read.provider == "affiliate_catalog"
    assert read.source_reference == "affiliate-catalog:local:v1"
    assert read.period_start is None and read.period_end is None
    assert before <= _naive(read.observed_at) <= after

    raw = read.raw_data
    # active のみ: A(fixed) / B(pct30) / C(pct10) の 3 件。paused/unknown は除外。
    assert raw["matched_program_count"] == 3
    assert sorted(raw["matched_program_names"]) == ["Meeting AI A", "Meeting AI B", "Meeting AI C"]
    assert raw["distinct_provider_count"] == 3
    assert raw["active_providers"] == ["Impact", "PartnerStack", "direct"]
    assert raw["program_match_score"] == 52.76
    assert raw["commission_score"] == 75.0  # max percentage 30% * 2.5
    assert raw["provider_spread_score"] == 100.0
    assert raw["program_match_weight"] == 0.55
    assert raw["commission_weight"] == 0.35
    assert raw["provider_spread_weight"] == 0.10
    assert raw["available_weight"] == 1.0
    assert raw["evidence_coverage"] == 1.0
    assert raw["market_evidence_available"] is True
    assert raw["normalizer"] == {"name": "affiliate_opportunity", "version": "v1"}
    assert raw["normalizer_version"] == "v1"
    assert raw["catalog_size"] == 5
    assert raw["active_catalog_size"] == 3

    # (52.76*0.55 + 75.0*0.35 + 100.0*0.10)
    assert read.normalized_value == pytest.approx(65.27, abs=0.01)


def test_percentage_and_fixed_provenance(session: Session) -> None:
    _seed_catalog(session)
    keyword = _make_keyword(session, "AI 議事録 おすすめ")
    read = KeywordSignalService(session).derive_affiliate_opportunity(keyword.id)

    raw = read.raw_data
    pct = {p["name"]: p["value"] for p in raw["percentage_commissions"]}
    assert pct == {"Meeting AI B": 30.0, "Meeting AI C": 10.0}
    fixed = raw["fixed_commissions"]
    assert len(fixed) == 1
    assert fixed[0]["name"] == "Meeting AI A"
    assert fixed[0]["value"] == 25.0
    assert fixed[0]["currency"] == "USD"


def test_secret_urls_never_in_raw_data(session: Session) -> None:
    _seed_catalog(session)
    keyword = _make_keyword(session, "AI 議事録 おすすめ")
    read = KeywordSignalService(session).derive_affiliate_opportunity(keyword.id)

    blob = repr(read.raw_data)
    assert "tracking_url" not in blob
    assert "landing_page_url" not in blob
    assert "SUPER_SECRET_TRACK_ID" not in blob
    assert "secret-landing" not in blob


def test_zero_match_creates_signal_with_value_zero(session: Session) -> None:
    _seed_catalog(session)
    keyword = _make_keyword(session, "ChatGPT 料金")
    read = KeywordSignalService(session).derive_affiliate_opportunity(keyword.id)

    assert read.normalized_value == 0.0
    assert read.raw_data["matched_program_count"] == 0
    assert read.raw_data["market_evidence_available"] is False
    assert read.raw_data["percentage_commissions"] == []
    assert read.raw_data["active_catalog_size"] == 3


def test_paused_and_unknown_excluded(session: Session) -> None:
    _seed_catalog(session)
    # "Notion AI" は paused の Notion 案件にしか term が無い -> 0 match
    keyword = _make_keyword(session, "Notion AI 料金")
    read = KeywordSignalService(session).derive_affiliate_opportunity(keyword.id)
    assert read.normalized_value == 0.0
    assert read.raw_data["matched_program_count"] == 0


def test_derive_persists_and_immutable_history(session: Session) -> None:
    _seed_catalog(session)
    keyword = _make_keyword(session, "AI 議事録 おすすめ")
    service = KeywordSignalService(session)

    first = service.derive_affiliate_opportunity(keyword.id)
    session.rollback()  # commit 済みなら残る
    assert KeywordSignalRepository(session).get_by_id(first.id) is not None

    second = service.derive_affiliate_opportunity(keyword.id)
    assert first.id != second.id
    history = KeywordSignalRepository(session).list_by_component(
        keyword.id, "affiliate_opportunity"
    )
    assert len(history) == 2
    latest = KeywordSignalRepository(session).get_latest(
        keyword.id, "affiliate_opportunity"
    )
    assert latest.id == second.id


def test_derive_does_not_mutate_catalog(session: Session) -> None:
    _seed_catalog(session)
    keyword = _make_keyword(session, "AI 議事録 おすすめ")
    before = AffiliateProgramRepository(session).count()

    KeywordSignalService(session).derive_affiliate_opportunity(keyword.id)

    assert AffiliateProgramRepository(session).count() == before


def test_derive_keyword_not_found(session: Session) -> None:
    _seed_catalog(session)
    with pytest.raises(EntityNotFoundError):
        KeywordSignalService(session).derive_affiliate_opportunity(999999)


def test_derive_commit_failure_rolls_back(session: Session, monkeypatch) -> None:
    _seed_catalog(session)
    keyword = _make_keyword(session, "AI 議事録 おすすめ")
    service = KeywordSignalService(session)

    def _boom() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(session, "commit", _boom)
    with pytest.raises(RuntimeError):
        service.derive_affiliate_opportunity(keyword.id)

    monkeypatch.undo()
    assert KeywordSignalRepository(session).list_by_keyword(keyword.id) == []
