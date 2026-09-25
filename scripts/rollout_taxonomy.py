"""管理用 CLI: W2 のカテゴリ整理を本番に 1 項目ずつ適用する (既定は **読むだけ**)。

    uv run python scripts/rollout_taxonomy.py status                       # 進み具合 (手元の記録)
    uv run python scripts/rollout_taxonomy.py plan                         # 次の 1 項目を確かめる
    uv run python scripts/rollout_taxonomy.py create-next-category         # 次の子カテゴリの PLAN
    uv run python scripts/rollout_taxonomy.py create-next-category --execute   # 1 つだけ作る
    uv run python scripts/rollout_taxonomy.py next                         # 次の記事の PLAN
    uv run python scripts/rollout_taxonomy.py next --execute               # 1 記事だけ書く
    uv run python scripts/rollout_taxonomy.py snapshot --out before.json   # 読むだけ
    uv run python scripts/rollout_taxonomy.py compare --before b.json --after a.json

入力は ``scripts/plan_taxonomy.py`` の計画 (``artifacts/taxonomy/w2-category-plan.json``)。
記録は ``artifacts/taxonomy/rollout/`` (git 管理外): ``category-<key>.json`` と
``article-<id>.json``。子カテゴリの ID は作ったときの読み戻しから記録し、推測しない。

- 子カテゴリは計画の順に 1 つずつ作る。計画とちょうど同じ名前・slug・親のカテゴリが既に
  あれば作らずに使う (記録だけ)。名前か slug だけが重なるものは止める。
- 記事は 5 つの子がそろってから、承認した順に 1 記事ずつ。``{"categories": [親, 子]}`` だけを
  送る。article 1 には書かない。書いたら読み戻しと公開ページ (URL・canonical・パンくず・
  ラベル・子と親のアーカイブ・カテゴリのサイトマップ) を確かめる。子がパンくずやラベルに
  出なければ止める (子だけの方式に自動で切り替えない)。
- 書く前に ``in_progress`` を記録する。止まった・途中の項目があれば、次へは進まない。
  うまくいった項目を繰り返さない。
- ``modified_gmt`` が変わるのは想定どおり (人が許容済み)。戻さない。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.wordpress.taxonomy_plan import (  # noqa: E402
    CHILDREN,
    PARENT_ONLY_ARTICLE_IDS,
    TAXONOMY_VERSION,
)
from app.wordpress.taxonomy_rollout import (  # noqa: E402
    ARTICLE_ORDER,
    CHILD_ORDER,
    ROLLOUT_VERSION,
    TaxonomyRolloutStop,
    archive_links,
    breadcrumb,
    canonical,
    categories_payload,
    category_create_payload,
    category_labels,
    category_readback_problems,
    check_parent,
    child_by_key,
    child_for_article,
    desired_categories,
    post_fingerprint,
    post_pre_problems,
    post_readback_problems,
    public_problems,
    resolve_child,
    sitemap_locs,
    unexpected_categories,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PLAN = ROOT / "artifacts" / "taxonomy" / "w2-category-plan.json"
DEFAULT_STATE = ROOT / "artifacts" / "taxonomy" / "rollout"
CATEGORY_SITEMAP = "/wp-sitemap-taxonomies-category-1.xml"
DONE_CATEGORY = ("created", "reused")
DONE_ARTICLE = ("applied",)
EXIT_OK = 0
EXIT_STOPPED = 2


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def default_http_get(url: str, *, bust: bool) -> tuple[int, str]:
    from scripts.rollout_featured_image import default_http_get as get

    return get(url, bust=bust)


# == plan / state ================================================================
def load_plan(path: Path) -> dict:
    plan = json.loads(path.read_text(encoding="utf-8"))
    if plan.get("taxonomy_version") != TAXONOMY_VERSION or plan.get("ready") is not True:
        raise TaxonomyRolloutStop("the W2 plan is not ready (re-run scripts/plan_taxonomy.py)")
    if plan.get("assignment_model") != "parent_and_child":
        raise TaxonomyRolloutStop("the W2 plan is not the parent + child model")
    planned = [
        (c["key"], c["name"], c["slug"], tuple(c["article_ids"])) for c in plan["planned_children"]
    ]
    code = [(c["key"], c["name"], c["slug"], tuple(c["article_ids"])) for c in CHILDREN]
    if planned != code:
        raise TaxonomyRolloutStop("the W2 plan's children differ from the code (re-run the plan)")
    if sorted(r["article_id"] for r in plan["articles"]) != list(range(1, 26)):
        raise TaxonomyRolloutStop("the W2 plan does not cover exactly articles 1-25")
    return plan


def _record_path(state: Path, kind: str, key) -> Path:
    return state / f"{kind}-{key}.json"


def _read(path: Path) -> dict | None:
    return json.loads(path.read_text("utf-8")) if path.exists() else None


def _write(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def progress(state: Path) -> dict:
    def split(kind, keys, done):
        out = {"done": [], "blocked": [], "pending": []}
        for key in keys:
            record = _read(_record_path(state, kind, key))
            if record is None:
                out["pending"].append(key)
            elif record.get("result") in done:
                out["done"].append(key)
            else:
                out["blocked"].append(key)
        return out

    return {
        "categories": split("category", CHILD_ORDER, DONE_CATEGORY),
        "articles": split("article", ARTICLE_ORDER, DONE_ARTICLE),
    }


def resolved_child_ids(state: Path) -> dict[str, int]:
    ids = {}
    for key in CHILD_ORDER:
        record = _read(_record_path(state, "category", key)) or {}
        if record.get("result") in DONE_CATEGORY:
            ids[key] = int(record["category_id"])
    return ids


def _next(state: Path, kind: str) -> object | None:
    snap = progress(state)
    for part in ("categories", "articles"):
        if snap[part]["blocked"]:
            raise TaxonomyRolloutStop(
                f"{part[:-1]} {snap[part]['blocked'][0]} is stopped or in progress; a human must "
                "resolve it first"
            )
    part = snap["categories" if kind == "category" else "articles"]
    order = list(CHILD_ORDER if kind == "category" else ARTICLE_ORDER)
    if part["done"] != order[: len(part["done"])]:
        raise TaxonomyRolloutStop(f"completed {kind} items are out of order: {part['done']}")
    return part["pending"][0] if part["pending"] else None


# == categories ==================================================================
def create_next_category(client, plan: dict, state: Path, *, execute: bool) -> dict:
    key = _next(state, "category")
    if key is None:
        return {"result": "nothing_to_do", "reason": "all 5 child categories are resolved"}
    child = child_by_key(key)
    parent = plan["parent_category"]
    log = {
        "version": ROLLOUT_VERSION,
        "kind": "category",
        "key": key,
        "name": child["name"],
        "slug": child["slug"],
        "parent_id": parent["id"],
        "execute": execute,
        "started_at": _now(),
        "stage": "pre",
    }
    path = _record_path(state, "category", key)
    try:
        categories = client.list_categories()
        check_parent(categories, parent_id=parent["id"], parent_slug=parent["slug"])
        known = resolved_child_ids(state)
        existing = resolve_child(categories, child, parent_id=parent["id"])
        allowed_extra = {existing["id"]} if existing else set()
        extra = [
            c
            for c in unexpected_categories(categories, parent_id=parent["id"], child_ids=known)
            if c["id"] not in allowed_extra
        ]
        if extra:
            raise TaxonomyRolloutStop(f"unexpected categories exist: {extra}")
        if existing:
            log.update(stage="reuse", category_id=existing["id"], existing=existing)
            if not execute:
                log["result"] = "ready"
                log["plan"] = f"reuse existing category {existing['id']} (exact name/slug/parent)"
                return log
            log["result"] = "reused"
            _write(path, {**log, "finished_at": _now()})
            return log
        payload = category_create_payload(child, parent_id=parent["id"])
        log["payload"] = json.loads(payload)
        log["stage"] = "plan"
        if not execute:
            log["result"] = "ready"
            log["plan"] = f"POST /wp/v2/categories {payload}"
            return log
        log["stage"] = "create"
        _write(path, {**log, "result": "in_progress"})
        created = client.create_category_exact(payload, expected_parent_id=parent["id"])
        log["category_id"] = created["id"]
        log["create_response"] = {
            k: created.get(k) for k in ("id", "name", "slug", "parent", "count")
        }
        _write(path, {**log, "result": "in_progress"})
        log["stage"] = "readback"
        after = client.list_categories()
        term = next((c for c in after if c.get("id") == created["id"]), None)
        if term is None:
            raise TaxonomyRolloutStop(f"category {created['id']} is not in the category list")
        problems = category_readback_problems(term, child, parent_id=parent["id"])
        if term.get("count") not in (0, None):
            problems.append(f"new category count is {term.get('count')}")
        check_parent(after, parent_id=parent["id"], parent_slug=parent["slug"])
        extra = unexpected_categories(
            after, parent_id=parent["id"], child_ids={**known, key: created["id"]}
        )
        if extra:
            problems.append(f"unexpected categories after create: {extra}")
        if problems:
            raise TaxonomyRolloutStop(f"readback: {problems}")
        log["readback"] = {k: term.get(k) for k in ("id", "name", "slug", "parent", "count")}
        log["result"] = "created"
    except Exception as exc:
        log["result"] = "stopped"
        log["reason"] = (
            str(exc) if isinstance(exc, TaxonomyRolloutStop) else f"{type(exc).__name__}: {exc}"
        )
    log["finished_at"] = _now()
    if execute:
        _write(path, log)
    return log


# == articles ====================================================================
def _archive_contains(http_get, url: str, link: str) -> bool:
    for page in range(1, 11):
        status, html = http_get(url if page == 1 else f"{url}page/{page}/", bust=True)
        if status != 200:
            return False
        if link in archive_links(html):
            return True
    return False


def public_state(http_get, *, site: str, link: str, parent_url: str, child_url: str) -> dict:
    status, html = http_get(link, bust=True)
    _, sitemap = http_get(f"{site}{CATEGORY_SITEMAP}", bust=True)
    return {
        "article_status": status,
        "canonical": canonical(html) if status == 200 else None,
        "breadcrumb": breadcrumb(html) if status == 200 else [],
        "labels": category_labels(html) if status == 200 else [],
        "in_child_archive": _archive_contains(http_get, child_url, link),
        "in_parent_archive": _archive_contains(http_get, parent_url, link),
        "category_sitemap": sitemap_locs(sitemap),
    }


def next_article(
    client,
    plan: dict,
    state: Path,
    *,
    execute: bool,
    http_get=default_http_get,
    sleep=time.sleep,
    attempts: int = 4,
    wait: float = 20,
) -> dict:
    snap = progress(state)
    if snap["categories"]["pending"] or snap["categories"]["blocked"]:
        raise TaxonomyRolloutStop("all 5 child categories must be resolved before any article")
    article_id = _next(state, "article")
    if article_id is None:
        return {"result": "nothing_to_do", "reason": "all 24 articles are applied"}
    if article_id in PARENT_ONLY_ARTICLE_IDS:  # 順番の表に入っていないが、念のため
        raise TaxonomyRolloutStop(f"article {article_id} is parent-only")
    row = next(r for r in plan["articles"] if r["article_id"] == article_id)
    child = child_for_article(article_id)
    parent = plan["parent_category"]
    child_ids = resolved_child_ids(state)
    child_id = child_ids[child["key"]]
    site = client.target_base_url
    log = {
        "version": ROLLOUT_VERSION,
        "kind": "article",
        "article_id": article_id,
        "wordpress_post_id": row["wordpress_post_id"],
        "slug": row["slug"],
        "child": {
            "key": child["key"],
            "name": child["name"],
            "slug": child["slug"],
            "id": child_id,
        },
        "original_categories": row["current_category_ids"],  # 巻き戻し: {"categories": [4]}
        "execute": execute,
        "started_at": _now(),
        "stage": "pre",
    }
    path = _record_path(state, "article", article_id)
    try:
        categories = client.list_categories()
        check_parent(categories, parent_id=parent["id"], parent_slug=parent["slug"])
        for key, cid in child_ids.items():
            term = resolve_child(categories, child_by_key(key), parent_id=parent["id"])
            if term is None or term["id"] != cid:
                raise TaxonomyRolloutStop(f"child {key} is not category {cid} any more")
        extra = unexpected_categories(categories, parent_id=parent["id"], child_ids=child_ids)
        if extra:
            raise TaxonomyRolloutStop(f"unexpected categories exist: {extra}")
        terms = {c.get("id"): c for c in categories}
        archive = {"parent": terms[parent["id"]].get("link"), "child": terms[child_id].get("link")}
        if not all(str(u or "").startswith(f"{site}/category/") for u in archive.values()):
            raise TaxonomyRolloutStop(f"category archive URLs are not under the site: {archive}")
        log["archive_urls"] = archive
        found = client.find_published_posts_by_slug(row["slug"])
        if [p.get("id") for p in found] != [row["wordpress_post_id"]]:
            raise TaxonomyRolloutStop(f"slug resolves to {[p.get('id') for p in found]}")
        before = client.get_post(row["wordpress_post_id"])
        problems = post_pre_problems(row, before, parent_id=parent["id"], base_url=site)
        if problems:
            raise TaxonomyRolloutStop(f"pre-check: {problems}")
        desired = desired_categories(parent_id=parent["id"], child_id=child_id)
        payload = categories_payload(
            list(before.get("categories") or []), desired, parent_id=parent["id"]
        )
        log.update(
            stage="plan",
            payload=json.loads(payload),
            before=post_fingerprint(before),
            before_categories=before.get("categories"),
            before_modified_gmt=before.get("modified_gmt"),
        )
        if not execute:
            log["result"] = "ready"
            log["plan"] = f"POST /wp/v2/posts/{row['wordpress_post_id']} {payload}"
            return log
        log["stage"] = "write"
        _write(path, {**log, "result": "in_progress"})
        client.set_post_categories_exact(row["wordpress_post_id"], payload)
        log["stage"] = "readback"
        after = client.get_post(row["wordpress_post_id"])
        log["after_categories"] = after.get("categories")
        log["after_modified_gmt"] = after.get("modified_gmt")
        problems = post_readback_problems(before, after, expected=desired)
        if problems:
            raise TaxonomyRolloutStop(f"readback: {problems}")
        log["stage"] = "public"
        for attempt in range(1, attempts + 1):
            rendered = public_state(
                http_get,
                site=site,
                link=row["link"],
                parent_url=archive["parent"],
                child_url=archive["child"],
            )
            problems = public_problems(
                rendered,
                link=row["link"],
                parent_url=archive["parent"],
                child_url=archive["child"],
                child_id=child_id,
            )
            log["public"] = {"attempt": attempt, "rendered": rendered, "problems": problems}
            if not problems:
                break
            if attempt < attempts:
                sleep(wait)
        if problems:
            raise TaxonomyRolloutStop(f"public: {problems}")
        log["stage"] = "done"
        log["result"] = "applied"
    except Exception as exc:
        log["result"] = "stopped"
        log["reason"] = (
            str(exc) if isinstance(exc, TaxonomyRolloutStop) else f"{type(exc).__name__}: {exc}"
        )
    log["finished_at"] = _now()
    if execute:
        _write(path, log)
    return log


# == snapshot / compare ==========================================================
def snapshot(client) -> dict:
    categories = [
        {k: c.get(k) for k in ("id", "name", "slug", "parent", "count")}
        for c in client.list_categories()
    ]
    posts = []
    for p in client.list_post_states():
        title = p.get("title")
        posts.append(
            {
                "id": p.get("id"),
                "slug": p.get("slug"),
                "status": p.get("status"),
                "title": title.get("raw") if isinstance(title, dict) else title,
                "categories": list(p.get("categories") or []),
                "tags": list(p.get("tags") or []),
                "date_gmt": p.get("date_gmt"),
                "modified_gmt": p.get("modified_gmt"),
                "link": p.get("link"),
            }
        )
    return {
        "taken_at": _now(),
        "categories": sorted(categories, key=lambda c: c["id"]),
        "posts": sorted(posts, key=lambda p: p["id"]),
    }


def compare(before: dict, after: dict) -> dict:
    old_c = {c["id"]: c for c in before["categories"]}
    new_c = {c["id"]: c for c in after["categories"]}
    old_p = {p["id"]: p for p in before["posts"]}
    new_p = {p["id"]: p for p in after["posts"]}
    changed_posts = {}
    for pid in sorted(old_p.keys() | new_p.keys()):
        a, b = old_p.get(pid) or {}, new_p.get(pid) or {}
        fields = sorted(k for k in (a or b) if a.get(k) != b.get(k))
        if fields:
            changed_posts[pid] = fields
    return {
        "categories_added": [new_c[i] for i in sorted(new_c.keys() - old_c.keys())],
        "categories_removed": [old_c[i] for i in sorted(old_c.keys() - new_c.keys())],
        "categories_changed": {
            i: sorted(k for k in old_c[i] if old_c[i][k] != new_c[i][k] and k != "count")
            for i in sorted(old_c.keys() & new_c.keys())
            if any(old_c[i][k] != new_c[i][k] for k in old_c[i] if k != "count")
        },
        "posts_changed": changed_posts,
    }


# == CLI =========================================================================
def main(argv=None, *, client=None, http_get=default_http_get, sleep=time.sleep) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("plan")
    create = commands.add_parser("create-next-category")
    create.add_argument("--execute", action="store_true")
    nxt = commands.add_parser("next")
    nxt.add_argument("--execute", action="store_true")
    snap = commands.add_parser("snapshot")
    snap.add_argument("--out", type=Path, required=True)
    cmp_ = commands.add_parser("compare")
    cmp_.add_argument("--before", type=Path, required=True)
    cmp_.add_argument("--after", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "status":
        print(json.dumps(progress(args.state), ensure_ascii=False))
        print("local records only; run `plan` to check the live WordPress state")
        return EXIT_OK
    if args.command == "compare":
        result = compare(
            json.loads(args.before.read_text("utf-8")), json.loads(args.after.read_text("utf-8"))
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return EXIT_OK
    if client is None:
        from app.config.settings import get_settings
        from app.wordpress.client import WordPressClient

        client = WordPressClient(get_settings())
    if args.command == "snapshot":
        data = snapshot(client)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(
            f"snapshot: {len(data['categories'])} categories, "
            f"{len(data['posts'])} posts -> {args.out}"
        )
        return EXIT_OK
    try:
        plan = load_plan(args.plan)
        if args.command == "plan":
            pending = progress(args.state)["categories"]["pending"]
            log = (
                create_next_category(client, plan, args.state, execute=False)
                if pending
                else next_article(
                    client, plan, args.state, execute=False, http_get=http_get, sleep=sleep
                )
            )
        elif args.command == "create-next-category":
            log = create_next_category(client, plan, args.state, execute=args.execute)
        else:
            log = next_article(
                client, plan, args.state, execute=args.execute, http_get=http_get, sleep=sleep
            )
    except TaxonomyRolloutStop as exc:
        print(f"refused: {exc}")
        return EXIT_STOPPED
    keys = (
        "kind",
        "key",
        "article_id",
        "wordpress_post_id",
        "category_id",
        "stage",
        "result",
        "reason",
        "plan",
    )
    print(json.dumps({k: log[k] for k in keys if k in log}, ensure_ascii=False))
    if not getattr(args, "execute", False):
        print("read-only: nothing was created or changed in WordPress")
    return (
        EXIT_OK
        if log.get("result") in ("ready", "created", "reused", "applied", "nothing_to_do")
        else EXIT_STOPPED
    )


if __name__ == "__main__":
    raise SystemExit(main())
