"""管理用 CLI: Threads の接続確認 (T1)。

    uv run python scripts/check_threads_connection.py
    uv run python scripts/check_threads_connection.py --json threads.json

**read-only。投稿は行わない。** このコマンドには公開する経路が無い。

設定が揃っていればプロフィール (id / username) だけを読む。揃っていなければ、
何が足りないかを示して安全に終了する。

**access token は決して表示しない** -- 出せるのは「設定されているか」だけで、
先頭数文字も出さない。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.settings import get_settings  # noqa: E402
from app.social.threads.models import (  # noqa: E402
    REQUIRED_SCOPES,
    UNVERIFIED_FACTS,
)
from app.social.threads.service import ThreadsService  # noqa: E402

EXIT_OK = 0
EXIT_NOT_CONFIGURED = 2
EXIT_UNREACHABLE = 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="設定だけを表示し、Threads へ問い合わせない",
    )
    parser.add_argument("--json", dest="json_path", help="結果を JSON で書き出すパス")
    args = parser.parse_args(argv)

    service = ThreadsService(get_settings())
    status = service.describe() if args.offline else service.check_connection()
    payload = status.as_dict()

    print("=== threads connection ===")
    print(f"threads_enabled                  = {payload['threads_enabled']}")
    print(f"threads_user_id_configured       = {payload['threads_user_id_configured']}")
    print(f"threads_access_token_configured  = {payload['threads_access_token_configured']}")
    print(f"threads_api_version              = {payload['threads_api_version']}")
    print(f"configured                       = {payload['threads_configured']}")
    print(f"reachable                        = {payload['reachable']}")
    for note in payload["notes"]:
        print(f"  note: {note}")

    if payload.get("profile"):
        print(f"\naccount user_id                  = {payload['profile']['user_id']}")
        print(f"account username                 = {payload['profile']['username']}")
    if payload.get("error"):
        err = payload["error"]
        print(f"\nerror category                   = {err['category']}")
        print(f"error reason                     = {err['reason']}")
        print(f"http status                      = {err['status']}")

    if not payload["threads_configured"]:
        print("\n必要な設定 (.env):")
        print("  THREADS_ENABLED=true")
        print("  THREADS_USER_ID=<Threads のユーザ id>")
        print("  THREADS_ACCESS_TOKEN=<long-lived access token>")
        print(f"\nMeta 側で必要な権限: {', '.join(REQUIRED_SCOPES)}")

    print("\n-- 公式ドキュメントで確認できなかったこと --")
    for fact in UNVERIFIED_FACTS:
        print(f"  * {fact}")
    print("\nこのコマンドは投稿しない。access token は表示しない。")

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nJSON written to {args.json_path}")

    if not payload["threads_configured"]:
        return EXIT_NOT_CONFIGURED
    return EXIT_OK if payload["reachable"] is not False else EXIT_UNREACHABLE


if __name__ == "__main__":
    raise SystemExit(main())
