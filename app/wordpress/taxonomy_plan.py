"""W2: カテゴリ整理の計画 (読むだけの確認の判定。WordPress とは話さない)。

親カテゴリ「業務効率化」の下に 5 つの子カテゴリを置き、公開済みの 25 記事を 1 つずつ割り当てる
計画を作る。呼び出し側の CLI が WordPress (カテゴリ・タグ・post) とアプリの DB (記事と post の
対応) を読んで渡す。ここでは次を決める:

- 親カテゴリの解決 (名前で 1 件。ID を決め打ちしない)
- 子カテゴリの slug の衝突 (既存のカテゴリ・タグ)
- 記事ごとの割り当て (25 件ちょうど・重複なし・article 1 は親だけ)
- post の URL がカテゴリに依存していないこと (パーマリンクに ``%category%`` があれば止める)
- 計画のときの状態 (タイトル・slug・今のカテゴリ) からのずれ

割り当ての方式は **親 + 子** (``categories = [親, 子]``)。article 1 は **親だけ** (変更なし)。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from urllib.parse import unquote, urlsplit

TAXONOMY_VERSION = "w2-taxonomy/1"
PARENT_NAME = "業務効率化"

# 子カテゴリ (順番は計画と適用の順)。slug は小文字の ASCII・ハイフン区切り・日付やブランド名なし。
CHILDREN = (
    {
        "key": "ai",
        "name": "AI・生成AI",
        "slug": "ai-generative-ai",
        "scope": (
            "AI の業務利用・生成AI ツール・ChatGPT の法人利用・AI エージェント・"
            "AI の指針とガバナンス"
        ),
        "article_ids": (19, 20, 21, 22, 23, 24, 25),
    },
    {
        "key": "meeting",
        "name": "会議・文字起こし",
        "slug": "meeting-transcription",
        "scope": "AI 議事録・文字起こしのツール・料金・無料の範囲",
        "article_ids": (2, 3, 6, 7, 8, 9, 12),
    },
    {
        "key": "automation",
        "name": "ノーコード・自動化・RPA",
        "slug": "nocode-automation-rpa",
        "scope": "ノーコードの自動化 (Make など)・RPA の比較と導入",
        "article_ids": (10, 11, 16, 17, 18),
    },
    {
        "key": "crm",
        "name": "CRM・営業効率化",
        "slug": "crm-sales-efficiency",
        "scope": "CRM・SFA・営業の管理 (HubSpot など)",
        "article_ids": (4, 5, 14),
    },
    {
        "key": "task",
        "name": "タスク・プロジェクト管理",
        "slug": "task-project-management",
        "scope": "プロジェクト管理・タスク管理のツールと使い方",
        "article_ids": (13, 15),
    },
)
PARENT_ONLY_ARTICLE_IDS = (1,)
EXPECTED_ARTICLE_IDS = frozenset(range(1, 26))
EXPECTED_CHILD_COUNTS = {"ai": 7, "meeting": 7, "automation": 5, "crm": 3, "task": 2}

# 記事ごとの判断の理由 (タイトル・slug・内容・既存の cluster の分類から)。
SEMANTIC_REASONS = {
    1: "7 ツールを CRM・プロジェクト管理・タスク・iPaaS・AI スケジューリングにまたがって比べる総論"
    " (cluster B の pillar)。どれか 1 つの子に入れると範囲を狭めてしまうので親だけ",
    2: "AI 議事録のおすすめ (cluster A の pillar)",
    3: "文字起こし AI のおすすめ (cluster A)。AI の話題だが用途は会議・文字起こし",
    4: "CRM のおすすめ (keyword category: CRM / sales operations)",
    5: "HubSpot の料金 (keyword category: CRM / sales operations)",
    6: "AI 議事録の比較 (cluster A)",
    7: "AI 議事録の基礎知識 (cluster A のカテゴリ解説)",
    8: "AI 議事録の無料の範囲 (cluster A)",
    9: "AI 議事録の料金 (cluster A)",
    10: "Make の使い方 (ノーコードの自動化の道具)。AI ではない",
    11: "Make の料金 (cluster C)。AI ではない",
    12: "無料の文字起こし。用途は会議・文字起こし",
    13: "プロジェクト管理ツールのおすすめ (ClickUp / monday.com)。自動化とは別の用途",
    14: "CRM と SFA の違い (HubSpot / Pipedrive の範囲の比較)",
    15: "Notion でのタスク管理。自動化とは別の用途",
    16: "RPA のおすすめ (cluster C の pillar)",
    17: "RPA の比較 (cluster C)",
    18: "RPA の導入 (cluster C)",
    19: "生成AI ツールのおすすめ (cluster D)",
    20: "ChatGPT Enterprise (法人向けの生成AI)",
    21: "生成AI・AI 事業者ガイドライン。AI の子の中で扱う (ガバナンス専用の子は作らない)",
    22: "AI ガバナンス。AI の子の中で扱う (ガバナンス専用の子は作らない)",
    23: "ChatGPT の法人プラン (法人向けの生成AI)",
    24: "AI エージェントの基礎知識",
    25: "AI の業務利用 (AI に任せる範囲)。AI の子に入れる。ただし既存の cluster 定義では"
    "「AI 業務効率化」は cluster B (業務効率化ツール) の supporting なので、人が確かめる",
}
HUMAN_REVIEW_ARTICLE_IDS = frozenset({25})

_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_FORBIDDEN_SLUG_PARTS = re.compile(r"(19|20)\d{2}|\bv\d+\b|\bversion\b|\bnew\b")


class TaxonomyPlanError(ValueError):
    """計画を作れない (人が確かめる)。"""


def _name(term: Mapping) -> str:
    return str(term.get("name") or "")


def resolve_parent(categories: Iterable[Mapping]) -> dict:
    """親カテゴリを名前で 1 件に決める (トップレベルであること)。ID は決め打ちしない。"""

    matches = [c for c in categories if _name(c) == PARENT_NAME]
    if len(matches) != 1:
        raise TaxonomyPlanError(
            f"expected exactly 1 category named {PARENT_NAME}, got {len(matches)}"
        )
    parent = matches[0]
    if parent.get("parent") not in (0, None):
        raise TaxonomyPlanError(f"{PARENT_NAME} is not a top-level category")
    return dict(parent)


def slug_problems(slug: str) -> list[str]:
    problems = []
    if not _SLUG.fullmatch(slug):
        problems.append("not lowercase ASCII words joined by hyphens")
    if _FORBIDDEN_SLUG_PARTS.search(slug):
        problems.append("contains a date or version term")
    return problems


def slug_collisions(categories: Iterable[Mapping], tags: Iterable[Mapping]) -> dict[str, list]:
    """子カテゴリの slug・名前が、既存のカテゴリやタグと重なっていないか。"""

    cats, tag_list = list(categories), list(tags)
    found: dict[str, list] = {}
    for child in CHILDREN:
        hits = []
        for kind, terms in (("category", cats), ("tag", tag_list)):
            for term in terms:
                if unquote(str(term.get("slug") or "")) == child["slug"]:
                    hits.append({"kind": kind, "id": term.get("id"), "field": "slug"})
                if _name(term) == child["name"]:
                    hits.append({"kind": kind, "id": term.get("id"), "field": "name"})
        if hits:
            found[child["slug"]] = hits
    return found


def permalink_problems(posts: Iterable[Mapping], *, base_url: str, categories: Iterable[Mapping]):
    """post の URL がカテゴリに依存していないか (``/%postname%/`` の形であること)。

    どの post の URL も ``<base>/<slug>/`` でなければならない。URL の中にカテゴリの slug や
    ``/category/`` があれば、カテゴリの変更で URL が変わりうるので止める。
    """

    slugs = {unquote(str(c.get("slug") or "")) for c in categories}
    problems = []
    for post in posts:
        link = str(post.get("link") or "")
        path = urlsplit(link).path
        expected = f"/{post.get('slug')}/"
        if not link.startswith(base_url.rstrip("/") + "/"):
            problems.append(f"post {post.get('id')}: link is not under the site")
        if path != expected:
            segments = [unquote(s) for s in path.strip("/").split("/")]
            if "category" in segments or slugs & set(segments[:-1]):
                problems.append(f"post {post.get('id')}: the URL contains a category ({path})")
            else:
                problems.append(f"post {post.get('id')}: the URL is not /<slug>/ ({path})")
    return problems


def assignment() -> dict[int, dict | None]:
    """article_id → 子カテゴリ (親だけなら None)。25 件ちょうど・重複なしを確かめる。"""

    mapping: dict[int, dict | None] = {}
    for child in CHILDREN:
        for article_id in child["article_ids"]:
            if article_id in mapping:
                raise TaxonomyPlanError(f"article {article_id} is assigned twice")
            mapping[article_id] = child
    for article_id in PARENT_ONLY_ARTICLE_IDS:
        if article_id in mapping:
            raise TaxonomyPlanError(f"article {article_id} is both parent-only and a child")
        mapping[article_id] = None
    if set(mapping) != EXPECTED_ARTICLE_IDS:
        missing = sorted(EXPECTED_ARTICLE_IDS - set(mapping))
        extra = sorted(set(mapping) - EXPECTED_ARTICLE_IDS)
        raise TaxonomyPlanError(
            f"assignment does not cover 1-25 (missing {missing}, extra {extra})"
        )
    return mapping


def build_plan(
    *,
    categories: list[Mapping],
    tags: list[Mapping],
    posts: list[Mapping],
    articles: list[Mapping],
    base_url: str,
    generated_at: str,
) -> dict:
    """25 記事の割り当ての計画と、書く前に満たすべき確認の結果。"""

    problems: list[str] = []
    parent = resolve_parent(categories)
    mapping = assignment()
    by_post = {p.get("id"): p for p in posts}
    names = {c.get("id"): _name(c) for c in categories}
    published = [p for p in posts if p.get("status") == "publish"]
    if len(published) != 25:
        problems.append(f"expected 25 published posts, got {len(published)}")

    rows = []
    for article in sorted(articles, key=lambda a: a["id"]):
        article_id = article["id"]
        if article_id not in mapping:
            continue
        post = by_post.get(article.get("wordpress_post_id"))
        if post is None:
            problems.append(
                f"article {article_id}: WordPress post {article.get('wordpress_post_id')} not found"
            )
            continue
        title = (
            post["title"].get("raw") if isinstance(post.get("title"), dict) else post.get("title")
        )
        if title != article["title"]:
            problems.append(f"article {article_id}: title differs from the application ({title!r})")
        if unquote(str(post.get("slug") or "")) != article["slug"]:
            problems.append(f"article {article_id}: slug differs from the application")
        if post.get("status") != "publish":
            problems.append(f"article {article_id}: post is {post.get('status')}")
        current = list(post.get("categories") or [])
        if current != [parent["id"]]:
            problems.append(
                f"article {article_id}: current categories {current} are not [{parent['id']}]"
            )
        child = mapping[article_id]
        rows.append(
            {
                "article_id": article_id,
                "wordpress_post_id": post["id"],
                "title": title,
                "slug": post.get("slug"),
                "slug_decoded": unquote(str(post.get("slug") or "")),
                "link": post.get("link"),
                "current_category_ids": current,
                "current_category_names": [names.get(c, f"#{c}") for c in current],
                "current_tag_ids": list(post.get("tags") or []),
                "current_modified_gmt": post.get("modified_gmt"),
                "planned_parent_category": {
                    "id": parent["id"],
                    "name": parent["name"],
                    "slug": parent["slug"],
                },
                "planned_child_category": None
                if child is None
                else {"name": child["name"], "slug": child["slug"], "id": None},
                "planned_category_names": [parent["name"]] + ([child["name"]] if child else []),
                "planned_category_slugs": [parent["slug"]] + ([child["slug"]] if child else []),
                # 子カテゴリはまだ無いので ID は未定。作ったあと (W2 の本番) に読み戻して埋める。
                "planned_category_ids": [parent["id"]] if child is None else None,
                "action": "keep_parent_only" if child is None else "add_child",
                "semantic_reason": SEMANTIC_REASONS[article_id],
                "human_review_required": article_id in HUMAN_REVIEW_ARTICLE_IDS,
            }
        )
    if {r["article_id"] for r in rows} != EXPECTED_ARTICLE_IDS:
        problems.append("the plan does not have exactly articles 1-25")

    collisions = slug_collisions(categories, tags)
    for slug, hits in collisions.items():
        problems.append(f"slug/name collision for {slug}: {hits}")
    for child in CHILDREN:
        for p in slug_problems(child["slug"]):
            problems.append(f"slug {child['slug']}: {p}")
    permalink = permalink_problems(published, base_url=base_url, categories=categories)
    problems += permalink

    counts = Counter(
        r["planned_child_category"]["slug"] for r in rows if r["planned_child_category"]
    )
    children = [
        {
            "key": c["key"],
            "name": c["name"],
            "slug": c["slug"],
            "parent_id": parent["id"],
            "scope": c["scope"],
            "article_ids": list(c["article_ids"]),
            "expected_count": counts.get(c["slug"], 0),
        }
        for c in CHILDREN
    ]
    checks = {
        "exactly_25_articles": len(rows) == 25,
        "exactly_5_children": len(children) == 5,
        "child_counts_7_7_5_3_2": {c["key"]: c["expected_count"] for c in children}
        == EXPECTED_CHILD_COUNTS,
        "article_1_parent_only": [
            r["article_id"] for r in rows if r["planned_child_category"] is None
        ]
        == list(PARENT_ONLY_ARTICLE_IDS),
        "others_exactly_one_child": all(
            r["planned_child_category"]
            for r in rows
            if r["article_id"] not in PARENT_ONLY_ARTICLE_IDS
        ),
        "no_slug_collision": not collisions,
        "permalink_has_no_category": not permalink,
        "current_categories_parent_only": all(
            r["current_category_ids"] == [parent["id"]] for r in rows
        ),
        "titles_and_slugs_match_application": not any(
            "title differs" in p or "slug differs" in p for p in problems
        ),
    }
    return {
        "taxonomy_version": TAXONOMY_VERSION,
        "generated_at": generated_at,
        "assignment_model": "parent_and_child",
        "parent_category": {
            "id": parent["id"],
            "name": parent["name"],
            "slug": parent["slug"],
            "count": parent.get("count"),
            "expected_count_after": 25,
        },
        "existing_categories": [
            {k: c.get(k) for k in ("id", "name", "slug", "parent", "count", "description")}
            for c in categories
        ],
        "existing_tags": [{k: t.get(k) for k in ("id", "name", "slug", "count")} for t in tags],
        "planned_children": children,
        "articles": rows,
        "checks": checks,
        "problems": problems,
        "ready": all(checks.values()) and not problems,
    }


def drift_problems(plan_row: Mapping, post: Mapping, *, parent_id: int) -> list[str]:
    """適用の直前に使う: 計画のときの post からずれていれば理由を返す (ずれていれば書かない)。"""

    problems = []
    title = post["title"].get("raw") if isinstance(post.get("title"), dict) else post.get("title")
    if post.get("id") != plan_row["wordpress_post_id"]:
        problems.append("post id differs")
    if title != plan_row["title"]:
        problems.append("title drifted")
    if post.get("slug") != plan_row["slug"]:
        problems.append("slug drifted")
    if post.get("link") != plan_row["link"]:
        problems.append("URL drifted")
    if list(post.get("categories") or []) != plan_row["current_category_ids"]:
        problems.append(f"categories drifted to {post.get('categories')}")
    if parent_id != plan_row["planned_parent_category"]["id"]:
        problems.append("parent category changed")
    return problems


def render_markdown(plan: Mapping) -> str:
    parent = plan["parent_category"]
    lines = [
        "# W2 taxonomy plan (read-only)",
        "",
        f"- generated: {plan['generated_at']}",
        f"- ready: **{plan['ready']}**; model: `{plan['assignment_model']}` (parent + one child; "
        "article 1 parent only)",
        f"- parent: {parent['name']} (id {parent['id']}, slug `{parent['slug']}`, "
        f"count {parent['count']})",
        "",
        "## Checks",
        "",
        *[f"- {'OK' if ok else 'NG'} {name}" for name, ok in plan["checks"].items()],
        "",
        "## Problems",
        "",
        *([f"- {p}" for p in plan["problems"]] or ["- none"]),
        "",
        "## Planned children",
        "",
        "| name | slug | parent | expected count | articles |",
        "|---|---|---|---|---|",
        *[
            f"| {c['name']} | `{c['slug']}` | {c['parent_id']} | {c['expected_count']} | "
            f"{', '.join(map(str, c['article_ids']))} |"
            for c in plan["planned_children"]
        ],
        "",
        "## Articles",
        "",
        "| article | WP post | title | now | planned | action | review |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in plan["articles"]:
        lines.append(
            f"| {r['article_id']} | {r['wordpress_post_id']} | {r['title']} | "
            f"{' / '.join(r['current_category_names'])} | "
            f"{' / '.join(r['planned_category_names'])} | "
            f"{r['action']} | {'yes' if r['human_review_required'] else ''} |"
        )
    lines += ["", "This plan wrote nothing to WordPress.", ""]
    return "\n".join(lines)
