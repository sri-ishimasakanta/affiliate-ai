"""管理用 CLI: D-D5C -- 承認済み artifact の内容で既に published 済みの WordPress
post を update する必要があるかを READ-ONLY で分類する。

    plan-only (デフォルト。local のみ、WordPress へ一切通信しない):
        uv run python -m scripts.preview_wordpress_content_update \
            --article-id 1 --artifact-id 1 --artifact-hash <hash>

    live read (``--live-read`` を明示した場合のみ、ちょうど 1 回だけ GET):
        uv run python -m scripts.preview_wordpress_content_update \
            --article-id 1 --artifact-id 1 --artifact-hash <hash> --live-read

--execute フラグは存在しない -- このフェーズ (D-D5C) は分類のみで、
WordPressContentUpdateRun 行の作成や WordPress への書き込みは一切行わない
(将来の D-D5D execution service の責務)。

出力は安全な要約のみ: tracked_html 全文・manifest 全文・full token・credential・
Authorization・application password は一切出力しない。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.database import SessionLocal  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.services.wordpress_content_update_preflight_service import (  # noqa: E402
    WordPressContentUpdatePreflightService,
)
from app.wordpress.client import WordPressClient  # noqa: E402

EXIT_OK = 0
EXIT_UNEXPECTED = 3

_NOT_PERFORMED = "LIVE_PREFLIGHT_NOT_PERFORMED"


def _print_result(result, *, live: bool) -> None:
    print("=== WordPress Content Update Preview (READ-ONLY) ===")
    print(
        f"mode                                            = {'live-read' if live else 'plan-only'}"
    )
    print(f"article_id                                      = {result.article_id}")
    print(f"article_title                                   = {result.article_title}")
    print(f"wordpress_post_id                                = {result.wordpress_post_id}")
    print(f"artifact_id                                      = {result.artifact_id}")
    print(f"artifact_hash                                    = {result.artifact_hash}")
    print(f"approved                                         = {result.approved}")
    print(f"current_canonical_status                         = {result.current_canonical_status}")
    print(f"substitution_count                               = {result.substitution_count}")
    print()
    candidate_hash = result.candidate_request_content_hash
    last_submitted_hash = result.last_successful_request_content_hash
    expected_raw = result.expected_pre_update_wordpress_raw_content_hash
    observed_raw = result.observed_pre_update_wordpress_raw_content_hash
    print(f"candidate_request_content_hash                 = {candidate_hash}")
    print(f"last_successful_request_content_hash           = {last_submitted_hash}")
    print(f"expected_pre_update_wordpress_raw_content_hash = {expected_raw}")
    print(f"observed_pre_update_wordpress_raw_content_hash = {observed_raw}")
    print()
    print(f"wordpress_status                                 = {result.wordpress_status}")
    print(f"wordpress_modified_gmt_raw                       = {result.wordpress_modified_gmt_raw}")
    print(f"target_base_url                                  = {result.target_base_url}")
    print()
    print(f"classification                                   = {result.classification}")
    print(f"would_execute                                    = {result.would_execute}")
    print(f"reason_code                                       = {result.reason_code}")
    print()
    if not live:
        print(f"{_NOT_PERFORMED}: pass --live-read to perform exactly one WordPress GET.")
    print("no DB write, no WordPress write (this command performs at most one read-only GET).")


def cmd_preview(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        if args.live_read:
            client = WordPressClient(get_settings())
            svc = WordPressContentUpdatePreflightService(session, wordpress_client=client)
            result = svc.classify(
                article_id=args.article_id,
                artifact_id=args.artifact_id,
                artifact_hash=args.artifact_hash,
            )
        else:
            svc = WordPressContentUpdatePreflightService(session)
            result = svc.plan(
                article_id=args.article_id,
                artifact_id=args.artifact_id,
                artifact_hash=args.artifact_hash,
            )

    _print_result(result, live=args.live_read)
    return EXIT_OK


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="preview_wordpress_content_update",
        description=(
            "D-D5C: READ-ONLY classification of whether a published WordPress post "
            "needs a content update (no write, no run creation)."
        ),
    )
    parser.add_argument("--article-id", type=int, required=True)
    parser.add_argument("--artifact-id", type=int, required=True)
    parser.add_argument("--artifact-hash", type=str, required=True)
    parser.add_argument(
        "--live-read",
        action="store_true",
        help=(
            "perform exactly one real WordPress GET to reach a final classification "
            "(CONTENT_NOOP / UPDATE_REQUIRED / WORDPRESS_CURRENT_CONTENT_DRIFT). "
            "Without this flag, only local-only provisional planning is performed "
            "and no WordPress request is made."
        ),
    )
    parser.set_defaults(func=cmd_preview)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 - 管理用 CLI は安全側に倒す
        print(f"UNEXPECTED: {type(exc).__name__}: {exc}")
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
