"""管理用 CLI: W1.5 の承認済み 21 枚を WordPress に適用する前の計画 (**読むだけ**)。

    uv run python scripts/plan_featured_image_rollout.py

- ローカル: 各バッチのパッケージが今の W1.5 manifest の写しであること、完成の WebP が
  あり・WebP で・1200×675 で・0 バイトでなく、SHA-256 が compose の報告と承認の記録
  (review plan の表) の両方と一致すること。
- WordPress (読むだけ): 全 post、slug での検索 (manifest の slug と WordPress が返す slug の
  両方)、media library の全件と webp の実ファイル (SHA-256 を測るため)。書き込みはしない。
- 出力: ``artifacts/featured-images/w1.5/wordpress-apply-manifest.json`` (apply の道具が読む)、
  ``rollout-plan-report.json`` / ``.md``。どれも git の管理外。

1 つでも確かめられなければ、報告に書き、``ready`` を False にする (推測で埋めない)。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.wordpress.featured_image import webp_dimensions  # noqa: E402
from app.wordpress.featured_image_batch import (  # noqa: E402
    manifest_sha256,
    validate_batch_package,
)
from app.wordpress.featured_image_rollout import (  # noqa: E402
    ACTION_REUSE,
    ACTION_REVIEW,
    EXPECTED_ARTICLE_IDS,
    PILOT_MAPPING,
    UNTOUCHABLE_MEDIA_IDS,
    apply_entry,
    audit,
    classify_featured,
    parse_approved_hashes,
    plan_media,
)

MANIFEST = ROOT / "docs" / "operations" / "featured-image-w1.5-manifest.json"
REVIEW_PLAN = ROOT / "docs" / "operations" / "featured-image-w1.5-review-plan.md"
W15_DIR = ROOT / "artifacts" / "featured-images" / "w1.5"
APPLY_SCHEMA = "featured-image-wordpress-apply/1"


def _title(post: dict) -> str | None:
    title = post.get("title")
    return title.get("raw") if isinstance(title, dict) else title


def local_images(
    manifest: dict, directory: Path, approved: dict, *, digest: str
) -> tuple[list[dict], list[str]]:
    """21 枚のローカルの確認。問題は一覧で返す (1 つでもあれば計画は ready にならない)。"""

    by_id = {a["article_id"]: a for a in manifest["articles"]}
    rows, problems = [], []
    for batch in manifest["batches"]:
        batch_dir = directory / f"batch-{batch['batch']}"
        package = json.loads((batch_dir / "batch-manifest.json").read_text(encoding="utf-8"))
        for problem in validate_batch_package(package, manifest, manifest_sha256=digest):
            problems.append(f"batch {batch['batch']}: {problem}")
        report_path = batch_dir / "compose-report.json"
        report = json.loads(report_path.read_text("utf-8")) if report_path.exists() else {}
        for item in package["items"]:
            article_id = item["article_id"]
            file = item["files"]["final_webp"]
            path = batch_dir / file
            row = {
                "article_id": article_id,
                "slug": by_id[article_id]["slug"],
                "title": by_id[article_id]["title"],
                "batch": batch["batch"],
                "file": file,
                "source": f"batch-{batch['batch']}/{file}",
                "approved": False,
            }
            if not path.is_file():
                problems.append(f"article {article_id}: {file} does not exist")
                rows.append(row)
                continue
            data = path.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            row.update(sha256=sha, bytes=len(data))
            try:
                row["width"], row["height"] = webp_dimensions(data)
            except Exception as exc:  # 本物の WebP でない
                problems.append(f"article {article_id}: {file} is not a WebP ({exc})")
                rows.append(row)
                continue
            if not data or (row["width"], row["height"]) != (1200, 675):
                problems.append(f"article {article_id}: {file} is {row['width']}x{row['height']}")
            if file != by_id[article_id]["planned_file"]:
                problems.append(f"article {article_id}: {file} is not the planned file")
            composed = report.get("composed", {}).get(str(article_id), {}).get("final_webp", {})
            if composed.get("sha256") != sha:
                problems.append(f"article {article_id}: SHA-256 differs from the compose report")
            record = approved.get(article_id)
            if record is None:
                problems.append(f"article {article_id}: no human approval is recorded")
            elif record != {"file": file, "sha256": sha}:
                problems.append(f"article {article_id}: the file is not the approved file/SHA-256")
            else:
                row["approved"] = True
            rows.append(row)
    return rows, problems


def resolve_posts(client, articles: list[dict], posts: list[dict]) -> tuple[dict, list[str]]:
    """slug (manifest の値) で公開済みの post を 1 件に決め、タイトルの完全一致を確かめる。

    WordPress が返す slug でもう一度検索し、同じ 1 件になること (apply の道具が後で使う形)。
    """

    resolved, problems = {}, []
    for article in articles:
        article_id, slug = article["article_id"], article["slug"]
        matches = client.find_published_posts_by_slug(slug)
        if len(matches) != 1:
            problems.append(f"article {article_id}: {len(matches)} published posts for the slug")
            continue
        post = matches[0]
        if _title(post) != article["title"]:
            problems.append(f"article {article_id}: title mismatch ({_title(post)!r})")
            continue
        if unquote(post.get("slug") or "") != slug or post.get("status") != "publish":
            problems.append(f"article {article_id}: slug/status differ ({post.get('slug')!r})")
            continue
        again = client.find_published_posts_by_slug(post["slug"])
        if [p.get("id") for p in again] != [post["id"]]:
            problems.append(f"article {article_id}: the WordPress slug does not resolve to 1 post")
            continue
        state = next((p for p in posts if p.get("id") == post["id"]), post)
        resolved[article_id] = {**post, "modified": state.get("modified"), "link": post.get("link")}
    return resolved, problems


def media_hashes(client, media_items: list[dict]) -> dict[int, str | None]:
    """webp の media の実ファイルの SHA-256 (読めなければ None。推測しない)。"""

    hashes: dict[int, str | None] = {}
    for media in media_items:
        if media.get("mime_type") != "image/webp":
            continue
        try:
            hashes[media["id"]] = hashlib.sha256(
                client.fetch_media_file(str(media.get("source_url") or ""))
            ).hexdigest()
        except Exception:
            hashes[media["id"]] = None
    return hashes


def build_plan(
    client,
    *,
    directory: Path = W15_DIR,
    manifest_path: Path = MANIFEST,
    review_plan_path: Path = REVIEW_PLAN,
    now: datetime | None = None,
) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    digest = manifest_sha256(manifest_path)
    approved = parse_approved_hashes(review_plan_path.read_text(encoding="utf-8"))
    articles = {a["article_id"]: a for a in manifest["articles"]}
    local, problems = local_images(manifest, directory, approved, digest=digest)

    posts = client.list_post_states()
    resolved, post_problems = resolve_posts(client, list(articles.values()), posts)
    problems += post_problems
    media_items = client.list_media_items()
    hashes = media_hashes(client, media_items)
    featured_by_media: dict[int, list[int]] = {}
    for post in posts:
        if post.get("featured_media"):
            featured_by_media.setdefault(post["featured_media"], []).append(post["id"])

    entries = []
    local_by_id = {row["article_id"]: row for row in local}
    for batch in manifest["batches"]:
        for article_id in batch["article_ids"]:
            row, post = local_by_id.get(article_id), resolved.get(article_id)
            if row is None or post is None or "sha256" not in row:
                continue
            media_plan = plan_media(
                planned_file=row["file"],
                sha256=row["sha256"],
                width=row.get("width", 0),
                height=row.get("height", 0),
                media_items=media_items,
                media_sha256=hashes,
                featured_by_media=featured_by_media,
            )
            state = classify_featured(
                post.get("featured_media"), sha256=row["sha256"], media_sha256=hashes
            )
            entries.append(
                apply_entry(
                    article=articles[article_id],
                    batch=batch["batch"],
                    local=row,
                    post=post,
                    media_plan=media_plan,
                    featured_state=state,
                )
            )

    pilots = {
        pid: next((p.get("featured_media") for p in posts if p.get("id") == pid), None)
        for pid in PILOT_MAPPING
    }
    media_99 = next((m for m in media_items if m.get("id") in UNTOUCHABLE_MEDIA_IDS), None)
    media_99_state = {
        "exists": media_99 is not None,
        "attached_post": (media_99 or {}).get("post"),
        "featured_by": featured_by_media.get(99, []),
        "file": (media_99 or {}).get("source_url", "").rsplit("/", 1)[-1] if media_99 else None,
        "sha256": hashes.get(99),
        "date_gmt": (media_99 or {}).get("date_gmt"),
        "modified_gmt": (media_99 or {}).get("modified_gmt"),
    }
    # 触られていない: まだあり、どこにも添付されず、どの post の featured image でもなく、
    # upload のあと一度も変更されていない (modified_gmt が date_gmt と同じ)。
    media_99_state["untouched"] = bool(
        media_99 is not None
        and media_99_state["attached_post"] in (None, 0)
        and not media_99_state["featured_by"]
        and media_99_state["modified_gmt"] == media_99_state["date_gmt"]
    )
    result = audit(entries, pilots=pilots, media_99=media_99_state)
    if problems:
        result["ready"] = False
    counts = {
        "posts_read": len(posts),
        "media_read": len(media_items),
        "webp_media_hashed": sum(1 for v in hashes.values() if v),
        "webp_media_unreadable": sorted(k for k, v in hashes.items() if v is None),
    }
    return {
        "schema": APPLY_SCHEMA,
        "approved": result["ready"],
        "phase": "W1.5G",
        "generated_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "production_rollout_authorized": False,
        "apply_status": "planned",
        "source_manifest_sha256": digest,
        "rollout_order": [e["article_id"] for e in entries],
        "items": entries,
        "local_images": local,
        "pilot_featured_media": {str(k): v for k, v in pilots.items()},
        "media_99": media_99_state,
        "read_counts": counts,
        "problems": problems,
        "audit": result,
    }


def render_report(plan: dict) -> str:
    items = plan["items"]
    audit_result = plan["audit"]
    lines = [
        "# W1.5 featured image rollout plan (read-only)",
        "",
        f"- generated: {plan['generated_at']}",
        f"- ready: **{audit_result['ready']}** (production rollout authorized: "
        f"{plan['production_rollout_authorized']})",
        f"- source manifest sha256: `{plan['source_manifest_sha256']}`",
        f"- read: {plan['read_counts']}",
        "",
        "## Audit",
        "",
        *[f"- {'OK' if ok else 'NG'} {name}" for name, ok in audit_result["checks"].items()],
        "",
        "## Problems",
        "",
        *([f"- {p}" for p in plan["problems"]] or ["- none"]),
        "",
        "## Rollout order and targets",
        "",
        "| # | batch | article | WP post | title | file | sha256 | featured now | media plan "
        "| modified_gmt now |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for index, e in enumerate(items, start=1):
        plan_text = e["media_action"] + (
            f" {e['planned_media_id']}" if e["media_action"] == ACTION_REUSE else ""
        )
        lines.append(
            f"| {index} | {e['batch']} | {e['article_id']} | {e['wordpress_post_id']} | "
            f"{e['title']} | {e['file']} | `{e['sha256'][:12]}…` | {e['current_featured_media']} "
            f"({e['featured_state']}) | {plan_text} | {e['original_modified_gmt']} |"
        )
    review = [e for e in items if e["media_action"] == ACTION_REVIEW]
    lines += [
        "",
        "## Needs a human",
        "",
        *([f"- article {e['article_id']}: {e['media_reason']}" for e in review] or ["- none"]),
        "",
        "## Slugs that WordPress stores differently",
        "",
    ]
    lines += [
        f"- article {e['article_id']}: manifest `{e['application_slug']}` → WordPress "
        f"`{e['wordpress_slug']}` (decoded `{e['wordpress_slug_decoded']}`)"
        for e in items
        if e["application_slug"] != e["wordpress_slug"]
    ] or ["- none"]
    lines += [
        "",
        "## W1.4 pilots (read-back)",
        "",
        *[
            f"- post {pid}: featured_media {media} (expected {PILOT_MAPPING[int(pid)]})"
            for pid, media in plan["pilot_featured_media"].items()
        ],
        "",
        "## media 99",
        "",
        f"- {plan['media_99']}",
        "",
        "This plan wrote nothing to WordPress.",
    ]
    return "\n".join(lines) + "\n"


def main(argv=None, *, client=None, directory: Path = W15_DIR) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    if client is None:
        from app.config.settings import get_settings
        from app.wordpress.client import WordPressClient

        client = WordPressClient(get_settings())
    plan = build_plan(client, directory=directory)
    manifest_path = directory / "wordpress-apply-manifest.json"
    text = json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
    manifest_path.write_text(text, encoding="utf-8")
    (directory / "rollout-plan-report.json").write_text(text, encoding="utf-8")
    (directory / "rollout-plan-report.md").write_text(render_report(plan), encoding="utf-8")
    ids = [e["article_id"] for e in plan["items"]]
    print(f"entries: {len(ids)} / {len(EXPECTED_ARTICLE_IDS)}; ready: {plan['audit']['ready']}")
    for problem in plan["problems"]:
        print(f"problem: {problem}")
    for name, ok in plan["audit"]["checks"].items():
        print(f"{'OK' if ok else 'NG'} {name}")
    print(f"wrote {manifest_path}")
    print("read-only: nothing was uploaded or changed in WordPress")
    return 0 if plan["audit"]["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
