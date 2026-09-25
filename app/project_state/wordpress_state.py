"""WordPress・featured image・カテゴリの状態 (読むだけ)。

- live: 既存の ``WordPressClient`` の GET だけ (post の一覧・カテゴリ・タグ・media)。
- repository: W1.4 / W1.5 の manifest (``docs/operations/``)、W2 の計画のコード
  (``app/wordpress/taxonomy_plan.py``)。
- offline のときは live を読まず、リポジトリの宣言だけを「宣言」として載せる (観測とは言わない)。

公開ページ・``/go/`` は読まない。
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from app.project_state.provenance import provenance, skipped, unavailable
from app.wordpress.taxonomy_plan import CHILDREN, PARENT_NAME, PARENT_ONLY_ARTICLE_IDS

PILOT_MANIFEST = Path("docs/operations/featured-image-pilot-manifest.json")
W15_MANIFEST = Path("docs/operations/featured-image-w1.5-manifest.json")
TAXONOMY_DOC = Path("docs/operations/taxonomy-w2.md")
DUPLICATE_MEDIA_ID = 99


def read_wordpress(client_factory: Callable[[], object]) -> dict:
    """live の読み取り (GET だけ)。失敗は呼び出し側で「読めなかった」にする。"""

    client = client_factory()
    return {
        "posts": client.list_post_states(),
        "categories": client.list_categories(),
        "tags": client.list_tags(),
        "media": client.list_media_items(),
    }


def _title(post: Mapping) -> str | None:
    title = post.get("title")
    return title.get("raw") if isinstance(title, dict) else title


def summarize_wordpress(
    live: Mapping | None, *, articles: list[Mapping], now: datetime, error=None
):
    """``live`` が None なら読めていない (offline か失敗)。"""

    by_post = {a["wordpress_post_id"]: a["id"] for a in articles if a.get("wordpress_post_id")}
    if live is None:
        prov = skipped("live_rest") if error is None else unavailable("live_rest", str(error))
        return {"provenance": prov, "article_post_map_declared": _map_rows(by_post)}
    posts = live["posts"]
    statuses = Counter(p.get("status") for p in posts)
    published = [p for p in posts if p.get("status") == "publish"]
    unmapped = sorted(p["id"] for p in published if p["id"] not in by_post)
    return {
        "provenance": provenance(
            "live_rest", kind="observed", status="ok", observed_at=now, freshness="fresh"
        ),
        "post_status_counts": dict(sorted(statuses.items())),
        "published_count": len(published),
        "tag_count": len(live["tags"]),
        "category_count": len(live["categories"]),
        "media_count": len(live["media"]),
        "article_post_map": _map_rows(by_post, posts={p["id"]: p for p in posts}),
        "published_posts_without_application_article": unmapped,
    }


def _map_rows(by_post: Mapping, posts: Mapping | None = None) -> list[dict]:
    rows = []
    for post_id, article_id in sorted(by_post.items(), key=lambda kv: kv[1]):
        row = {"article_id": article_id, "wordpress_post_id": post_id}
        if posts is not None:
            post = posts.get(post_id) or {}
            row.update(status=post.get("status"), slug=post.get("slug"))
        rows.append(row)
    return rows


def _read_json(root: Path, rel: Path) -> dict | None:
    path = root / rel
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def declared_featured_images(root: Path) -> dict:
    """リポジトリの記録: article → WordPress の post → media (W1.4 の試作 + W1.5)。"""

    mapping: dict[int, dict] = {}
    pilot = _read_json(root, PILOT_MANIFEST) or {}
    for item in pilot.get("pilots", []):
        wp = item.get("wordpress") or {}
        if wp.get("media_id"):
            mapping[item["article_id"]] = {
                "wordpress_post_id": wp.get("wordpress_post_id"),
                "media_id": wp.get("media_id"),
                "phase": "W1.4",
            }
    w15 = _read_json(root, W15_MANIFEST) or {}
    for item in w15.get("articles", []):
        wp = item.get("wordpress") or {}
        if item.get("status") == "applied" and wp.get("media_id"):
            mapping[item["article_id"]] = {
                "wordpress_post_id": wp.get("wordpress_post_id"),
                "media_id": wp.get("media_id"),
                "phase": "W1.5",
            }
    return {
        "pilot_status": pilot.get("status"),
        "w15_status": w15.get("status"),
        "mapping": dict(sorted(mapping.items())),
    }


def summarize_featured_images(root: Path, live: Mapping | None, *, now: datetime, error=None):
    declared = declared_featured_images(root)
    out = {
        "w1_4_pilot": declared["pilot_status"],
        "w1_5_rollout": declared["w15_status"],
        "declared_count": len(declared["mapping"]),
        "declared_mapping_source": [str(PILOT_MANIFEST), str(W15_MANIFEST)],
        "declared_mapping": [{"article_id": a, **m} for a, m in declared["mapping"].items()],
    }
    if live is None:
        out["provenance"] = (
            skipped("live_rest") if error is None else unavailable("live_rest", str(error))
        )
        return out
    posts = {p["id"]: p for p in live["posts"] if p.get("status") == "publish"}
    media = {m["id"]: m for m in live["media"]}
    with_image = [p for p in posts.values() if p.get("featured_media")]
    missing_media = sorted(p["id"] for p in with_image if p["featured_media"] not in media)
    disagreements = []
    for article_id, m in declared["mapping"].items():
        live_media = (posts.get(m["wordpress_post_id"]) or {}).get("featured_media")
        if live_media != m["media_id"]:
            disagreements.append(
                {
                    "article_id": article_id,
                    "declared_media": m["media_id"],
                    "live_media": live_media,
                }
            )
    featured_by = Counter(p.get("featured_media") for p in with_image)
    duplicate = media.get(DUPLICATE_MEDIA_ID)
    out.update(
        provenance=provenance(
            "live_rest", kind="observed", status="ok", observed_at=now, freshness="fresh"
        ),
        published=len(posts),
        with_featured_image=len(with_image),
        without_featured_image=sorted(
            pid for pid, p in posts.items() if not p.get("featured_media")
        ),
        featured_media_missing_from_library=missing_media,
        media_used_by_more_than_one_post=sorted(k for k, n in featured_by.items() if n > 1),
        declared_vs_live_disagreements=disagreements,
        media_99={
            "exists": duplicate is not None,
            "attached_post": (duplicate or {}).get("post"),
            "featured_by": sorted(
                p["id"] for p in with_image if p["featured_media"] == DUPLICATE_MEDIA_ID
            ),
            "note": "W1.4 の重複 (使われていない)。任意の片付け。急がない。消さない (人が判断)。",
        },
    )
    return out


def summarize_taxonomy(root: Path, live: Mapping | None, *, articles, now: datetime, error=None):
    """W2 の計画 (コード) と live のカテゴリ・記事の割り当てを突き合わせる。"""

    planned = {
        "parent": PARENT_NAME,
        "children": [
            {
                "name": c["name"],
                "slug": c["slug"],
                "article_ids": list(c["article_ids"]),
                "expected_count": len(c["article_ids"]),
            }
            for c in CHILDREN
        ],
        "parent_only_article_ids": list(PARENT_ONLY_ARTICLE_IDS),
        "model": "parent + exactly one child (article 1 parent only)",
    }
    doc = (
        (root / TAXONOMY_DOC).read_text(encoding="utf-8") if (root / TAXONOMY_DOC).exists() else ""
    )
    out = {
        "w2_status_declared": "applied (W2C)"
        if "本番に適用済み (W2C" in doc
        else "not recorded as applied",
        "plan": planned,
        "accepted_effects": [
            (
                "カテゴリを付けると post の modified_gmt が変わり、Cocoon "
                "の更新日とサイトマップの lastmod が新しくなる (W2 で人が許容)"
            ),
        ],
        "lessons": [
            (
                "WordPress の API の利用者は最小権限の author でカテゴリを作れない (403)。W2 "
                "の子カテゴリは人が wp-admin で作った。この利用者の権限を広げることを既定で勧めな"
                "い"
            ),
            "WordPress はカテゴリの ID を自分の順で返す ([9, 4] など)。カテゴリの比較は集合で行う",
        ],
        "expected_rendering": (
            "パンくず「ホーム › 業務効率化 › 子」、記事ページのラベルは子が先・親が後、"
            "一覧のカードのラベルは子 (W2C で確認)"
        ),
        "source_doc": str(TAXONOMY_DOC),
    }
    if live is None:
        out["provenance"] = (
            skipped("live_rest") if error is None else unavailable("live_rest", str(error))
        )
        return out
    cats = live["categories"]
    parents = [c for c in cats if c.get("name") == PARENT_NAME and not c.get("parent")]
    parent = parents[0] if len(parents) == 1 else None
    children = []
    for child in CHILDREN:
        match = [c for c in cats if c.get("slug") == child["slug"]]
        term = match[0] if len(match) == 1 else None
        children.append(
            {
                "name": child["name"],
                "slug": child["slug"],
                "id": term.get("id") if term else None,
                "exact": bool(
                    term
                    and term.get("name") == child["name"]
                    and parent
                    and term.get("parent") == parent["id"]
                ),
                "count": term.get("count") if term else None,
                "expected_count": len(child["article_ids"]),
            }
        )
    by_article = {a["id"]: a.get("wordpress_post_id") for a in articles}
    posts = {p["id"]: p for p in live["posts"]}
    child_id = {c["slug"]: c["id"] for c in children}
    mismatches = []
    parent_only_ok = None
    for article_id in range(1, 26):
        post = posts.get(by_article.get(article_id))
        if post is None or parent is None:
            mismatches.append({"article_id": article_id, "reason": "post or parent not found"})
            continue
        got = sorted(post.get("categories") or [])
        if article_id in PARENT_ONLY_ARTICLE_IDS:
            want = [parent["id"]]
        else:
            child = next(c for c in CHILDREN if article_id in c["article_ids"])
            want = sorted([parent["id"], child_id.get(child["slug"]) or -1])
        if got != want:
            mismatches.append({"article_id": article_id, "live": got, "planned": want})
        if article_id == 1:
            parent_only_ok = got == [parent["id"]]
    out.update(
        provenance=provenance(
            "live_rest", kind="observed", status="ok", observed_at=now, freshness="fresh"
        ),
        parent={k: (parent or {}).get(k) for k in ("id", "name", "slug", "count")}
        if parent
        else None,
        children=children,
        categories_total=len(cats),
        tags_total=len(live["tags"]),
        article_1_parent_only=parent_only_ok,
        parent_plus_child_posts=sum(
            1 for a in range(2, 26) if not any(m["article_id"] == a for m in mismatches)
        ),
        assignment_mismatches=mismatches,
        matches_plan=bool(parent)
        and all(c["exact"] and c["count"] == c["expected_count"] for c in children)
        and not mismatches,
    )
    return out
