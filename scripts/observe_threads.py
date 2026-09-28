"""手動のパイロット用 CLI: 外の Threads を **読むだけ** で観察する (T6.5B)。

    uv run python scripts/observe_threads.py --login
    uv run python scripts/observe_threads.py --for-you --headed --screenshots
    uv run python scripts/observe_threads.py --search "生成AI" --trending
    uv run python scripts/observe_threads.py --account someone --dry-run

- **読むだけ。** いいね・返信・フォロー・再投稿・引用・DM・投稿はしない (その操作は、ブラウザの
  操作の型に存在しない)。
- **定期の実行はしない** (Windows のスケジュールに登録しない。人が手で実行する)。
- 件数には上限がある (``selectors.LIMITS``)。``--limit-total`` は上限を下げることだけができる。
- ログインは ``--login`` で開いた画面で **人が** 行う。CAPTCHA・確認の画面を自動で越えない。
  ログインが要ると分かったら、何も保存せずに止まる。
- ブラウザのプロファイルは ``data/threads-observer/browser-profile`` (git に入らない)。
  画面の保存は ``artifacts/threads-observer/YYYY-MM-DD/`` (git に入らない)。
- 保存するのは観察の表 (migration ``2cfa0ccb2059``) だけ。表が無い DB では止まる。
  ``--dry-run`` は何も保存せず、取れた件数だけを表示する。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.threads_observer import RUN_DOM_UNRECOGNIZED, RUN_LOGIN_REQUIRED  # noqa: E402
from app.social.threads.observer import selectors as sel  # noqa: E402
from app.social.threads.observer.collector import CollectionPlan, collect  # noqa: E402
from app.social.threads.observer.driver import DEFAULT_PROFILE_DIR, ObserverError  # noqa: E402

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_LOGIN_REQUIRED = 3
EXIT_DOM_UNRECOGNIZED = 4
EXIT_UNAVAILABLE = 5
ARTIFACT_ROOT = Path("artifacts/threads-observer")


def _plan(args) -> CollectionPlan:
    return CollectionPlan(
        for_you=args.for_you,
        trending=args.trending,
        search_queries=tuple(args.search or ()),
        custom_feeds=tuple(args.custom_feed or ()),
        known_accounts=tuple(a.lstrip("@") for a in (args.account or ())),
        screenshots=args.screenshots,
    )


def _summary(result) -> dict:
    return {
        "status": result.status,
        "reason": result.reason,
        "source_types": result.source_types,
        "items_collected": len(result.posts),
        "items_rejected": result.rejected,
        "pages_opened": result.pages_opened,
        "scrolls": result.scrolls,
        "trending_topics": [name for name, _ in result.trending_topics],
        "screenshots": result.screenshots,
        "selector_version": sel.SELECTOR_VERSION,
        "selector_verified": sel.SELECTOR_VERIFIED,
    }


def _post_summary(item) -> dict:
    """照合用の 1 件 (公開の名前・キー・数・特徴。**本文そのものは出さない**)。"""

    record = item.record
    return {
        "source_type": item.source_type,
        "external_post_key": record.external_post_key,
        "author_handle": record.author_handle,
        "post_timestamp": record.post_timestamp.isoformat() if record.post_timestamp else None,
        "body_length": record.features.get("body_length"),
        "body_lines": record.body_text.count("\n") + 1,
        "topic": record.topic,
        "media_type": record.media_type,
        "has_link": record.has_link,
        "likes": record.likes,
        "replies": record.replies,
        "reposts": record.reposts,
        "shares": record.shares,
        "cta_class": record.features.get("cta_class"),
        "structure_class": record.features.get("structure_class"),
    }


def main(argv: list[str] | None = None, *, session_factory=None, page_factory=None,
         now: datetime | None = None) -> int:  # fmt: skip
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--login", action="store_true",
                        help="画面を開き、人がログインしてブラウザを閉じるまで待つ (観察しない)")
    parser.add_argument("--for-you", action="store_true", help="おすすめ (For You)")
    parser.add_argument("--trending", action="store_true", help="トレンドのトピック")
    parser.add_argument("--search", action="append", metavar="QUERY", help="検索 (繰り返し可)")
    parser.add_argument("--custom-feed", action="append", metavar="ID",
                        help="カスタムフィード (繰り返し可)")
    parser.add_argument("--account", action="append", metavar="HANDLE",
                        help="知っているアカウント (繰り返し可)")
    parser.add_argument("--limit-total", type=int, help="1 回の合計の上限を下げる")
    parser.add_argument("--screenshots", action="store_true", help="ページごとに画面を保存する")
    parser.add_argument("--headed", action="store_true", help="画面を表示して観察する")
    parser.add_argument("--dry-run", action="store_true", help="保存しない")
    parser.add_argument("--show-posts", action="store_true",
                        help="画面と照らすため、投稿ごとの取り出した値を出す (本文は長さだけ)")
    parser.add_argument("--profile-dir", default=str(DEFAULT_PROFILE_DIR))
    args = parser.parse_args(argv)
    profile = Path(args.profile_dir)

    if page_factory is None:
        from app.social.threads.observer.driver import PlaywrightPage

        page_factory = PlaywrightPage

    if args.login:
        try:
            page = page_factory(profile_dir=profile, headless=False)
        except ObserverError as exc:
            print(f"observer unavailable: {exc}")
            return EXIT_UNAVAILABLE
        print("ブラウザで Threads に人がログインしてください。終わったらブラウザを閉じます。"
              " (パスワードはこの CLI に入力しない。CAPTCHA は人が行う)")  # fmt: skip
        try:
            page.wait_for_human()
        finally:
            page.close()
        return EXIT_OK

    plan = _plan(args)
    if not plan.source_types():
        parser.error("choose at least one source (--for-you / --trending / --search / "
                     "--custom-feed / --account)")  # fmt: skip
    limits = dict(sel.LIMITS)
    if args.limit_total is not None:
        if args.limit_total < 1:
            parser.error("--limit-total must be >= 1")
        limits["run_total"] = min(int(args.limit_total), sel.LIMITS["run_total"])

    if session_factory is None and not args.dry_run:
        from app.config.database import SessionLocal

        session_factory = SessionLocal
    if not args.dry_run:
        from app.services.threads_trend_analysis_service import observer_tables_present

        with session_factory() as session:
            present = observer_tables_present(session)
        if not present:
            print(
                "observer tables are missing in this database (migration 2cfa0ccb2059 is "
                "not applied); nothing was opened. use --dry-run for a no-store pilot"
            )
            return EXIT_UNAVAILABLE

    now = now or datetime.now(UTC)
    shot_dir = ARTIFACT_ROOT / now.strftime("%Y-%m-%d") / now.strftime("%H%M%S")
    try:
        page = page_factory(profile_dir=profile, headless=not args.headed)
    except ObserverError as exc:
        print(f"observer unavailable: {exc}")
        return EXIT_UNAVAILABLE
    try:
        result = collect(page, plan, limits=limits,
                         screenshot_dir=shot_dir if args.screenshots else None)  # fmt: skip
    finally:
        close = getattr(page, "close", None)
        if close:
            close()

    summary = _summary(result)
    if not args.dry_run:
        from app.services.threads_observer_service import record_run

        with session_factory() as session:
            run = record_run(session, result)
            summary["run_id"] = run.id
    summary["stored"] = not args.dry_run
    if args.show_posts:
        summary["posts"] = [_post_summary(item) for item in result.posts]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if result.status == RUN_LOGIN_REQUIRED:
        print("human login required: run `uv run python scripts/observe_threads.py --login`")
        return EXIT_LOGIN_REQUIRED
    if result.status == RUN_DOM_UNRECOGNIZED:
        print(f"page layout not recognized ({sel.SELECTOR_VERSION}); nothing was stored")
        return EXIT_DOM_UNRECOGNIZED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
