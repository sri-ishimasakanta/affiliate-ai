"""W2B: カテゴリ整理を 1 項目ずつ適用する道具 (偽の WordPress と偽の公開ページ。通信しない)。

pin する契約:

- 既定は読むだけ (書き込み 0)。``--execute`` のときだけ、次の 1 項目だけを書く。
- 子カテゴリは計画の順に 1 つずつ。ちょうど同じ名前・slug・親のものは作らずに使う。
  名前か slug だけが重なる・親が違うものは止める。読み戻しが合わなければ止める。
- 記事は 5 つの子がそろってから、canary (15) → 承認した順。article 1 には書かない。
  ``{"categories": [親, 子]}`` だけを送る。今のカテゴリを黙って外さない。
- 書いたら: カテゴリがちょうど計画どおり、modified_gmt は変わる (想定どおり)、公開日・本文・
  抜粋・状態・タグ・タイトル・slug・URL は同じ。公開ページの URL・canonical・パンくず・
  ラベル・アーカイブ・サイトマップ。子がパンくずに出なければ止める (方式を自動で変えない)。
- 止まった・途中の項目があれば次へ進まない。うまくいった項目を繰り返さない。
"""

from __future__ import annotations

import copy
import json

import pytest

from app.wordpress.taxonomy_plan import CHILDREN, build_plan
from app.wordpress.taxonomy_rollout import (
    ARTICLE_ORDER,
    CHILD_ORDER,
    TaxonomyRolloutStop,
    categories_payload,
    child_for_article,
    desired_categories,
)
from scripts.rollout_taxonomy import compare, main, snapshot

SITE = "https://bizfluxlab.com"
PARENT = {
    "id": 4,
    "name": "業務効率化",
    "slug": "gyomu-koritsuka",
    "parent": 0,
    "link": f"{SITE}/category/gyomu-koritsuka/",
}


class FakeWP:
    target_base_url = SITE

    def __init__(self) -> None:
        self.writes: list[tuple[str, dict]] = []
        self.categories = {
            1: {
                "id": 1,
                "name": "未分類",
                "slug": "uncategorized",
                "parent": 0,
                "link": f"{SITE}/category/uncategorized/",
            },
            4: dict(PARENT),
        }
        self.posts = {}
        for article_id in range(1, 26):
            pid = 100 + article_id
            self.posts[pid] = {
                "id": pid,
                "slug": f"article-{article_id}",
                "status": "publish",
                "title": {"raw": f"記事 {article_id}"},
                "content": {"raw": f"<p>{article_id}</p>"},
                "excerpt": {"raw": "要約"},
                "categories": [4],
                "tags": [],
                "date_gmt": "2026-09-22T10:00:00",
                "modified_gmt": "2026-09-25T16:16:02",
                "featured_media": 200 + article_id,
                "link": f"{SITE}/article-{article_id}/",
            }
        self.next_category_id = 50
        self.bump_modified = True
        self.corrupt_readback = None

    def _counts(self):
        return {
            cid: sum(cid in p["categories"] for p in self.posts.values()) for cid in self.categories
        }

    # -- reads
    def list_categories(self):
        counts = self._counts()
        return [{**copy.deepcopy(c), "count": counts[c["id"]]} for c in self.categories.values()]

    def list_tags(self):
        return []

    def list_post_states(self):
        return [copy.deepcopy(p) for p in self.posts.values()]

    def get_post(self, post_id):
        return copy.deepcopy(self.posts[post_id])

    def find_published_posts_by_slug(self, slug):
        return [copy.deepcopy(p) for p in self.posts.values() if p["slug"] == slug]

    # -- writes (exact contracts)
    def create_category_exact(self, payload_json, *, expected_parent_id):
        payload = json.loads(payload_json)
        assert (
            set(payload) == {"name", "slug", "parent"} and payload["parent"] == expected_parent_id
        )
        self.writes.append(("create_category", payload))
        cid = self.next_category_id
        self.next_category_id += 1
        # WordPress は子のアーカイブを親の下に置く (/category/<親>/<子>/)。
        link = f"{SITE}/category/gyomu-koritsuka/{payload['slug']}/"
        self.categories[cid] = {"id": cid, **payload, "link": link}
        if self.corrupt_readback:
            self.categories[cid].update(self.corrupt_readback)
        return {**self.categories[cid], "count": 0}

    def set_post_categories_exact(self, post_id, payload_json):
        payload = json.loads(payload_json)
        assert set(payload) == {"categories"}
        self.writes.append(("set_categories", {"post": post_id, **payload}))
        self.posts[post_id]["categories"] = payload["categories"]
        if self.bump_modified:
            self.posts[post_id]["modified_gmt"] = "2026-09-26T09:00:00"
        return copy.deepcopy(self.posts[post_id])

    def __getattr__(self, name):  # 契約の外の method は無い
        raise AttributeError(name)


