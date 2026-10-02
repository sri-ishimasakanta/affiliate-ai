"""管理用 CLI: 自アカウントの Threads 投稿の台帳 (manual-post coexistence、2026-10-02)。

    uv run python scripts/manage_threads_account_posts.py list [--origin manual]
    uv run python scripts/manage_threads_account_posts.py show 12          # 出来事の履歴つき
    uv run python scripts/manage_threads_account_posts.py dataset     # 分析の材料 (origin)

    # 人が manual / unknown の投稿を分類する (既定は PLAN。値は上書きせず出来事を追記)
    uv run python scripts/manage_threads_account_posts.py classify 12 --kind growth
        --by human --reason "follow / interact post written in the app" [--execute]

    # 一覧を 1 回読んで照合する (既定は PLAN = 読まない。--execute で Threads を **読む**)
    uv run python scripts/manage_threads_account_posts.py discover [--execute]

Threads へは書かない (投稿・編集・削除をしない)。manual 投稿は公開の本数・承認・Growth の枠に
入らない。migration ``3d5382e2a6bd`` の前の DB では止まる。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

EXIT_OK, EXIT_REFUSED = 0, 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list")
    listing.add_argument("--origin", choices=("system", "manual", "unknown"))
    show = sub.add_parser("show")
    show.add_argument("account_post_id", type=int)
    sub.add_parser("dataset")
    classify = sub.add_parser("classify")
    classify.add_argument("account_post_id", type=int)
    classify.add_argument("--kind", choices=("normal", "growth"), required=True)
    classify.add_argument("--by", required=True)
    classify.add_argument("--reason", required=True)
    classify.add_argument("--execute", action="store_true")
    discover = sub.add_parser("discover")
    discover.add_argument("--execute", action="store_true",
                          help="read the account's post list from Threads (read-only)")
    return parser


def _row(post) -> dict:
    return {"account_post_id": post.id, "threads_media_id": post.threads_media_id,
            "origin": post.origin, "origin_evidence": post.origin_evidence,
            "post_kind": post.post_kind, "publication_id": post.threads_publication_id,
            "published_at": post.published_at.isoformat() if post.published_at else None,
            "permalink": post.permalink, "missing_since": (
                post.missing_since.isoformat() if post.missing_since else None),
            "text_preview": (post.latest_text or "")[:60]}  # fmt: skip


def main(argv=None, *, session_factory=None, threads_service=None,
         now: datetime | None = None) -> int:  # fmt: skip
    from app.services.threads_account_post_service import (
        AccountPostDiscoveryError,
        AccountPostError,
        ThreadsAccountPostService,
        account_posts_ready,
    )

    args = _parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    now = now or datetime.now(UTC)
    if session_factory is None:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    with session_factory() as session:
        if not account_posts_ready(session):
            print("refused: the account post tables are missing (migration 3d5382e2a6bd not "
                  "applied)")
            return EXIT_REFUSED
        if args.command == "discover" and args.execute and threads_service is None:
            from app.config.settings import get_settings
            from app.social.threads.service import ThreadsService

            threads_service = ThreadsService(get_settings())
        from app.social.threads.policy import get_operations_policy

        service = ThreadsAccountPostService(
            session, threads_service=threads_service,
            listing_limit=get_operations_policy().account_post_listing_limit)  # fmt: skip
        try:
            if args.command == "list":
                result = [_row(p) for p in service.posts()
                          if args.origin is None or p.origin == args.origin]
            elif args.command == "show":
                post = next((p for p in service.posts() if p.id == args.account_post_id), None)
                if post is None:
                    raise AccountPostError(f"account post {args.account_post_id} does not exist")
                result = {**_row(post), "events": [
                    {"id": e.id, "event_type": e.event_type, "actor": e.actor,
                     "occurred_at": e.occurred_at.isoformat(), "detail": e.detail_json}
                    for e in service.events(post.id)]}  # fmt: skip
            elif args.command == "dataset":
                from app.operations.policy import get_policy

                result = service.dataset(now=now, tz=get_policy().timezone)
            elif args.command == "classify":
                result = service.classify(args.account_post_id, kind=args.kind, by=args.by,
                                          reason=args.reason, execute=args.execute, now=now)
            elif not args.execute:
                result = {"executed": False, "would_read": "GET /{user-id}/threads (read-only, "
                          f"up to {service._limit} posts)",
                          "note": "PLAN only; re-run with --execute to read and reconcile"}
            else:
                result = {"executed": True, **service.refresh(now=now, source="manual_cli")}
        except (AccountPostError, AccountPostDiscoveryError) as exc:
            session.rollback()
            print(f"refused: {exc}")
            return EXIT_REFUSED
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print("Threads writes: 0 (this command never posts, edits or deletes on Threads)")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
