"""記事の公式調査結果 (Source + ArticleFact) を JSON からまとめて投入する。

**外部 Web アクセスなし。** human が公式ページで確認した結果を転記した JSON を、
1 file = 1 transaction で local DB へ取り込む。値の推測・補完はしない。

    uv run python scripts/import_article_facts.py --article-id 1 --file facts.json
    uv run python scripts/import_article_facts.py --article-id 1 --file facts.json --dry-run

JSON 形状 (抜粋):
    {
      "article_id": 1,
      "sources": [
        {"tmp_id": "make_pricing", "source_type": "official_pricing",
         "source_url": "https://www.make.com/en/pricing", "title": "Make Pricing",
         "checked_at": "2026-08-28T10:00:00+09:00"}
      ],
      "tools": [
        {"subject_ref": "Make", "affiliate_program_id": 1,
         "facts": {
           "official_url": {"value": "https://www.make.com/", "value_status": "verified",
                            "source": "make_pricing", "checked_at": "2026-08-28T10:00:00+09:00"},
           "japan_business_support": {"value_status": "unknown",
                                      "unknown_reason": "公式に記載なし",
                                      "source": "make_pricing",
                                      "checked_at": "2026-08-28T10:00:00+09:00"}
         }}
      ]
    }
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.database import SessionLocal  # noqa: E402
from app.exceptions import ApplicationError  # noqa: E402
from app.services.article_fact_import_service import (  # noqa: E402
    ArticleFactImportService,
    FactImportResult,
)

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_BAD_INPUT = 2


def run_import(
    *, article_id: int, path: Path, dry_run: bool, session_factory=SessionLocal
) -> FactImportResult:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON root must be an object")
    with session_factory() as session:
        return ArticleFactImportService(session).run(
            article_id=article_id, payload=payload, dry_run=dry_run
        )


def _print(result: FactImportResult) -> None:
    for m in result.messages:
        print(m)
    verb = "would create" if result.dry_run else "created"
    print(
        f"\nsources: {verb} {result.sources_created}, reused {result.sources_reused}"
    )
    print(
        f"facts: {verb} {result.facts_created}, skipped_same {result.facts_skipped_same}"
    )
    if result.dry_run:
        print("dry-run: no changes were committed")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="import_article_facts",
        description="記事の公式調査結果 (Source + ArticleFact) を JSON から投入する",
    )
    parser.add_argument("--article-id", required=True, type=int)
    parser.add_argument("--file", required=True, metavar="PATH")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    path = Path(args.file)
    if not path.is_file():
        print(f"file not found: {path}", file=sys.stderr)
        return EXIT_BAD_INPUT

    try:
        result = run_import(
            article_id=args.article_id, path=path, dry_run=args.dry_run
        )
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"cannot import: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    except ApplicationError as exc:
        print(f"import rejected: {exc}", file=sys.stderr)
        return EXIT_INVALID

    _print(result)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
