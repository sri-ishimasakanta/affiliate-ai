"""ローカル CSV から competition_ease evidence (Organic SEO Keyword Difficulty) を投入する。

**外部 API 通信なし・追加実費 0 円。** 無料 SEO ツール等で確認した Keyword Difficulty
(0 = easy 〜 100 = hard) を、既存 Keyword に対する ``competition_ease`` Signal として
まとめて投入する。

    uv run python scripts/import_competition_ease.py --file kd.csv
    uv run python scripts/import_competition_ease.py --file kd.csv --dry-run
    uv run python scripts/import_competition_ease.py --file kd.csv --force

CSV 列 (ヘッダ必須):
    keyword              (必須。既存 Keyword.keyword と完全一致で lookup。新規作成しない)
    keyword_difficulty   (必須。0〜100 の Organic SEO Difficulty)
    source_name          (必須。例: "example_free_seo_tool")
    source_reference     (任意。credential / API key / tracking parameter を入れない)
    observed_at          (任意。ISO-8601)

idempotency: 同一 CSV 内の keyword 重複は invalid。既存の最新 competition_ease Signal が
provider=manual_keyword_difficulty かつ (difficulty, source_name, source_reference) が
今回と一致する場合は default で skip。``--force`` で同値でも新 history を追加する。

安全性: 1 行のエラーでもセル値・source_reference を無制限に出力しない。credential は扱わない。
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import ValidationError  # noqa: E402

from app.config.database import SessionLocal  # noqa: E402
from app.keyword.schemas import CompetitionEaseManualCreate  # noqa: E402
from app.repositories.keyword_repository import KeywordRepository  # noqa: E402
from app.repositories.keyword_signal_repository import (  # noqa: E402
    KeywordSignalRepository,
)
from app.services.keyword_signal_service import (  # noqa: E402
    _COMPETITION_EASE_PROVIDER,
    _COMPETITION_EASE_SOURCE_REFERENCE,
    KeywordSignalService,
)

EXIT_OK = 0
EXIT_ROW_ERRORS = 1
EXIT_BAD_INPUT = 2

_COMPONENT = "competition_ease"


@dataclass
class ImportSummary:
    total_rows: int = 0
    would_import: int = 0
    skipped: int = 0
    invalid: int = 0
    messages: list[str] = field(default_factory=list)


def _cell(row: dict[str, str], key: str) -> str | None:
    raw = row.get(key)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


@dataclass(frozen=True)
class _ParsedRow:
    keyword: str
    payload: CompetitionEaseManualCreate


def _parse_row(row: dict[str, str]) -> _ParsedRow:
    """CSV 1 行 -> (keyword, payload)。値そのものを含む例外は投げない。"""

    keyword = _cell(row, "keyword")
    if not keyword:
        raise ValueError("missing required value for 'keyword'")

    difficulty_raw = _cell(row, "keyword_difficulty")
    if difficulty_raw is None:
        raise ValueError("missing required value for 'keyword_difficulty'")
    try:
        difficulty = float(difficulty_raw)
    except ValueError:
        raise ValueError("keyword_difficulty must be a number") from None

    source_name = _cell(row, "source_name")
    if not source_name:
        raise ValueError("missing required value for 'source_name'")

    observed_raw = _cell(row, "observed_at")
    observed_at: datetime | None = None
    if observed_raw is not None:
        try:
            observed_at = datetime.fromisoformat(observed_raw.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("observed_at must be ISO-8601") from None

    payload = CompetitionEaseManualCreate(
        keyword_difficulty=difficulty,
        source_name=source_name,
        source_reference=_cell(row, "source_reference"),
        observed_at=observed_at,
    )
    return _ParsedRow(keyword=keyword, payload=payload)


def _is_same_as_latest(
    signals: KeywordSignalRepository,
    keyword_id: int,
    payload: CompetitionEaseManualCreate,
) -> bool:
    latest = signals.get_latest(keyword_id, _COMPONENT)
    if latest is None or latest.provider != _COMPETITION_EASE_PROVIDER:
        return False
    raw = latest.raw_data or {}
    effective_ref = payload.source_reference or _COMPETITION_EASE_SOURCE_REFERENCE
    return (
        raw.get("keyword_difficulty") == payload.keyword_difficulty
        and raw.get("source_name") == payload.source_name
        and latest.source_reference == effective_ref
    )


def _validation_message(row_number: int, exc: ValidationError) -> str:
    fields = sorted(
        {".".join(str(part) for part in err["loc"]) or "?" for err in exc.errors()}
    )
    return (
        f"row {row_number}: invalid "
        f"({len(exc.errors())} error(s) in: {', '.join(fields)})"
    )


def run_import(
    csv_path: Path,
    *,
    dry_run: bool,
    force: bool,
    session_factory=SessionLocal,
) -> ImportSummary:
    summary = ImportSummary()

    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "keyword" not in reader.fieldnames:
            raise ValueError("CSV must have a 'keyword' header column")
        rows = list(reader)

    seen_keywords: set[str] = set()

    with session_factory() as session:
        keywords = KeywordRepository(session)
        signals = KeywordSignalRepository(session)
        service = KeywordSignalService(session)

        for row_number, raw_row in enumerate(rows, start=2):
            summary.total_rows += 1
            try:
                parsed = _parse_row(raw_row)
            except ValidationError as exc:
                summary.invalid += 1
                summary.messages.append(_validation_message(row_number, exc))
                continue
            except ValueError as exc:
                summary.invalid += 1
                summary.messages.append(f"row {row_number}: invalid ({exc})")
                continue

            if parsed.keyword in seen_keywords:
                summary.invalid += 1
                summary.messages.append(
                    f"row {row_number}: duplicate keyword in file"
                )
                continue
            seen_keywords.add(parsed.keyword)

            keyword = keywords.get_by_keyword(parsed.keyword)
            if keyword is None:
                summary.invalid += 1
                summary.messages.append(
                    f"row {row_number}: keyword not found (create it first)"
                )
                continue

            if not force and _is_same_as_latest(signals, keyword.id, parsed.payload):
                summary.skipped += 1
                summary.messages.append(
                    f"row {row_number}: skipped (same as latest; use --force)"
                )
                continue

            if dry_run:
                summary.would_import += 1
                summary.messages.append(
                    f"row {row_number}: ok (dry-run, not written)"
                )
                continue

            service.derive_competition_ease_manual(keyword.id, parsed.payload)
            summary.would_import += 1

    return summary


def _print_summary(summary: ImportSummary, *, dry_run: bool) -> None:
    for message in summary.messages:
        print(message)
    verb = "would import" if dry_run else "imported"
    print(
        f"\n{summary.total_rows} row(s): {verb} {summary.would_import}, "
        f"skipped {summary.skipped}, invalid {summary.invalid}"
    )
    if dry_run:
        print("dry-run: no changes were committed to the database")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="import_competition_ease",
        description=(
            "ローカル CSV から competition_ease evidence を投入する "
            "(Organic SEO Keyword Difficulty、外部 API なし)"
        ),
    )
    parser.add_argument("--file", required=True, metavar="PATH", help="入力 CSV")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="DB へ commit せず、パース・検証結果だけ確認する",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="最新 Signal と同値でも新しい history を追加する",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    path = Path(args.file)
    if not path.is_file():
        print(f"file not found: {path}", file=sys.stderr)
        return EXIT_BAD_INPUT

    try:
        summary = run_import(path, dry_run=args.dry_run, force=args.force)
    except ValueError as exc:
        print(f"cannot import: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    _print_summary(summary, dry_run=args.dry_run)
    return EXIT_OK if summary.invalid == 0 else EXIT_ROW_ERRORS


if __name__ == "__main__":
    raise SystemExit(main())