def pages(wp: FakeWP, *, child_in_breadcrumb=True, child_label=True, canonical_override=None):
    """Cocoon に似た公開ページを WordPress の状態から作る。"""

    def cat_url(cid):
        return wp.categories[cid]["link"]

    def http_get(url, *, bust):
        if url.endswith("/wp-sitemap-taxonomies-category-1.xml"):
            counts = wp._counts()
            locs = "".join(f"<loc>{cat_url(c)}</loc>" for c, n in counts.items() if n)
            return 200, f"<urlset>{locs}</urlset>"
        if "/category/" in url:
            if "/page/" in url:
                return 404, ""
            cid = next(c for c in wp.categories if url == cat_url(c))
            links = [
                p["link"]
                for p in wp.posts.values()
                if cid in p["categories"]
                or any(wp.categories[x].get("parent") == cid for x in p["categories"])
            ]
            return 200, "".join(
                f'<a href="{link}" class="entry-card-wrap a-wrap">' for link in links
            )
        post = next((p for p in wp.posts.values() if p["link"] == url), None)
        if post is None:
            return 404, ""
        children = [c for c in post["categories"] if wp.categories[c].get("parent") == 4]
        crumbs = [(SITE, "ホーム"), (cat_url(4), "業務効率化")]
        if children and child_in_breadcrumb:
            crumbs.append((cat_url(children[0]), wp.categories[children[0]]["name"]))
        crumb_html = "".join(
            f'<div class="breadcrumb-item"><a href="{u}" itemprop="item">'
            f'<span itemprop="name" class="breadcrumb-caption">{n}</span></a></div>'
            for u, n in crumbs
        )
        label = children[0] if children and child_label else 4
        return 200, (
            f'<link rel="canonical" href="{canonical_override or post["link"]}">'
            f'<div id="breadcrumb" class="breadcrumb">{crumb_html}</div>'
            f'<a class="cat-link cat-link-{label}" href="{cat_url(label)}">'
            f"{wp.categories[label]['name']}</a>"
        )

    return http_get


@pytest.fixture
def world(tmp_path):
    wp = FakeWP()
    articles = [
        {"id": a, "title": f"記事 {a}", "slug": f"article-{a}", "wordpress_post_id": 100 + a}
        for a in range(1, 26)
    ]
    plan = build_plan(
        categories=wp.list_categories(),
        tags=[],
        posts=wp.list_post_states(),
        articles=articles,
        base_url=SITE,
        generated_at="2026-09-26T00:00:00+00:00",
    )
    assert plan["ready"] is True
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return wp, plan_path, tmp_path / "state"


def run(world, *args, http_get=None, capsys=None):
    wp, plan_path, state = world
    if capsys:
        capsys.readouterr()  # それまでの出力は捨てる
    code = main(
        ["--plan", str(plan_path), "--state", str(state), *args],
        client=wp,
        http_get=http_get or pages(wp),
        sleep=lambda _s: None,
    )
    out = capsys.readouterr().out if capsys else ""
    return code, out


def record(world, kind, key):
    return json.loads((world[2] / f"{kind}-{key}.json").read_text("utf-8"))


def create_all(world):
    for _ in CHILD_ORDER:
        assert run(world, "create-next-category", "--execute")[0] == 0


# == the plan =====================================================================
def test_the_final_plan_has_5_children_and_24_article_writes() -> None:
    assert len(CHILDREN) == 5 and len(CHILD_ORDER) == 5
    assert CHILD_ORDER == ("ai", "meeting", "automation", "crm", "task")
    assert len(ARTICLE_ORDER) == 24 and len(set(ARTICLE_ORDER)) == 24
    assert 1 not in ARTICLE_ORDER and ARTICLE_ORDER[0] == 15
    assert ARTICLE_ORDER[1:] == (
        13,
        4,
        5,
        14,
        10,
        11,
        16,
        17,
        18,
        2,
        3,
        6,
        7,
        8,
        9,
        12,
        19,
        20,
        21,
        22,
        23,
        24,
        25,
    )
    assert child_for_article(25)["name"] == "AI・生成AI"
    assert child_for_article(15)["slug"] == "task-project-management"
    with pytest.raises(TaxonomyRolloutStop, match="parent-only"):
        child_for_article(1)


