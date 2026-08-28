"""scripts/export_article_plan.py の検証 (in-memory DB、外部通信なし)。"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models import Keyword
from app.models.enums import AffiliateProgramStatus
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.repositories.keyword_signal_repository import KeywordSignalRepository
from scripts import export_article_plan as cli


def _factory(session: Session):
    @contextmanager
    def _f() -> Iterator[Session]:
        yield session

    return _f


def _setup(session: Session) -> Keyword:
    AffiliateProgramRepository(session).create(
        name="Make", provider="direct", commission_type="percentage",
        commission_value=35.0, match_terms=["業務効率化"],
        status=AffiliateProgramStatus.ACTIVE,
    )
    k = Keyword(keyword="業務効率化 ツール おすすめ")
    k.status = "analyzed"
    k.opportunity_score = 68.81
    session.add(k)
    session.flush()
    for comp in (
        "search_demand", "commercial_intent", "affiliate_opportunity",
        "competition_ease", "trend", "originality", "site_relevance",
    ):
        KeywordSignalRepository(session).create(
            keyword_id=k.id, component=comp, normalized_value=50.0, provider="test",
            observed_at=datetime(2026, 1, 1, tzinfo=UTC), raw_data={},
            source_reference="test",
        )
    session.commit()
    return k


def test_export_by_keyword_text(session: Session, capsys) -> None:
    _setup(session)
    rc = cli.run_export(
        keyword="業務効率化 ツール おすすめ", keyword_id=None,
        session_factory=_factory(session),
    )
    data = json.loads(rc)
    assert data["article_type"] == "recommendation_roundup"
    assert data["keyword"] == "業務効率化 ツール おすすめ"
    assert "tracking" not in rc.lower()


def test_export_by_keyword_id(session: Session) -> None:
    k = _setup(session)
    data = json.loads(
        cli.run_export(keyword=None, keyword_id=k.id, session_factory=_factory(session))
    )
    assert data["keyword_id"] == k.id


def test_export_keyword_not_found_exit_1(session: Session, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "SessionLocal", _factory(session))
    assert cli.main(["--keyword", "存在しない"]) == cli.EXIT_NOT_FOUND
    assert "not found" in capsys.readouterr().err.lower()


def test_requires_one_selector() -> None:
    # argparse の mutually exclusive required
    try:
        cli.main([])
    except SystemExit as exc:
        assert exc.code == 2
