"""scripts/import_affiliate_programs.py の CSV importer テスト (外部通信なし)。"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from scripts.import_affiliate_programs import (
    EXIT_BAD_INPUT,
    ImportSummary,
    main,
    run_import,
)

_HEADER = (
    "name,provider,category,commission_type,commission_value,currency,"
    "landing_page_url,tracking_url,notes,status,match_terms"
)


def _session_factory(session: Session):
    @contextmanager
    def _factory() -> Iterator[Session]:
        yield session  # fixture セッションは閉じない

    return _factory


def _write_csv(tmp_path: Path, *rows: str, header: str = _HEADER) -> Path:
    path = tmp_path / "programs.csv"
    path.write_text("\n".join((header, *rows)) + "\n", encoding="utf-8")
    return path


def _programs(session: Session) -> list:
    return AffiliateProgramRepository(session).list()


# -- valid import ----------------------------------------------------
def test_valid_csv_imports(tmp_path: Path, session: Session) -> None:
    row1 = ",".join(
        [
            "Example AI Tool", "example", "ai", "fixed", "3000", "jpy",
            "https://example.com", "", "Example only", "active", "AI|生成AI|業務効率化",
        ]
    )
    row2 = ",".join(
        ["議事録メーカー", "a8", "ai", "percentage", "20", "JPY", "", "", "", "",
         "議事録|AI 議事録|文字起こし"]
    )
    path = _write_csv(tmp_path, row1, row2)
    summary = run_import(path, dry_run=False, session_factory=_session_factory(session))

    assert isinstance(summary, ImportSummary)
    assert (summary.total_rows, summary.imported, summary.invalid) == (2, 2, 0)

    rows = {p.name: p for p in _programs(session)}
    assert set(rows) == {"Example AI Tool", "議事録メーカー"}
    first = rows["Example AI Tool"]
    assert first.provider == "example"
    assert first.commission_value == 3000.0
    assert first.currency == "JPY"  # normalized from "jpy"
    assert first.match_terms == ["AI", "生成AI", "業務効率化"]
    assert rows["議事録メーカー"].match_terms == ["議事録", "AI 議事録", "文字起こし"]


def test_optional_fields_default(tmp_path: Path, session: Session) -> None:
    path = _write_csv(tmp_path, "Bare,,,,,,,,,,")
    run_import(path, dry_run=False, session_factory=_session_factory(session))
    [program] = _programs(session)
    assert program.name == "Bare"
    assert program.provider is None
    assert program.match_terms is None
    assert program.status == "active"


# -- dry-run -------------------------------------------------------
def test_dry_run_writes_nothing(tmp_path: Path, session: Session) -> None:
    path = _write_csv(tmp_path, "DryRun,example,ai,fixed,1000,JPY,,,,,AI")
    summary = run_import(path, dry_run=True, session_factory=_session_factory(session))
    assert summary.imported == 1  # "would import"
    assert _programs(session) == []  # 何も書かれない


# -- invalid rows ------------------------------------------------
def test_invalid_row_counted_others_imported(tmp_path: Path, session: Session) -> None:
    path = _write_csv(
        tmp_path,
        "Good,example,ai,fixed,1000,JPY,,,,,AI",
        "BadCurrency,example,ai,fixed,1000,JPYXX,,,,,AI",  # 不正 currency
        ",example,ai,fixed,1000,JPY,,,,,AI",               # name 空
        "BadNumber,example,ai,fixed,not-a-number,JPY,,,,,AI",  # commission_value 非数値
    )
    summary = run_import(path, dry_run=False, session_factory=_session_factory(session))

    assert summary.total_rows == 4
    assert summary.imported == 1
    assert summary.invalid == 3
    assert {p.name for p in _programs(session)} == {"Good"}


def test_missing_name_column_is_bad_input(tmp_path: Path, session: Session) -> None:
    path = _write_csv(tmp_path, "x", header="provider,category")
    with pytest.raises(ValueError, match="name"):
        run_import(path, dry_run=True, session_factory=_session_factory(session))


# -- duplicate policy -----------------------------------------
def test_duplicate_name_provider_skipped(tmp_path: Path, session: Session) -> None:
    path = _write_csv(
        tmp_path,
        "Same,a8,ai,fixed,1000,JPY,,,,,AI",
        "Same,a8,ai,fixed,9999,JPY,,,,,AI",   # 同一 name+provider -> skip
        "Same,moshimo,ai,fixed,1000,JPY,,,,,AI",  # provider 違いは別案件
    )
    summary = run_import(path, dry_run=False, session_factory=_session_factory(session))

    assert summary.imported == 2
    assert summary.skipped_duplicate == 1
    same_a8 = AffiliateProgramRepository(session).get_by_name_and_provider("Same", "a8")
    assert same_a8.commission_value == 1000.0  # 上書きされていない


def test_rerun_skips_already_imported(tmp_path: Path, session: Session) -> None:
    path = _write_csv(tmp_path, "Once,a8,ai,fixed,1000,JPY,,,,,AI")
    run_import(path, dry_run=False, session_factory=_session_factory(session))
    summary = run_import(path, dry_run=False, session_factory=_session_factory(session))
    assert summary.imported == 0
    assert summary.skipped_duplicate == 1
    assert len(_programs(session)) == 1


# -- safety: no secret / tracking_url leakage --------------
def test_tracking_url_not_leaked_in_error_messages(
    tmp_path: Path, session: Session, capsys
) -> None:
    secret_url = "https://aff.example.test/redirect?token=SUPER_SECRET_TRACK_ID"
    path = _write_csv(
        tmp_path,
        # currency 不正 -> この行は invalid。tracking_url がメッセージに出てはいけない
        f"Leaky,example,ai,fixed,1000,BADCUR,https://example.com,{secret_url},note,active,AI",
    )
    summary = run_import(path, dry_run=False, session_factory=_session_factory(session))

    assert summary.invalid == 1
    joined = " ".join(summary.messages)
    assert "SUPER_SECRET_TRACK_ID" not in joined
    assert secret_url not in joined
    # 標準出力にも出ない
    out = capsys.readouterr()
    assert "SUPER_SECRET_TRACK_ID" not in (out.out + out.err)


# -- CLI entrypoint --------------------------------------------
def test_main_requires_file() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2  # argparse usage error


def test_main_file_not_found(capsys) -> None:
    code = main(["--file", "does-not-exist-12345.csv"])
    assert code == EXIT_BAD_INPUT
    assert "not found" in capsys.readouterr().err.lower()