# == category creation ============================================================
def test_category_creation_is_read_only_by_default(world, capsys) -> None:
    code, out = run(world, "create-next-category", capsys=capsys)
    assert code == 0 and world[0].writes == []
    assert '"result": "ready"' in out and "POST /wp/v2/categories" in out
    assert not world[2].exists()


def test_execute_creates_exactly_one_category_with_the_exact_payload(world) -> None:
    wp = world[0]
    assert run(world, "create-next-category", "--execute")[0] == 0
    assert wp.writes == [
        ("create_category", {"name": "AI・生成AI", "slug": "ai-generative-ai", "parent": 4})
    ]
    rec = record(world, "category", "ai")
    assert (rec["result"], rec["category_id"]) == ("created", 50)
    assert rec["readback"] == {
        "id": 50,
        "name": "AI・生成AI",
        "slug": "ai-generative-ai",
        "parent": 4,
        "count": 0,
    }
    # 次の実行は 2 つ目の子。作ったものを繰り返さない。
    assert run(world, "create-next-category", "--execute")[0] == 0
    assert [w[1]["slug"] for w in wp.writes] == ["ai-generative-ai", "meeting-transcription"]


def test_an_exact_existing_child_is_reused_without_a_write(world) -> None:
    wp = world[0]
    wp.categories[77] = {
        "id": 77,
        "name": "AI・生成AI",
        "slug": "ai-generative-ai",
        "parent": 4,
        "link": f"{SITE}/category/gyomu-koritsuka/ai-generative-ai/",
    }
    assert run(world, "create-next-category", "--execute")[0] == 0
    assert wp.writes == []
    rec = record(world, "category", "ai")
    assert (rec["result"], rec["category_id"]) == ("reused", 77)


@pytest.mark.parametrize(
    ("term", "message"),
    [
        ({"name": "AI・生成AI", "slug": "ai", "parent": 4}, "collision"),  # 名前だけ重なる
        ({"name": "AI", "slug": "ai-generative-ai", "parent": 4}, "collision"),  # slug だけ
        ({"name": "AI・生成AI", "slug": "ai-generative-ai", "parent": 1}, "collision"),  # 親が違う
        ({"name": "別のカテゴリ", "slug": "other", "parent": 4}, "unexpected categories"),
    ],
)
def test_category_collisions_and_unexpected_categories_fail_closed(world, term, message) -> None:
    wp = world[0]
    wp.categories[77] = {"id": 77, **term, "link": f"{SITE}/category/x/"}
    assert run(world, "create-next-category", "--execute")[0] == 2
    assert wp.writes == []
    rec = record(world, "category", "ai")
    assert rec["result"] == "stopped" and message in rec["reason"]


def test_a_changed_parent_fails_closed(world) -> None:
    wp = world[0]
    wp.categories[4]["slug"] = "renamed"
    assert run(world, "create-next-category", "--execute")[0] == 2
    assert (
        wp.writes == [] and "parent category changed" in record(world, "category", "ai")["reason"]
    )


def test_a_create_readback_mismatch_stops_and_blocks_the_rest(world, capsys) -> None:
    wp = world[0]
    wp.corrupt_readback = {"slug": "ai-generative-ai-2"}
    assert run(world, "create-next-category", "--execute")[0] == 2
    rec = record(world, "category", "ai")
    assert rec["result"] == "stopped" and "slug read back" in rec["reason"]
    assert rec["category_id"] == 50  # 作ったことは記録に残る
    code, out = run(world, "create-next-category", "--execute", capsys=capsys)
    assert code == 2 and "a human must resolve" in out
    assert len(wp.writes) == 1


# == articles =====================================================================
def test_articles_wait_for_all_five_children(world, capsys) -> None:
    run(world, "create-next-category", "--execute")
    code, out = run(world, "next", "--execute", capsys=capsys)
    assert code == 2 and "all 5 child categories" in out


def test_the_canary_write_is_read_only_by_default_then_exact(world, capsys) -> None:
    wp = world[0]
    create_all(world)
    task_id = record(world, "category", "task")["category_id"]
    writes_before = list(wp.writes)
    code, out = run(world, "next", capsys=capsys)
    assert code == 0 and wp.writes == writes_before
    shown = json.loads(out.splitlines()[0])
    assert shown["plan"] == f'POST /wp/v2/posts/115 {{"categories": [4, {task_id}]}}'  # article 15

    assert run(world, "next", "--execute")[0] == 0
    assert wp.writes[-1] == ("set_categories", {"post": 115, "categories": [4, task_id]})
    rec = record(world, "article", 15)
    assert rec["result"] == "applied" and rec["stage"] == "done"
    assert rec["before_modified_gmt"] != rec["after_modified_gmt"]  # 変わるのが想定どおり
    assert rec["before"]["date_gmt"] == wp.posts[115]["date_gmt"]  # 公開日は同じ
    assert rec["original_categories"] == [4]  # 巻き戻しに使う
    rendered = rec["public"]["rendered"]
    assert [c["name"] for c in rendered["breadcrumb"]] == [
        "ホーム",
        "業務効率化",
        "タスク・プロジェクト管理",
    ]
    assert rendered["in_child_archive"] and rendered["in_parent_archive"]
    # 成功した記事は繰り返さず、次は 13。
    assert run(world, "next", "--execute")[0] == 0
    assert wp.writes[-1][1]["post"] == 113
    assert [w for w in wp.writes if w[0] == "set_categories" and w[1]["post"] == 115].__len__() == 1


