"""analyze_affiliate_opportunities CLI の DB 連携部分の統合テスト (read-only)。"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.enums import AffiliateProgramStatus
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from scripts.analyze_affiliate_opportunities import (
    EXIT_BAD_INPUT,
    EXIT_OK,
    load_active_program_facts,
    main,
    run_analysis,
)

_TRACKING = "https://aff.example.test/redirect?token=SUPER_SECRET_TRACK_ID"
_LANDING = "https://lp.example.test/secret-landing"


def _session_factory(session: Session):
    @contextmanager
    def _factory() -> Iterator[Session]:
        yield session

    return _factory


def _seed(session: Session) -> None:
    repo = AffiliateProgramRepository(session)
    repo.create(
        name="Active Meeting AI",
        provider="direct",
        category="ai_meeting",
        commission_type="percentage",
        commission_value=30,
        match_terms=["議事録", "AI 議事録", "文字起こし"],
        tracking_url=_TRACKING,
        landing_page_url=_LANDING,
        status=AffiliateProgramStatus.ACTIVE,
    )
    repo.create(
        name="Paused Notion",
        provider="PartnerStack",
        commission_type="percentage",
        commission_value=20,
        match_terms=["Notion", "Notion AI"],
        status=AffiliateProgramStatus.PAUSED,
    )
    repo.create(
        name="Unknown Jasper",
        provider="FirstPromoter",
        commission_type="percentage",
        commission_value=25,
        match_terms=["生成AI", "AI ライティング"],
        status=AffiliateProgramStatus.UNKNOWN,
    )
    session.commit()


def test_load_active_program_facts_excludes_paused_and_unknown(session: Session) -> None:
    _seed(session)
    facts = load_active_program_facts(session)
    assert [f.name for f in facts] == ["Active Meeting AI"]
    # URL は ProgramFacts に含まれない
    assert not hasattr(facts[0], "tracking_url")
    assert not hasattr(facts[0], "landing_page_url")


def test_run_analysis_only_matches_active(session: Session, capsys) -> None:
    _seed(session)
    code = run_analysis(
        ["AI 議事録 おすすめ", "Notion AI 料金", "生成AI とは"],
        session_factory=_session_factory(session),
    )
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "active affiliate programs in catalog: 1" in out
    # paused / unknown はマッチしない -> coverage 1/3
    assert "keywords_with_matches   : 1" in out
    assert "keywords_without_matches: 2" in out


def test_tracking_url_not_in_console_or_csv(
    session: Session, tmp_path: Path, capsys
) -> None:
    _seed(session)
    out_csv = tmp_path / "analysis.csv"
    run_analysis(
        ["AI 議事録 おすすめ", "議事録 自動作成 ツール"],
        session_factory=_session_factory(session),
        output=str(out_csv),
        show_programs=True,
    )
    captured = capsys.readouterr()
    combined = captured.out + captured.err + out_csv.read_text(encoding="utf-8")
    assert "SUPER_SECRET_TRACK_ID" not in combined
    assert _TRACKING not in combined
    assert _LANDING not in combined
    assert "secret-landing" not in combined


def test_csv_written(session: Session, tmp_path: Path) -> None:
    _seed(session)
    out_csv = tmp_path / "out.csv"
    run_analysis(
        ["AI 議事録 おすすめ"],
        session_factory=_session_factory(session),
        output=str(out_csv),
    )
    text = out_csv.read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith("keyword,matched_program_count")
    assert "AI 議事録 おすすめ" in text


def test_main_no_keywords_is_bad_input(capsys) -> None:
    assert main([]) == EXIT_BAD_INPUT
    assert "no keywords" in capsys.readouterr().err.lower()


def test_main_input_file_not_found(capsys) -> None:
    assert main(["--input", "nope-12345.csv"]) == EXIT_BAD_INPUT
    assert "not found" in capsys.readouterr().err.lower()
