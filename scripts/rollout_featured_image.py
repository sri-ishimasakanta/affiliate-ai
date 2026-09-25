"""管理用 CLI: W1.5 の featured image を **1 記事ずつ** 本番に適用し、その場で確かめる。

    # 次の 1 記事の事前確認と PLAN (読むだけ。既定)
    uv run python scripts/rollout_featured_image.py next

    # 次の 1 記事を適用して確かめる (書くのはこの 1 記事だけ)
    uv run python scripts/rollout_featured_image.py next --execute

    # 進み具合 (読むだけ)
    uv run python scripts/rollout_featured_image.py status

    # 公開ページの確認だけ (読むだけ。試作の記事で確かめ方を確かめる)
    uv run python scripts/rollout_featured_image.py check-public --link <URL> --stem <s> --alt <a>

書き込みは既存の ``scripts/apply_featured_image.py`` (``apply_one``) だけが行う。この道具は
その前後を固める:

1. 事前 (読むだけ): slug で 1 件・post ID・タイトル・slug・状態が計画どおり、
   ``featured_media`` と ``modified_gmt`` が計画の元の値のまま、ローカルの SHA-256、
   media library に同じ名前 (``-1`` など) も同じ byte の media も無い、試作 4 件の
   featured_media と media 95 / 99 が変わっていない。
2. apply の道具の PLAN (``apply --slug``) → ``--execute`` のときだけ ``apply --slug --execute``
   (upload 1 回 → alt / title → ``featured_media`` → 読み戻し)。
3. 事後 (読むだけ): REST の読み戻し (新しい media・WebP・1200×675・ファイル名・alt・title・
   byte が承認済み、post の他の項目は変わらず、``modified_gmt`` は変わる)、公開ページ
   (アイキャッチ・alt・og:image・twitter:image・summary_large_image・カテゴリ一覧のカード)。

**順番を守る**: 承認した順 (manifest の ``rollout_order``) の次の 1 記事だけを扱う。前の記事が
失敗していれば、次の記事には進まない (人が確かめる)。まとめて書く命令は無い。
公開ページはキャッシュを避けた URL (クエリ付き) で確かめ、ふつうの URL の結果は記録だけ。
キャッシュの消去や設定の変更はしない。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import re
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.wordpress.featured_image import (  # noqa: E402
    FeaturedImageError,
    load_manifest,
    post_fingerprint,
    verify_local,
)
from app.wordpress.featured_image_rollout import (  # noqa: E402
    PILOT_MAPPING,
    filename_matches,
)
from scripts import apply_featured_image  # noqa: E402

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "artifacts" / "featured-images" / "w1.5"
CATEGORY_URL = "https://bizfluxlab.com/category/gyomu-koritsuka/"
# 触らない media: まだあり、どこにも添付されず、featured でもなく、upload のあと変更されていない。
UNTOUCHED_MEDIA_IDS = (95, 99)
USER_AGENT = "Mozilla/5.0 (bizfluxlab featured-image verification, read-only)"
EXIT_OK = 0
EXIT_STOPPED = 2


class RolloutStop(RuntimeError):
    """この記事で止める (何を書いたかは結果の記録に残す)。"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _title(post: dict) -> str | None:
    title = post.get("title")
    return title.get("raw") if isinstance(title, dict) else title


# == public pages (read-only) ===================================================
def default_http_get(url: str, *, bust: bool) -> tuple[int, str]:
    import httpx

    params = {"w15check": str(int(time.time() * 1000))} if bust else None
    response = httpx.get(
        url, params=params, timeout=30, follow_redirects=False, headers={"User-Agent": USER_AGENT}
    )
    return response.status_code, response.text


def _meta(html: str, attr: str, name: str) -> str | None:
    match = re.search(rf'<meta {attr}="{re.escape(name)}" content="([^"]*)"', html)
    return match.group(1) if match else None


def article_state(html: str) -> dict:
    eye = re.search(r'<figure class="eye-catch"[^>]*>\s*<img([^>]*)>', html, re.S)
    attrs = eye.group(1) if eye else ""
    src = re.search(r'\ssrc="([^"]+)"', attrs)
    alt = re.search(r'\salt="([^"]*)"', attrs)
    return {
        "og_image": _meta(html, "property", "og:image"),
        "twitter_image": _meta(html, "name", "twitter:image"),
        "twitter_card": _meta(html, "name", "twitter:card"),
        "eye_catch_src": src.group(1) if src else None,
        "eye_catch_alt": alt.group(1) if alt else None,
    }


