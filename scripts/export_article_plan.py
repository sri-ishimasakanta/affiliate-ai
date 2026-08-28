"""1 keyword の Article Plan (read-only) を JSON で出力する。

    uv run python scripts/export_article_plan.py --keyword "業務効率化 ツール おすすめ"
    uv run python scripts/export_article_plan.py --keyword-id 21 --output plan.json

DB は read-only。LLM / 外部 API を呼ばない。tracking_url 等は ArticlePlanDTO に
含まれないためそのまま出力してよい。bulk export はしない (1 件のみ)。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.database import SessionLocal  # noqa: E402
from app.exceptions import EntityNotFoundError  # noqa: E402
from app.repositories.keyword_repository import KeywordRepository  # noqa: E402
from app.services.article_plan_service import ArticlePlanService  # noqa: E402

EXIT_OK = 0
EXIT_NOT_FOUND = 1
EXIT_BAD_INPUT = 2


def run_export(
    *,
    keyword: str | None,
    keyword_id: int | None,
    session_factory=SessionLocal,
) -> str:
    with session_factory() as session:
        if keyword_id is None:
            entity = KeywordRepository(session).get_by_keyword((keyword or "").strip())
            if entity is None:
                raise EntityNotFoundError("Keyword", keyword)
            keyword_id = entity.id
        dto = ArticlePlanService(session).plan_for_keyword(keyword_id)
    return dto.model_dump_json(indent=2)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="export_article_plan",
        description="1 keyword の Article Plan を JSON 出力する (read-only)",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--keyword", metavar="TEXT")
    group.add_argument("--keyword-id", type=int, metavar="ID")
    parser.add_argument("--output", metavar="PATH", help="省略時は標準出力")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        payload = run_export(keyword=args.keyword, keyword_id=args.keyword_id)
    except EntityNotFoundError as exc:
        print(f"keyword not found: {exc}", file=sys.stderr)
        return EXIT_NOT_FOUND

    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(payload)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
