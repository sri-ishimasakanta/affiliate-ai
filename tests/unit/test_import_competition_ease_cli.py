"""scripts/import_competition_ease.py の CSV importer テスト (外部通信なし)。"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.models import Keyword
from app.repositories.keyword_signal_repository import KeywordSignalRepository
from scripts.import_competition_ease import (
    EXIT_BAD_INPUT,
    ImportSummary,
    main,
    run_import,
)

_HEADER = "keyword,keyword_difficulty,source_name,source_reference,observed_at"


def _factory(session: Session):
    @contextmanager
    def _f() -> Iterator[Session]:
        yield session

    return _f


def _csv(tmp_path: Path, *rows: str, header: str = _HEADER) -> Path:
    path = tmp_path / "kd.csv"
    path.write_text("\n".join((header, *rows)) + "\n", encoding="utf-8")
    return path


def _kw(session: Session, text: str) -> Keyword:
    entity = Keyword(keyword=text)
    session.add(entity)
    session.flush()
    session.commit()
    return entity


def _signals(session: Session, keyword_id: int) -> list:
    return KeywordSignalRepository(session).list_by_component(
        keyword_id, "competition_ease"
    )


# -- valid import --------------------------------------------------
def test_valid_multi_row_import(tmp_path: Path, session: Session) -> None:
    a = _kw(session, "AI 議事録 おすすめ")
    b = _kw(session, "ChatGPT 料金")
    path = _csv(
        tmp_path,
        "AI 議事録 おすすめ,32,example_free_seo_tool,,2026-08-28T00:00:00Z",
        "ChatGPT 料金,78,example_free_seo_tool,,",
    )
    summary = run_import(path, dry_run=False, force=False, session_factory=_factory(session))

    assert isinstance(summary, ImportSummary)
    assert (summary.total_rows, summary.would_import, summary.invalid) == (2, 2, 0)
    sa = _signals(session, a.id)
    assert len(sa) == 1 and sa[0].normalized_value == 68.0
    sb = _signals(session, b.id)
    assert len(sb) == 1 and sb[0].normalized_value == 22.0


def test_source_name_trim_and_observed_at_parse(tmp_path: Path, session: Session) -> None:
    kw = _kw(session, "kw x")
    path = _csv(tmp_path, "kw x,40,  spaced_tool  ,,2026-08-28T09:30:00Z")
    run_import(path, dry_run=False, force=False, session_factory=_factory(session))
    [sig] = _signals(session, kw.id)
    assert sig.raw_data["source_name"] == "spaced_tool"
    assert sig.observed_at.replace(tzinfo=None) == datetime(2026, 8, 28, 9, 30)


# -- dry-run -----------------------------------------------------
def test_dry_run_writes_nothing(tmp_path: Path, session: Session) -> None:
    kw = _kw(session, "kw dry")
    path = _csv(tmp_path, "kw dry,50,t,,")
    summary = run_import(path, dry_run=True, force=False, session_factory=_factory(session))
    assert summary.would_import == 1
    assert _signals(session, kw.id) == []


# -- invalid rows ---------------------------------------------
def test_invalid_difficulty_and_out_of_range(tmp_path: Path, session: Session) -> None:
    _kw(session, "kw1")
    _kw(session, "kw2")
    _kw(session, "kw3")
    _kw(session, "ok kw")
    path = _csv(
        tmp_path,
        "kw1,not-a-number,t,,",
        "kw2,150,t,,",
        "kw3,-5,t,,",
        "ok kw,30,t,,",
    )
    summary = run_import(path, dry_run=False, force=False, session_factory=_factory(session))
    assert summary.invalid == 3
    assert summary.would_import == 1


def test_unknown_keyword_is_invalid(tmp_path: Path, session: Session) -> None:
    path = _csv(tmp_path, "存在しない keyword,30,t,,")
    summary = run_import(path, dry_run=False, force=False, session_factory=_factory(session))
    assert summary.invalid == 1
    assert "not found" in summary.messages[0]


def test_missing_source_name_invalid(tmp_path: Path, session: Session) -> None:
    _kw(session, "kw ns")
    path = _csv(tmp_path, "kw ns,30,,,")
    summary = run_import(path, dry_run=False, force=False, session_factory=_factory(session))
    assert summary.invalid == 1


def test_duplicate_keyword_in_file_is_invalid(tmp_path: Path, session: Session) -> None:
    kw = _kw(session, "dup kw")
    path = _csv(tmp_path, "dup kw,30,t,,", "dup kw,40,t,,")
    summary = run_import(path, dry_run=False, force=False, session_factory=_factory(session))
    assert summary.would_import == 1
    assert summary.invalid == 1
    assert len(_signals(session, kw.id)) == 1


# -- rerun safety / --force ---------------------------------
def test_rerun_same_value_is_skipped(tmp_path: Path, session: Session) -> None:
    kw = _kw(session, "rerun kw")
    path = _csv(tmp_path, "rerun kw,30,example_free_seo_tool,,")

    first = run_import(path, dry_run=False, force=False, session_factory=_factory(session))
    assert first.would_import == 1
    second = run_import(path, dry_run=False, force=False, session_factory=_factory(session))
    assert second.skipped == 1
    assert second.would_import == 0
    assert len(_signals(session, kw.id)) == 1  # history 増えない


def test_force_adds_new_history_even_if_same(tmp_path: Path, session: Session) -> None:
    kw = _kw(session, "force kw")
    path = _csv(tmp_path, "force kw,30,example_free_seo_tool,,")
    run_import(path, dry_run=False, force=False, session_factory=_factory(session))
    run_import(path, dry_run=False, force=True, session_factory=_factory(session))
    assert len(_signals(session, kw.id)) == 2


def test_rerun_different_value_creates_new_history(tmp_path: Path, session: Session) -> None:
    kw = _kw(session, "changed kw")
    run_import(
        _csv(tmp_path, "changed kw,30,t,,"),
        dry_run=False,
        force=False,
        session_factory=_factory(session),
    )
    run_import(
        _csv(tmp_path, "changed kw,55,t,,"),
        dry_run=False,
        force=False,
        session_factory=_factory(session),
    )
    history = _signals(session, kw.id)
    assert len(history) == 2


# -- safety / CLI ------------------------------------------
def test_secret_like_source_reference_not_leaked_on_error(
    tmp_path: Path, session: Session, capsys
) -> None:
    _kw(session, "leak kw")
    secret = "https://tool.example.test/report?key=SUPER_SECRET_KEY"
    # difficulty 不正 -> この行は invalid。source_reference をメッセージへ出さない
    path = _csv(tmp_path, f"leak kw,bad,t,{secret},")
    summary = run_import(path, dry_run=False, force=False, session_factory=_factory(session))
    joined = " ".join(summary.messages)
    assert "SUPER_SECRET_KEY" not in joined
    assert secret not in joined
    out = capsys.readouterr()
    assert "SUPER_SECRET_KEY" not in (out.out + out.err)


def test_main_requires_file() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_main_file_not_found(capsys) -> None:
    assert main(["--file", "nope-12345.csv"]) == EXIT_BAD_INPUT
    assert "not found" in capsys.readouterr().err.lower()


def test_missing_keyword_column_is_bad_input(tmp_path: Path, session: Session) -> None:
    path = _csv(tmp_path, "x,1", header="difficulty,source_name")
    with pytest.raises(ValueError, match="keyword"):
        run_import(path, dry_run=True, force=False, session_factory=_factory(session))