def test_article_1_is_never_written_even_after_all_24(world) -> None:
    wp = world[0]
    create_all(world)
    for _ in ARTICLE_ORDER:
        assert run(world, "next", "--execute")[0] == 0
    posts = [w[1]["post"] for w in wp.writes if w[0] == "set_categories"]
    assert len(posts) == 24 and 101 not in posts
    assert wp.posts[101]["categories"] == [4]
    assert run(world, "next", "--execute")[0] == 0  # 全部済み: 何もしない
    assert len([w for w in wp.writes if w[0] == "set_categories"]) == 24


def _drift_title(wp):
    wp.posts[115]["title"] = {"raw": "変わった"}


def _drift_slug(wp):
    wp.posts[115]["slug"] = "renamed"


def _drift_categories(wp):
    wp.posts[115]["categories"] = [4, 1]


def _drift_permalink(wp):
    wp.posts[115]["link"] = f"{SITE}/gyomu-koritsuka/article-15/"


def _drift_child_deleted(wp):
    del wp.categories[max(wp.categories)]


@pytest.mark.parametrize(
    ("drift", "message"),
    [
        (_drift_title, "title drifted"),
        (_drift_slug, "slug resolves to []"),
        (_drift_categories, "categories drifted"),
        (_drift_permalink, "URL"),
        (_drift_child_deleted, "is not category"),
    ],
)
def test_drift_before_an_article_write_fails_closed(world, drift, message) -> None:
    wp = world[0]
    create_all(world)
    drift(wp)
    writes = list(wp.writes)
    assert run(world, "next", "--execute")[0] == 2
    assert wp.writes == writes
    rec = record(world, "article", 15)
    assert rec["result"] == "stopped" and message in rec["reason"]


def test_the_payload_keeps_the_parent_and_never_drops_a_category_silently() -> None:
    assert categories_payload([4], [4, 55], parent_id=4) == '{"categories": [4, 55]}'
    with pytest.raises(TaxonomyRolloutStop, match="would remove"):
        categories_payload([4, 1], [4, 55], parent_id=4)
    for bad in ([4], [4, 55, 56], [55, 4], [4, 4]):
        with pytest.raises(TaxonomyRolloutStop):
            categories_payload([4], bad, parent_id=4)
    for bad in (0, 4, None, "55"):
        with pytest.raises(TaxonomyRolloutStop):
            desired_categories(parent_id=4, child_id=bad)


def test_an_unchanged_modified_gmt_means_the_write_is_not_proven(world) -> None:
    wp = world[0]
    create_all(world)
    wp.bump_modified = False
    assert run(world, "next", "--execute")[0] == 2
    rec = record(world, "article", 15)
    assert rec["stage"] == "readback" and "modified_gmt did not change" in rec["reason"]


@pytest.mark.parametrize("field", ["content", "excerpt", "status", "tags", "date_gmt", "title"])
def test_any_other_post_change_on_readback_stops(world, field) -> None:
    wp = world[0]
    create_all(world)
    original = wp.set_post_categories_exact

    def drifting(post_id, payload_json):
        result = original(post_id, payload_json)
        value = {
            "content": {"raw": "x"},
            "excerpt": {"raw": "x"},
            "status": "draft",
            "tags": [9],
            "date_gmt": "2020-01-01T00:00:00",
            "title": {"raw": "x"},
        }[field]
        wp.posts[post_id][field] = value
        return result

    wp.set_post_categories_exact = drifting
    assert run(world, "next", "--execute")[0] == 2
    rec = record(world, "article", 15)
    assert rec["stage"] == "readback" and "unexpected post changes" in rec["reason"]


