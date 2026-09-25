"""W2: カテゴリ整理を本番に適用するときの判定 (pure。WordPress とは話さない)。

``scripts/rollout_taxonomy.py`` が WordPress と公開ページを読み、ここで判定する:

- 子カテゴリの解決: 計画の名前・slug・親とちょうど一致する既存のものは使ってよい (再利用)。
  名前か slug だけが重なるもの・親が違うものは衝突として止める。ID を推測しない。
- カテゴリを作る payload (ちょうど ``{"name","slug","parent"}``) と、作ったあとの読み戻し。
- 記事のカテゴリの payload (ちょうど ``{"categories": [親, 子]}``) と、書く前・書いたあとの確認。
  ``modified_gmt`` が変わることは **想定どおり** (人が許容済み)。公開日・本文・抜粋・状態・
  タグ・タイトル・slug・URL は変わってはいけない。
- 公開ページ: URL・canonical・パンくず・カテゴリのラベル・子と親のアーカイブ・カテゴリの
  サイトマップ。子がパンくずやラベルに出なければ止める (子だけの方式に自動で切り替えない)。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from urllib.parse import unquote

from app.wordpress.taxonomy_plan import (
    CHILDREN,
    PARENT_NAME,
    PARENT_ONLY_ARTICLE_IDS,
    drift_problems,
    permalink_problems,
    slug_problems,
)

ROLLOUT_VERSION = "w2-taxonomy-rollout/1"
# 子カテゴリを作る順 (1 つずつ)。
CHILD_ORDER = tuple(child["key"] for child in CHILDREN)
# 記事に書く順: canary (15) → 残りの子ごと。article 1 は書かない。
CANARY_ARTICLE_ID = 15
ARTICLE_ORDER = (
    15,
    13,
    4, 5, 14,
    10, 11, 16, 17, 18,
    2, 3, 6, 7, 8, 9, 12,
    19, 20, 21, 22, 23, 24, 25,
)  # fmt: skip
UNCATEGORIZED_SLUG = "uncategorized"


class TaxonomyRolloutStop(RuntimeError):
    """この項目で止める (人が確かめる)。"""


def child_by_key(key: str) -> dict:
    return next(c for c in CHILDREN if c["key"] == key)


def child_for_article(article_id: int) -> dict:
    if article_id in PARENT_ONLY_ARTICLE_IDS:
        raise TaxonomyRolloutStop(f"article {article_id} is parent-only and is never written")
    return next(c for c in CHILDREN if article_id in c["article_ids"])


def _name(term: Mapping) -> str:
    return unquote(str(term.get("name") or ""))


def _slug(term: Mapping) -> str:
    return unquote(str(term.get("slug") or ""))


def check_parent(categories: Iterable[Mapping], *, parent_id: int, parent_slug: str) -> dict:
    """親カテゴリが計画のままか (名前・ID・slug・トップレベル)。"""

    matches = [c for c in categories if _name(c) == PARENT_NAME]
    if len(matches) != 1:
        raise TaxonomyRolloutStop(f"{PARENT_NAME} does not resolve to exactly 1 category")
    parent = matches[0]
    if parent.get("id") != parent_id or _slug(parent) != parent_slug or parent.get("parent"):
        raise TaxonomyRolloutStop(
            f"parent category changed: id {parent.get('id')} slug {parent.get('slug')!r} "
            f"parent {parent.get('parent')}"
        )
    return dict(parent)


def resolve_child(categories: Iterable[Mapping], child: Mapping, *, parent_id: int) -> dict | None:
    """計画の子とちょうど一致する既存のカテゴリ (無ければ None)。重なりだけのものは止める。"""

    exact, conflicts = [], []
    for term in categories:
        same_name = _name(term) == child["name"]
        same_slug = _slug(term) == child["slug"]
        if not (same_name or same_slug):
            continue
        if same_name and same_slug and term.get("parent") == parent_id:
            exact.append(term)
        else:
            conflicts.append(
                {
                    "id": term.get("id"),
                    "name": term.get("name"),
                    "slug": term.get("slug"),
                    "parent": term.get("parent"),
                }
            )
    if conflicts:
        raise TaxonomyRolloutStop(f"category name/slug collision for {child['slug']}: {conflicts}")
    if len(exact) > 1:
        raise TaxonomyRolloutStop(f"more than one category matches {child['slug']}")
    return dict(exact[0]) if exact else None


def category_create_payload(child: Mapping, *, parent_id: int) -> str:
    problems = slug_problems(child["slug"])
    if problems or not child["name"].strip():
        raise TaxonomyRolloutStop(f"child {child['slug']} is not creatable: {problems}")
    return json.dumps(
        {"name": child["name"], "slug": child["slug"], "parent": parent_id}, ensure_ascii=False
    )


def category_readback_problems(term: Mapping, child: Mapping, *, parent_id: int) -> list[str]:
    problems = []
    if _name(term) != child["name"]:
        problems.append(f"name read back as {term.get('name')!r}")
    if _slug(term) != child["slug"]:
        problems.append(f"slug read back as {term.get('slug')!r}")
    if term.get("parent") != parent_id:
        problems.append(f"parent read back as {term.get('parent')}")
    return problems


def expected_category_ids(
    categories: Iterable[Mapping], *, parent_id: int, child_ids: Mapping[str, int]
):
    """作ったあとにあってよいカテゴリの ID (未分類・親・計画の子だけ)。"""

    allowed = {parent_id, *child_ids.values()}
    allowed |= {c.get("id") for c in categories if _slug(c) == UNCATEGORIZED_SLUG}
    return allowed


def unexpected_categories(categories: Iterable[Mapping], *, parent_id: int, child_ids) -> list:
    cats = list(categories)
    allowed = expected_category_ids(cats, parent_id=parent_id, child_ids=child_ids)
    return [
        {"id": c.get("id"), "name": c.get("name"), "slug": c.get("slug")}
        for c in cats
        if c.get("id") not in allowed
    ]


def desired_categories(*, parent_id: int, child_id: int) -> list[int]:
    if not isinstance(child_id, int) or child_id <= 0 or child_id == parent_id:
        raise TaxonomyRolloutStop(f"child category id {child_id!r} is not usable")
    return [parent_id, child_id]


def categories_payload(current: list[int], desired: list[int], *, parent_id: int) -> str:
    """``{"categories": [親, 子]}``。今のカテゴリを黙って外さないことを確かめる。"""

    if len(desired) != 2 or desired[0] != parent_id or len(set(desired)) != 2:
        raise TaxonomyRolloutStop(f"desired categories {desired} are not [parent, one child]")
    removed = sorted(set(current) - set(desired))
    if removed:
        raise TaxonomyRolloutStop(f"the write would remove categories {removed}")
    return json.dumps({"categories": desired})


def _raw(post: Mapping, field: str):
    value = post.get(field)
    return value.get("raw") if isinstance(value, dict) else value


def post_fingerprint(post: Mapping) -> dict:
    """カテゴリの設定で変わってはいけない項目 (本文と抜粋は hash)。"""

    return {
        "id": post.get("id"),
        "slug": post.get("slug"),
        "status": post.get("status"),
        "title": _raw(post, "title"),
        "date_gmt": post.get("date_gmt"),
        "tags": sorted(post.get("tags") or []),
        "featured_media": post.get("featured_media"),
        "link": post.get("link"),
        "content_sha256": hashlib.sha256((_raw(post, "content") or "").encode("utf-8")).hexdigest(),
        "excerpt_sha256": hashlib.sha256((_raw(post, "excerpt") or "").encode("utf-8")).hexdigest(),
    }


def post_pre_problems(
    plan_row: Mapping, post: Mapping, *, parent_id: int, base_url: str
) -> list[str]:
    """書く前: 計画の行と今の post (タイトル・slug・URL・カテゴリ・親・パーマリンク)。"""

    problems = drift_problems(plan_row, post, parent_id=parent_id)
    if post.get("status") != "publish":
        problems.append(f"post is {post.get('status')}")
    problems += permalink_problems([post], base_url=base_url, categories=[])
    return problems


def post_readback_problems(before: Mapping, after: Mapping, *, expected: list[int]) -> list[str]:
    """書いたあと: カテゴリがちょうど計画どおり、modified_gmt は変わり、ほかは同じ。"""

    problems = []
    got = list(after.get("categories") or [])
    # WordPress は ID を自分の順で返す ([9, 4] など)。同じ集合で重複が無ければ一致とみなす。
    if sorted(got) != sorted(expected) or len(set(got)) != len(got):
        problems.append(f"categories read back as {got}, expected {expected}")
    if after.get("modified_gmt") == before.get("modified_gmt"):
        problems.append("modified_gmt did not change (the write may not have been applied)")
    old, new = post_fingerprint(before), post_fingerprint(after)
    changed = sorted(k for k in old if old[k] != new[k])
    if changed:
        problems.append(f"unexpected post changes: {changed}")
    return problems


# == public pages (read-only parsing) ===========================================
def canonical(html: str) -> str | None:
    match = re.search(r'<link rel="canonical" href="([^"]+)"', html)
    return match.group(1) if match else None


def breadcrumb(html: str) -> list[dict]:
    """Cocoon のパンくず (BreadcrumbList の microdata) の項目: 名前と URL。"""

    block = re.search(r'<div id="breadcrumb"(.*?)</div>\s*</div>', html, re.S)
    if not block:
        return []
    items = []
    for href, name in re.findall(
        r'<a href="([^"]+)" itemprop="item"><span itemprop="name"[^>]*>(.*?)</span>',
        block.group(1),
        re.S,
    ):
        items.append({"name": re.sub(r"<[^>]+>", "", name).strip(), "url": href})
    return items


def category_labels(html: str) -> list[dict]:
    """記事ページのカテゴリのラベル (Cocoon の ``cat-link cat-link-<id>``)。"""

    return [
        {"id": int(cid), "url": href, "name": re.sub(r"<[^>]+>|\s+", "", name)}
        for cid, href, name in re.findall(
            r'<a class="cat-link cat-link-(\d+)" href="([^"]+)"[^>]*>(.*?)</a>', html, re.S
        )
    ]


def archive_links(html: str) -> list[str]:
    return re.findall(r'<a href="([^"]+)" class="entry-card-wrap[^"]*"', html)


def sitemap_locs(xml: str) -> list[str]:
    return re.findall(r"<loc>(.*?)</loc>", xml)


def public_problems(
    state: Mapping, *, link: str, parent_url: str, child_url: str, child_id: int
) -> list[str]:
    """公開ページの判定。子がパンくず・ラベルに出ないのも止める理由 (方式は自動で変えない)。

    カテゴリのアーカイブの URL は REST の ``link`` を使う (子は ``/category/<親>/<子>/`` になる)。
    """

    problems = []
    if state.get("article_status") != 200:
        problems.append(f"article returned {state.get('article_status')} (URL must not change)")
    if state.get("canonical") != link:
        problems.append(f"canonical is {state.get('canonical')!r}, expected {link!r}")
    crumbs = [c["url"] for c in state.get("breadcrumb") or []]
    if child_url not in crumbs:
        problems.append(f"breadcrumb does not show the child: {state.get('breadcrumb')}")
    elif parent_url not in crumbs or crumbs.index(parent_url) > crumbs.index(child_url):
        problems.append(f"breadcrumb does not show parent before child: {state.get('breadcrumb')}")
    if child_id not in [label["id"] for label in state.get("labels") or []]:
        problems.append(f"category label does not show the child: {state.get('labels')}")
    if not state.get("in_child_archive"):
        problems.append("the child archive does not list the article")
    if not state.get("in_parent_archive"):
        problems.append("the parent archive does not list the article")
    locs = state.get("category_sitemap") or []
    if child_url not in locs or parent_url not in locs:
        problems.append(f"category sitemap is missing parent/child: {locs}")
    return problems
