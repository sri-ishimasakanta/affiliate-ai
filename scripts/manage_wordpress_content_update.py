"""管理用 CLI: D-D5D -- 承認済み artifact の内容で、既に published 済みの
WordPress post を実際に update する (write, gated)。

    plan (デフォルト。読み取り専用、WordPress へ一切通信しない):
        uv run python -m scripts.manage_wordpress_content_update \
            --article-id 1 --artifact-id 1 --artifact-hash <hash>

        D-D5C の ``WordPressContentUpdatePreflightService.plan()`` をそのまま
        再利用する (別の contradictory な planner を作らない)。

    execute (書き込み。``--execute`` を明示しない限り実行しない):
        uv run python -m scripts.manage_wordpress_content_update \
            --article-id 1 --artifact-id 1 --artifact-hash <hash> --execute

        fresh な D-D5C classify() (高々 1 回の GET) -> CONTENT_NOOP / drift なら
        書き込み 0。UPDATE_REQUIRED のときだけ Transaction A -> 1 回だけの
        content-update POST -> mandatory read-back GET -> Transaction B。

--post-id / --html / --update-latest / --all は存在しない -- 常に exact な
article_id/artifact_id/artifact_hash のみを受け付ける。

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
from app.services.wordpress_content_update_execution_service import (  # noqa: E402
    WordPressContentUpdateExecutionService,
)
from app.services.wordpress_content_update_preflight_service import (  # noqa: E402
    WordPressContentUpdatePreflightService,
)
from app.wordpress.client import WordPressClient  # noqa: E402

EXIT_OK = 0
EXIT_UNEXPECTED = 3


def _print_plan(result) -> None:
    print("=== WordPress Content Update Plan (READ-ONLY, no network) ===")
    print(f"article_id                                     = {result.article_id}")
    print(f"article_title                                  = {result.article_title}")
    print(f"wordpress_post_id                               = {result.wordpress_post_id}")
    print(f"artifact_id                                     = {result.artifact_id}")
    print(f"approved                                        = {result.approved}")
    print(f"current_canonical_status                        = {result.current_canonical_status}")
    print(f"classification                                  = {result.classification}")
    print(f"reason_code                                      = {result.reason_code}")
    print()
    print("no DB write, no WordPress request. Pass --execute to run with a live preflight.")


def _print_execution(result) -> None:
    print("=== WordPress Content Update Execution ===")
    print(f"article_id                                     = {result.article_id}")
    print(f"wordpress_post_id                               = {result.wordpress_post_id}")
    print(f"artifact_id                                     = {result.artifact_id}")
    print(f"preflight_classification                        = {result.preflight_classification}")
    print(f"executed                                        = {result.executed}")
    print(f"run_id                                          = {result.run_id}")
    print(f"run_status                                       = {result.run_status}")
    print()
    print(f"request_content_hash                            = {result.request_content_hash}")
    expected_raw = result.expected_pre_update_wordpress_raw_content_hash
    observed_raw = result.observed_pre_update_wordpress_raw_content_hash
    response_raw = result.response_content_raw_hash
    print(f"expected_pre_update_wordpress_raw_content_hash  = {expected_raw}")
    print(f"observed_pre_update_wordpress_raw_content_hash  = {observed_raw}")
    print(f"response_content_raw_hash                       = {response_raw}")
    print()
    print(f"wordpress_status                                 = {result.wordpress_status}")
    print(f"reason_code                                       = {result.reason_code}")


def cmd_manage(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        if not args.execute:
            svc = WordPressContentUpdatePreflightService(session)
            result = svc.plan(
                article_id=args.article_id,
                artifact_id=args.artifact_id,
                artifact_hash=args.artifact_hash,
            )
            _print_plan(result)
            return EXIT_OK

        client = WordPressClient(get_settings())
        exec_svc = WordPressContentUpdateExecutionService(session, wordpress_client=client)
        result = exec_svc.execute(
            article_id=args.article_id,
            artifact_id=args.artifact_id,
            artifact_hash=args.artifact_hash,
            idempotency_key=args.idempotency_key,
        )
        _print_execution(result)
        return EXIT_OK


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="manage_wordpress_content_update",
        description=(
            "D-D5D: classify and (with --execute) perform at most one WordPress "
            "content-update POST for an already-published post."
        ),
    )
    parser.add_argument("--article-id", type=int, required=True)
    parser.add_argument("--artifact-id", type=int, required=True)
    parser.add_argument("--artifact-hash", type=str, required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="required to perform a live preflight GET and possibly one write POST",
    )
    parser.add_argument("--idempotency-key", type=str, default=None)
    parser.set_defaults(func=cmd_manage)
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
