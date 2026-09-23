"""管理用 CLI: 公開済み Threads 投稿を読む (T3、read-only)。

    uv run python scripts/check_threads_post.py --publication-id 1

**書き込みはしない。** 公開済み投稿の項目と、公式に存在する指標だけを読む。

取得できなかった指標は 0 で埋めず ``missing`` として残す (「観測できなかった」と
「0 だった」を混同しない)。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.database import SessionLocal  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.services.threads_publication_service import (  # noqa: E402
    ThreadsPublicationError,
    ThreadsPublicationService,
)

EXIT_OK = 0
EXIT_REFUSED = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publication-id", type=int, required=True)
    parser.add_argument("--json", dest="json_path", help="結果を JSON で書き出すパス")
    args = parser.parse_args(argv)

    settings = get_settings()
    with SessionLocal() as session:
        service = ThreadsPublicationService(session, settings=settings)
        try:
            payload = service.inspect(publication_id=args.publication_id)
        except ThreadsPublicationError as exc:
            print(f"refused: {exc.reason}")
            return EXIT_REFUSED

    print("=== threads publication ===")
    print(f"publication_id     = {payload['publication_id']}")
    print(f"proposal_id        = {payload['proposal_id']}")
    print(f"media_id           = {payload['media_id']}")
    print(f"permalink          = {payload['permalink']}")
    print(f"published_at       = {payload['published_at']}")
    print(f"text == approved   = {payload['text_matches_approved']}")
    print(f"remote media       = {payload['media']}")
    print("\n--- insights ---")
    for name, value in (payload["insights"]["values"] or {}).items():
        print(f"  {name:16}= {value}")
    missing = payload["insights"]["missing"]
    if missing:
        print(f"  missing (0 ではなく未観測): {', '.join(missing)}")
    print("\nread-only。投稿もしないし、DB も更新しない。")

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nJSON written to {args.json_path}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
