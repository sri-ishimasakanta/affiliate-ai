"""ローカル CSV から AffiliateProgram カタログを投入する importer。

外部 ASP API / スクレイピングは使わない。手元で管理したアフィリエイト案件一覧を
安全に DB へ投入するためのもの。

    uv run python scripts/import_affiliate_programs.py --file programs.csv
    uv run python scripts/import_affiliate_programs.py --file programs.csv --dry-run

CSV 列 (ヘッダ必須):
    name              (必須)
    provider category commission_type commission_value currency
    landing_page_url tracking_url notes status            (任意)
    match_terms       (任意。パイプ区切り: "議事録|AI 議事録|文字起こし")

重複ポリシー: 同一 (name, provider) が既にあれば **skip** (upsert しない)。
安全性: 1 行のエラーでもセル値・tracking_url を出力しない。credential は扱わない。
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import ValidationError  # noqa: E402

from app.affiliate.schemas import AffiliateProgramCreate  # noqa: E402
from app.config.database import SessionLocal  # noqa: E402
from app.exceptions import DuplicateEntityError  # noqa: E402
from app.repositories.affiliate_program_repository import (  # noqa: E402
    AffiliateProgramRepository,
)
from app.services.affiliate_program_service import AffiliateProgramService  # noqa: E402

EXIT_OK = 0
EXIT_ROW_ERRORS = 1
EXIT_BAD_INPUT = 2

_OPTIONAL_STR_COLUMNS = (
    "provider",
    "category",
    "commission_type",
    "currency",
    "landing_page_url",
    "tracking_url",
    "notes",
    "status",
)
_MATCH_TERMS_SEPARATOR = "|"


@dataclass
class ImportSummary:
    total_rows: int = 0
    imported: int = 0
    skipped_duplicate: int = 0
    invalid: int = 0
    messages: list[str] = field(default_factory=list)


def _cell(row: dict[str, str], key: str) -> str | None:
    raw = row.get(key)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _row_to_payload(row: dict[str, str]) -> AffiliateProgramCreate:
    """CSV 1 行 -> AffiliateProgramCreate。値そのものを含む例外は投げない。"""

    name = _cell(row, "name")
    if not name:
        raise ValueError("missing required value for 'name'")

    kwargs: dict[str, object] = {"name": name}
    for key in _OPTIONAL_STR_COLUMNS:
        value = _cell(row, key)
        if value is not None:
            kwargs[key] = value

    commission_value = _cell(row, "commission_value")
    if commission_value is not None:
        try:
            kwargs["commission_value"] = float(commission_value)
        except ValueError:
            raise ValueError("commission_value must be a number") from None

    match_terms = _cell(row, "match_terms")
    if match_terms is not None:
        kwargs["match_terms"] = match_terms.split(_MATCH_TERMS_SEPARATOR)

    return AffiliateProgramCreate(**kwargs)


def _validation_message(row_number: int, exc: ValidationError) -> str:
    # セル値 (errors()[*]["input"]) は出さず、フィールド名と件数だけ。
    fields = sorted(
        {".".join(str(part) for part in err["loc"]) or "?" for err in exc.errors()}
    )
    return f"row {row_number}: invalid ({len(exc.errors())} error(s) in: {', '.join(fields)})"


def run_import(
    csv_path: Path,
    *,
    dry_run: bool,
    session_factory=SessionLocal,
) -> ImportSummary:
    summary = ImportSummary()

    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "name" not in reader.fieldnames:
            raise ValueError("CSV must have a 'name' header column")
        rows = list(reader)

    with session_factory() as session:
        repo = AffiliateProgramRepository(session)
        service = AffiliateProgramService(session)

        for row_number, raw_row in enumerate(rows, start=2):
            summary.total_rows += 1
            try:
                payload = _row_to_payload(raw_row)
            except ValidationError as exc:
                summary.invalid += 1
                summary.messages.append(_validation_message(row_number, exc))
                continue
            except ValueError as exc:
                summary.invalid += 1
                summary.messages.append(f"row {row_number}: invalid ({exc})")
                continue

            if repo.get_by_name_and_provider(payload.name, payload.provider) is not None:
                summary.skipped_duplicate += 1
                summary.messages.append(
                    f"row {row_number}: skipped (duplicate name+provider)"
                )
                continue

            if dry_run:
                summary.imported += 1
                summary.messages.append(f"row {row_number}: ok (dry-run, not written)")
                continue

            try:
                service.create_program(payload)
                summary.imported += 1
            except DuplicateEntityError:
                summary.skipped_duplicate += 1
                summary.messages.append(f"row {row_number}: skipped (duplicate)")

    return summary


def _print_summary(summary: ImportSummary, *, dry_run: bool) -> None:
    for message in summary.messages:
        print(message)
    verb = "would import" if dry_run else "imported"
    print(
        f"\n{summary.total_rows} row(s): {verb} {summary.imported}, "
        f"skipped {summary.skipped_duplicate}, invalid {summary.invalid}"
    )
    if dry_run:
        print("dry-run: no changes were committed to the database")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="import_affiliate_programs",
        description="ローカル CSV から AffiliateProgram カタログを投入する (ASP API なし)",
    )
    parser.add_argument("--file", required=True, metavar="PATH", help="入力 CSV ファイル")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="DB へ commit せず、パース・検証結果だけ確認する",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    path = Path(args.file)
    if not path.is_file():
        print(f"file not found: {path}", file=sys.stderr)
        return EXIT_BAD_INPUT

    try:
        summary = run_import(path, dry_run=args.dry_run)
    except ValueError as exc:
        print(f"cannot import: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    _print_summary(summary, dry_run=args.dry_run)
    return EXIT_OK if summary.invalid == 0 else EXIT_ROW_ERRORS


if __name__ == "__main__":
    raise SystemExit(main())