def category_card(http_get, link: str, *, bust: bool) -> dict:
    for page in range(1, 11):
        url = CATEGORY_URL if page == 1 else f"{CATEGORY_URL}page/{page}/"
        status, html = http_get(url, bust=bust)
        if status != 200:
            return {"found": False, "status": status}
        for href, body in re.findall(
            r'<a href="([^"]+)" class="entry-card-wrap[^"]*"[^>]*>(.*?)</a>', html, re.S
        ):
            if href == link:
                img = re.search(r'<img[^>]+src="([^"]+)"', body)
                return {"found": True, "page": page, "img": img.group(1) if img else None}
    return {"found": False}


def _name(url) -> str:
    return PurePosixPath(str(url or "")).name


def public_check(
    http_get,
    link: str,
    stem: str,
    alt: str,
    *,
    attempts: int = 6,
    wait: float = 20,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """キャッシュを避けた URL で確かめる (これが関門)。ふつうの URL は記録だけ。"""

    result: dict = {"link": link, "stem": stem, "checked_at": _now()}
    for attempt in range(1, attempts + 1):
        status, html = http_get(link, bust=True)
        state = article_state(html) if status == 200 else {}
        card = category_card(http_get, link, bust=True)
        card_img = _name(card.get("img"))
        checks = {
            "article_200": status == 200,
            "eye_catch_is_image": _name(state.get("eye_catch_src")) == f"{stem}.webp",
            "eye_catch_alt": state.get("eye_catch_alt") == alt,
            "og_image": _name(state.get("og_image")) == f"{stem}.webp",
            "twitter_image": _name(state.get("twitter_image")) == f"{stem}.webp",
            "twitter_card_large": state.get("twitter_card") == "summary_large_image",
            "category_card_found": card.get("found") is True,
            "category_card_image": card_img.startswith(f"{stem}-") and "no-image" not in card_img,
        }
        result.update(attempt=attempt, article=state, card=card, checks=checks)
        if all(checks.values()):
            break
        if attempt < attempts:
            sleep(wait)
    status, html = http_get(link, bust=False)
    normal_card = category_card(http_get, link, bust=False)
    result["normal_url"] = {
        "article_og_image": article_state(html).get("og_image") if status == 200 else None,
        "article_eye_catch": article_state(html).get("eye_catch_src") if status == 200 else None,
        "card_img": normal_card.get("img"),
    }
    result["ok"] = all(result["checks"].values())
    return result


# == one article ================================================================
def _media_sha(client, media: dict) -> str | None:
    try:
        return hashlib.sha256(client.fetch_media_file(str(media.get("source_url")))).hexdigest()
    except Exception:
        return None


def pre_checks(client, entry: dict, directory: Path) -> dict:
    """書く前の確認 (読むだけ)。合わなければ ``RolloutStop``。"""

    posts = client.find_published_posts_by_slug(entry["slug"])
    if [p.get("id") for p in posts] != [entry["wordpress_post_id"]]:
        raise RolloutStop(
            f"slug resolves to {[p.get('id') for p in posts]}, planned post "
            f"{entry['wordpress_post_id']}"
        )
    post = posts[0]
    if _title(post) != entry["title"]:
        raise RolloutStop(f"title differs from the plan: {_title(post)!r}")
    if post.get("slug") != entry["slug"] or post.get("status") != "publish":
        raise RolloutStop(f"slug/status differ: {post.get('slug')!r} {post.get('status')!r}")
    if (post.get("featured_media") or 0) != entry["original_featured_media"]:
        raise RolloutStop(
            f"featured_media is {post.get('featured_media')}, planned original "
            f"{entry['original_featured_media']}"
        )
    if post.get("modified_gmt") != entry["original_modified_gmt"]:
        raise RolloutStop(f"modified_gmt changed since the plan: {post.get('modified_gmt')}")
    item = next(
        i
        for i in load_manifest(directory / "wordpress-apply-manifest.json")
        if i.article_id == entry["article_id"]
    )
    try:
        verify_local(item, directory)
    except FeaturedImageError as exc:
        raise RolloutStop(f"local image: {exc}") from exc
    media = client.list_media_items()
    names = [m["id"] for m in media if filename_matches(entry["file"], m)]
    same = [
        m["id"]
        for m in media
        if m.get("mime_type") == "image/webp" and _media_sha(client, m) == entry["sha256"]
    ]
    if names or same:
        raise RolloutStop(f"media collision: same name {names}, same bytes {same}")
    states = {p.get("id"): p for p in client.list_post_states()}
    pilots = {pid: (states.get(pid) or {}).get("featured_media") for pid in PILOT_MAPPING}
    if pilots != PILOT_MAPPING:
        raise RolloutStop(f"W1.4 pilot mapping changed: {pilots}")
    featured = {p.get("featured_media") for p in states.values()}
    by_id = {m["id"]: m for m in media}
    for media_id in UNTOUCHED_MEDIA_IDS:
        m = by_id.get(media_id)
        if (
            m is None
            or m.get("post") not in (None, 0)
            or media_id in featured
            or m.get("modified_gmt") != m.get("date_gmt")
        ):
            raise RolloutStop(f"media {media_id} is not in its untouched state")
    return {
        "featured_media": post.get("featured_media") or 0,
        "modified_gmt": post.get("modified_gmt"),
        "fingerprint": post_fingerprint(client.get_post(entry["wordpress_post_id"])),
        "media_ids_before": sorted(by_id),
    }


def _tool(argv: list[str], client) -> tuple[int, str]:
    """既存の apply の道具を同じ client で呼ぶ (出力を記録に残す)。"""

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        try:
            code = apply_featured_image.main(argv, client=client)
        except FeaturedImageError as exc:
            print(f"STOP ({exc})")
            code = EXIT_STOPPED
    return code, buffer.getvalue().strip()


def verify_rest(client, entry: dict, pre: dict, record: dict) -> dict:
    media_id = record["media_id"]
    post = client.get_post(entry["wordpress_post_id"])
    media = client.get_media(media_id)
    details = media.get("media_details") or {}
    title = media.get("title")
    title = title.get("raw") if isinstance(title, dict) else title
    checks = {
        "featured_media_is_new_media": post.get("featured_media") == media_id,
        "fields_unchanged": post_fingerprint(post) == pre["fingerprint"],
        "modified_gmt_changed": post.get("modified_gmt") != pre["modified_gmt"],
        "media_new": media_id not in pre["media_ids_before"],
        "media_webp": media.get("mime_type") == "image/webp",
        "media_1200x675": (details.get("width"), details.get("height")) == (1200, 675),
        "media_filename": _name(media.get("source_url")) == entry["file"],
        "media_alt": media.get("alt_text") == entry["alt_text"],
        "media_title": title == entry["media_title"],
        "media_bytes_approved": _media_sha(client, media) == entry["sha256"],
    }
    return {
        "checks": checks,
        "ok": all(checks.values()),
        "media_id": media_id,
        "source_url": media.get("source_url"),
        "link": post.get("link"),
        "modified_gmt_after": post.get("modified_gmt"),
        "date_gmt_after": post.get("date_gmt"),
    }


def run_article(
    client,
    entry: dict,
    directory: Path,
    *,
    execute: bool,
    http_get=default_http_get,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """1 記事だけ。``execute`` が False なら書かない (事前確認と PLAN だけ)。"""

    log: dict = {
        "article_id": entry["article_id"],
        "wordpress_post_id": entry["wordpress_post_id"],
        "slug": entry["slug"],
        "file": entry["file"],
        "execute": execute,
        "started_at": _now(),
        "stage": "pre",
    }
    try:
        log["pre"] = pre_checks(client, entry, directory)
        log["stage"] = "plan"
        base = ["--dir", str(directory), "apply", "--slug", entry["slug"]]
        code, output = _tool(base, client)
        log["plan_output"] = output
        if code != EXIT_OK or "would apply" not in output:
            raise RolloutStop(f"PLAN did not pass: {output}")
        if not execute:
            log["stage"] = "ready"
            log["result"] = "ready"
            return log
        log["stage"] = "apply"
        code, output = _tool([*base, "--execute"], client)
        log["apply_output"] = output
        record_path = directory / "applied" / f"{entry['slug']}.json"
        record = json.loads(record_path.read_text("utf-8")) if record_path.exists() else {}
        log["apply_record"] = record
        if code != EXIT_OK or record.get("result") != "applied":
            raise RolloutStop(f"apply stopped: {output[-500:]}")
        log["stage"] = "rest"
        rest = verify_rest(client, entry, log["pre"], record)
        log["rest"] = rest
        if not rest["ok"]:
            raise RolloutStop(f"REST readback: {[k for k, v in rest['checks'].items() if not v]}")
        log["stage"] = "public"
        stem = PurePosixPath(_name(rest["source_url"])).stem
        public = public_check(http_get, rest["link"], stem, entry["alt_text"], sleep=sleep)
        log["public"] = public
        if not public["ok"]:
            raise RolloutStop(f"public: {[k for k, v in public['checks'].items() if not v]}")
        log["stage"] = "done"
        log["result"] = "applied"
    except RolloutStop as exc:
        log["result"] = "stopped"
        log["reason"] = str(exc)
    except Exception as exc:  # 通信の失敗なども止める (どこまで進んだかは stage に残る)
        log["result"] = "stopped"
        log["reason"] = f"{type(exc).__name__}: {exc}"
    finally:
        log["finished_at"] = _now()
    return log


# == order / status ==============================================================
def _result_path(directory: Path, article_id: int) -> Path:
    return directory / "rollout" / f"article-{article_id}.json"


def progress(directory: Path, manifest: dict) -> dict:
    done, stopped = [], []
    for article_id in manifest["rollout_order"]:
        path = _result_path(directory, article_id)
        if not path.exists():
            continue
        result = json.loads(path.read_text("utf-8")).get("result")
        (done if result == "applied" else stopped).append(article_id)
    pending = [a for a in manifest["rollout_order"] if a not in done and a not in stopped]
    return {"done": done, "stopped": stopped, "pending": pending}


def next_entry(directory: Path, manifest: dict) -> dict | None:
    state = progress(directory, manifest)
    if state["stopped"]:
        raise RolloutStop(f"article {state['stopped'][0]} stopped; a human must resolve it first")
    order = manifest["rollout_order"]
    if state["done"] != order[: len(state["done"])]:
        raise RolloutStop(f"applied articles are out of order: {state['done']}")
    if not state["pending"]:
        return None
    article_id = state["pending"][0]
    return next(e for e in manifest["items"] if e["article_id"] == article_id)


def main(argv=None, *, client=None, http_get=default_http_get, sleep=time.sleep) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    commands = parser.add_subparsers(dest="command", required=True)
    nxt = commands.add_parser("next")
    nxt.add_argument("--execute", action="store_true")
    commands.add_parser("status")
    pub = commands.add_parser("check-public")
    pub.add_argument("--link", required=True)
    pub.add_argument("--stem", required=True)
    pub.add_argument("--alt", required=True)
    args = parser.parse_args(argv)

    if args.command == "check-public":
        result = public_check(http_get, args.link, args.stem, args.alt, attempts=1, sleep=sleep)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return EXIT_OK if result["ok"] else EXIT_STOPPED

    manifest = json.loads((args.dir / "wordpress-apply-manifest.json").read_text("utf-8"))
    if manifest.get("approved") is not True:
        print("refused: the apply manifest is not approved (re-run the plan)")
        return EXIT_STOPPED
    if args.command == "status":
        print(json.dumps(progress(args.dir, manifest), ensure_ascii=False))
        return EXIT_OK
    try:
        entry = next_entry(args.dir, manifest)
    except RolloutStop as exc:
        print(f"refused: {exc}")
        return EXIT_STOPPED
    if entry is None:
        print("all articles in the rollout order are applied")
        return EXIT_OK
    if client is None:
        from app.config.settings import get_settings
        from app.wordpress.client import WordPressClient

        client = WordPressClient(get_settings())
    log = run_article(client, entry, args.dir, execute=args.execute, http_get=http_get, sleep=sleep)
    if args.execute:  # 書いた (か、書こうとした) ときだけ結果を残す。読むだけの確認は残さない
        path = _result_path(args.dir, entry["article_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {
        k: log.get(k) for k in ("article_id", "wordpress_post_id", "stage", "result", "reason")
    }
    if log.get("rest"):
        summary["media_id"] = log["rest"]["media_id"]
    print(json.dumps(summary, ensure_ascii=False))
    if not args.execute:
        print("read-only: nothing was uploaded or changed (add --execute to apply this article)")
    return EXIT_OK if log.get("result") in ("ready", "applied") else EXIT_STOPPED


if __name__ == "__main__":
    raise SystemExit(main())
