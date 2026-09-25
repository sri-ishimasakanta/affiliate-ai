"""管理用 CLI: W1.4 featured image を 1 記事ずつ WordPress に設定する。

    # 対象と状態を確かめる (読むだけ。既定)
    uv run python scripts/apply_featured_image.py plan

    # W1.5: 21 枚の manifest (scripts/plan_featured_image_rollout.py が作る) を使う
    uv run python scripts/apply_featured_image.py --dir artifacts/featured-images/w1.5 plan

    # 全 post の状態を保存する (読むだけ。前後比較に使う)
    uv run python scripts/apply_featured_image.py snapshot --out before.json

    # 1 記事にだけ適用する (upload → alt/title → featured_media → read-back)
    uv run python scripts/apply_featured_image.py apply --slug ai-business-efficiency --execute

    # 前後の状態を比べる (読むだけ)
    uv run python scripts/apply_featured_image.py compare --before before.json --after after.json

認証は既存の ``WordPressClient`` (設定の application password を BasicAuth で渡す) だけを
使う。credential は表示しない。1 回の ``apply`` で書き換える post は 1 件だけ。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.wordpress.featured_image import (  # noqa: E402
    FeaturedImageError,
    apply_one,
    compare_snapshots,
    load_manifest,
    media_action_problem,
    resolve_post,
    snapshot_posts,
    verify_existing_media,
    verify_local,
)

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "artifacts" / "featured-images" / "pilot"
EXIT_OK = 0
EXIT_STOPPED = 2


def main(argv: list[str] | None = None, *, client=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan")
    snap = commands.add_parser("snapshot")
    snap.add_argument("--out", type=Path, required=True)
    apply = commands.add_parser("apply")
    apply.add_argument("--slug", required=True)
    apply.add_argument("--execute", action="store_true")
    apply.add_argument(
        "--media-id",
        type=int,
        default=None,
        help="upload せず、media library にある承認済みの画像 (SHA-256 一致) を使う",
    )
    compare = commands.add_parser("compare")
    compare.add_argument("--before", type=Path, required=True)
    compare.add_argument("--after", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "compare":
        result = compare_snapshots(
            json.loads(args.before.read_text(encoding="utf-8")),
            json.loads(args.after.read_text(encoding="utf-8")),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return EXIT_OK

    if client is None:
        from app.config.settings import get_settings
        from app.wordpress.client import WordPressClient

        client = WordPressClient(get_settings())
    items = load_manifest(args.dir / "wordpress-apply-manifest.json")

    if args.command == "snapshot":
        rows = snapshot_posts(client)
        args.out.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", "utf-8")
        print(f"snapshot: {len(rows)} posts -> {args.out}")
        return EXIT_OK

    if args.command == "plan":
        ready = 0
        for item in items:
            try:
                ready += _plan_one(client, item, args.dir)
            except FeaturedImageError as exc:
                print(f"article {item.article_id} {item.slug}: STOP ({exc})")
        print(f"\n{ready}/{len(items)} ready. PLAN only: nothing was uploaded or changed.")
        return EXIT_OK

    item = next((i for i in items if i.slug == args.slug), None)
    if item is None:
        print(f"refused: {args.slug} is not in the apply manifest")
        return EXIT_STOPPED
    problem = media_action_problem(item, args.media_id)
    if problem:
        print(f"refused: {problem}")
        return EXIT_STOPPED
    if not args.execute:
        verify_local(item, args.dir)
        post = resolve_post(client, item)
        print(f"would apply {item.file} to post {post['id']} ({item.slug}); add --execute")
        return EXIT_OK
    try:
        record = apply_one(
            client, item, args.dir, args.dir / "applied", existing_media_id=args.media_id
        )
    except Exception as exc:
        print(f"STOPPED: {type(exc).__name__}: {exc}")
        print(f"record: {args.dir / 'applied' / (item.slug + '.json')}")
        return EXIT_STOPPED
    print(
        json.dumps(
            {
                k: record[k]
                for k in (
                    "slug",
                    "wordpress_post_id",
                    "original_featured_media",
                    "media_id",
                    "media_url",
                    "media_reused",
                    "final_featured_media",
                    "post_link",
                    "steps",
                    "result",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return EXIT_OK


def _plan_one(client, item, directory: Path) -> int:
    """1 記事の PLAN (読むだけ)。書く内容をすべて表示する。適用できる状態なら 1。"""

    problem = media_action_problem(item, item.planned_media_id)
    if problem:
        raise FeaturedImageError(problem)
    verify_local(item, directory)
    post = resolve_post(client, item)
    if item.media_action == "reuse":
        verify_existing_media(client, item, item.planned_media_id)
        media = f"reuse media {item.planned_media_id} (byte-identical, verified now)"
        change = f"featured_media {post.get('featured_media')} -> {item.planned_media_id}"
    else:
        media = f"upload {item.file} once"
        change = f"featured_media {post.get('featured_media')} -> <new media id>"
    print(
        f"article {item.article_id} {item.slug}: post {post['id']} status={post['status']} "
        f"title matches\n"
        f"    local {item.source or item.file} sha256={item.sha256[:16]}… "
        f"{item.width}x{item.height} OK\n"
        f"    media: {media}\n"
        f"    alt_text: {item.alt_text}\n"
        f"    media title: {item.title} アイキャッチ\n"
        f"    change: {change} (no write performed)"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
