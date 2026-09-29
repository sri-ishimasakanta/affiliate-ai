"""管理用 CLI: 今日の Growth Post (T6.3.3) を用意するかを見る / 1 回だけ用意する。

    # 既定は PLAN (DB にもファイルにも書かない。OpenAI にも Threads にも触れない)
    uv run python scripts/maintain_threads_growth_post.py

    # 期限が来ていれば今日の分を 1 本だけ用意する (awaiting_approval。承認・公開はしない)
    uv run python scripts/maintain_threads_growth_post.py --execute

JST の 1 日に 1 本まで。その日の提案があれば、何度実行しても作らない。T6.3.3c: その日の
呼び出しは多くても 4 回 (記録のファイルから数える。再起動しても戻らない)。似すぎた候補は
別の書き方で書き直す。T6.3.3c より前の記録がある日は呼び直さない。
フォロワー数は ``--collect-followers`` のときだけ Threads から 1 回読む (読むだけ)。
記事の無い提案を保存する migration (``c4d2e8f1a9b3``) の前の DB では、PLAN だけが動く。

    # 人が許した、今日 1 回だけの同じ日のやり直し (T6.3.3c より前の記録の日だけ。前の試みは
    # 残し、前の呼び出しも 1 日の上限 4 回に数える。承認・公開はしない)
    uv run python scripts/maintain_threads_growth_post.py --execute --collect-followers \
        --allow-same-day-growth-retry 2026-09-29
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.threads_growth_service import ThreadsGrowthService  # noqa: E402

EXIT_OK = 0


def main(
    argv: list[str] | None = None,
    *,
    session_factory=None,
    settings=None,
    overrides=None,
    now: datetime | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="1 回だけ用意する (既定は PLAN)")
    parser.add_argument(
        "--collect-followers",
        action="store_true",
        help="生成の直前にフォロワー数を Threads から 1 回読む (読むだけ。--execute のときだけ)",
    )
    parser.add_argument("--json", dest="json_path", help="結果を JSON で書き出すパス")
    parser.add_argument(
        "--allow-same-day-growth-retry",
        dest="same_day_retry",
        metavar="YYYY-MM-DD",
        help="人が許した、今日 (JST) 1 回だけの同じ日のやり直し (--execute のときだけ。"
        "T6.3.3c より前の記録の日だけに効き、上限は前の呼び出しを数えたまま)",
    )
    args = parser.parse_args(argv)
    same_day_retry = None
    if args.same_day_retry is not None:
        if not args.execute:
            parser.error("--allow-same-day-growth-retry needs --execute")
        from datetime import date as _date

        try:
            same_day_retry = _date.fromisoformat(args.same_day_retry)
        except ValueError:
            parser.error("--allow-same-day-growth-retry needs a date (YYYY-MM-DD)")

    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    if settings is None:
        from app.config.settings import get_settings

        settings = get_settings()
    from app.operations.policy import get_policy
    from app.services.threads_openai_provider import build_responses_client

    options = {
        # client を作るだけでは呼ばない (PLAN でも本当の判断を出すために作る)。
        "client": build_responses_client(settings),
        "collect_followers": args.collect_followers,
        "same_day_retry": same_day_retry,
        **(overrides or {}),
    }
    if args.collect_followers and "threads_service" not in options:
        from app.social.threads.service import ThreadsService

        options["threads_service"] = ThreadsService(settings)
    now = now or datetime.now(UTC)
    with session_factory() as session:
        service = ThreadsGrowthService(session, timezone=get_policy().timezone, **options)
        result = (
            service.maintain(now=now, execute=True) if args.execute else service.plan(now=now)
        )
    print(f"mode                = {'EXECUTE' if args.execute else 'PLAN'}")
    keys = (
        "date_jst", "due", "reason", "active_proposal", "published_today",
        "follower_target", "follower_target_reached", "created", "model_calls",
        "model_calls_today", "model_call_budget", "outcome", "strategy", "same_day_retry",
    )  # fmt: skip
    for key in keys:
        if key in result:
            print(f"{key:<20}= {result[key]}")
    if not args.execute:
        print("plan only: no database write, no OpenAI call, no Threads call")
    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