def test_a_breadcrumb_without_the_child_stops_the_canary_and_the_rest(world, capsys) -> None:
    wp = world[0]
    create_all(world)
    http_get = pages(wp, child_in_breadcrumb=False)
    assert run(world, "next", "--execute", http_get=http_get)[0] == 2
    rec = record(world, "article", 15)
    assert rec["stage"] == "public" and "breadcrumb does not show the child" in rec["reason"]
    # 見えたとおりを記録する (人が決める)。方式は自動で変えない: カテゴリは [親, 子] のまま。
    assert [c["name"] for c in rec["public"]["rendered"]["breadcrumb"]] == ["ホーム", "業務効率化"]
    assert wp.posts[115]["categories"] == [4, record(world, "category", "task")["category_id"]]
    code, out = run(world, "next", "--execute", capsys=capsys)
    assert code == 2 and "a human must resolve" in out
    assert len([w for w in wp.writes if w[0] == "set_categories"]) == 1  # 残りには書かない


def test_a_label_without_the_child_stops(world) -> None:
    wp = world[0]
    create_all(world)
    assert run(world, "next", "--execute", http_get=pages(wp, child_label=False))[0] == 2
    assert "category label does not show the child" in record(world, "article", 15)["reason"]


def test_a_changed_canonical_fails_the_public_check(world) -> None:
    wp = world[0]
    create_all(world)
    http_get = pages(wp, canonical_override=f"{SITE}/somewhere-else/")
    assert run(world, "next", "--execute", http_get=http_get)[0] == 2
    rec = record(world, "article", 15)
    assert "canonical is" in rec["reason"] and rec["public"]["attempt"] == 4


def test_an_interrupted_write_blocks_resume_until_a_human_looks(world, capsys) -> None:
    wp = world[0]
    create_all(world)
    state = world[2]
    (state / "article-15.json").write_text(json.dumps({"result": "in_progress"}), "utf-8")
    code, out = run(world, "next", "--execute", capsys=capsys)
    assert code == 2 and "stopped or in progress" in out
    assert not [w for w in wp.writes if w[0] == "set_categories"]


def test_status_is_local_and_plan_checks_live_state(world, capsys) -> None:
    create_all(world)
    code, out = run(world, "status", capsys=capsys)
    assert code == 0
    progress = json.loads(out.splitlines()[0])
    assert progress["categories"]["done"] == list(CHILD_ORDER)
    assert progress["articles"]["pending"][0] == 15
    world[0].posts[115]["title"] = {"raw": "変わった"}
    code, out = run(world, "plan", capsys=capsys)
    assert code == 2 and "title drifted" in out  # 再開の前に今の WordPress を確かめる


def test_snapshot_and_compare_show_the_created_categories_and_changed_posts(world) -> None:
    wp = world[0]
    before = snapshot(wp)
    create_all(world)
    run(world, "next", "--execute")
    after = snapshot(wp)
    result = compare(before, after)
    assert [c["slug"] for c in result["categories_added"]] == [c["slug"] for c in CHILDREN]
    assert result["categories_removed"] == [] and result["categories_changed"] == {}
    assert result["posts_changed"] == {115: ["categories", "modified_gmt"]}


def test_children_created_in_wp_admin_are_adopted_one_by_one_without_a_post(world) -> None:
    """W2C: API の利用者は author でカテゴリを作れない (403)。人が wp-admin で 5 つを作った。"""

    wp = world[0]
    for offset, child in enumerate(CHILDREN):
        cid = 5 + offset
        wp.categories[cid] = {
            "id": cid,
            "name": child["name"],
            "slug": child["slug"],
            "parent": 4,
            "link": f"{SITE}/category/gyomu-koritsuka/{child['slug']}/",
        }
    for key in CHILD_ORDER:
        assert run(world, "create-next-category", "--execute")[0] == 0
        assert record(world, "category", key)["result"] == "reused"
    assert wp.writes == []  # カテゴリを作る POST は 0
    assert {k: record(world, "category", k)["category_id"] for k in CHILD_ORDER} == {
        "ai": 5,
        "meeting": 6,
        "automation": 7,
        "crm": 8,
        "task": 9,
    }
    assert run(world, "next", "--execute")[0] == 0  # canary は採用した ID を使う
    assert wp.writes == [("set_categories", {"post": 115, "categories": [4, 9]})]


def test_a_near_miss_manual_child_still_blocks_adoption(world) -> None:
    wp = world[0]
    wp.categories[6] = {
        "id": 6,
        "name": "会議・文字起こし",
        "slug": "meeting",
        "parent": 4,
        "link": f"{SITE}/category/gyomu-koritsuka/meeting/",
    }
    assert run(world, "create-next-category", "--execute")[0] == 2
    assert "collision" in record(world, "category", "ai")["reason"] and wp.writes == []
