"""W2A: カテゴリ整理の計画 (読むだけ。WordPress へは一切通信しない)。

pin する契約:

- 公開済みの 25 記事ちょうど。子カテゴリは 5 つで、件数は 7 / 7 / 5 / 3 / 2。
- article 1 は親だけ。ほかの 24 記事は子がちょうど 1 つ。重複なし。
- 親カテゴリは名前で解決する (ID を決め打ちしない)。トップレベルでなければ止める。
- slug・名前が既存のカテゴリ・タグと重なれば止める。
- post の URL にカテゴリが入っていれば (``%category%``) 止める。
- 計画のときの状態からのずれ (タイトル・slug・URL・カテゴリ・親) は書く前に止める。
- 計画は WordPress に書かない。
"""

from __future__ import annotations

import copy
import json

import pytest

from app.wordpress import taxonomy_plan as tp
from app.wordpress.taxonomy_plan import (
    TaxonomyPlanError,
    assignment,
    build_plan,
    drift_problems,
    permalink_problems,
    resolve_parent,
    slug_collisions,
    slug_problems,
)

BASE = "https://bizfluxlab.com"


def _world(parent_id=4, *, link=None, extra_categories=(), tags=()):
    categories = [
        {"id": 1, "name": "未分類", "slug": "uncategorized", "parent": 0, "count": 0},
        {
            "id": parent_id,
            "name": "業務効率化",
            "slug": "gyomu-koritsuka",
            "parent": 0,
            "count": 25,
        },
        *extra_categories,
    ]
    articles, posts = [], []
    for article_id in range(1, 26):
        slug = f"article-{article_id}"
        articles.append(
            {
                "id": article_id,
                "title": f"記事 {article_id}",
                "slug": slug,
                "wordpress_post_id": 100 + article_id,
            }
        )
        posts.append(
            {
                "id": 100 + article_id,
                "slug": slug,
                "status": "publish",
                "title": {"raw": f"記事 {article_id}"},
                "categories": [parent_id],
                "tags": [],
                "link": (link or (lambda s: f"{BASE}/{s}/"))(slug),
                "modified_gmt": "2026-09-25T16:16:02",
            }
        )
    return {"categories": categories, "tags": list(tags), "posts": posts, "articles": articles}


def _plan(world):
    return build_plan(base_url=BASE, generated_at="2026-09-26T00:00:00+00:00", **world)


def test_the_plan_covers_exactly_25_articles_with_the_five_children() -> None:
    plan = _plan(_world())
    assert plan["ready"] is True and plan["problems"] == []
    assert len(plan["articles"]) == 25 and len(plan["planned_children"]) == 5
    counts = {c["slug"]: c["expected_count"] for c in plan["planned_children"]}
    assert counts == {
        "ai-generative-ai": 7,
        "meeting-transcription": 7,
        "nocode-automation-rpa": 5,
        "crm-sales-efficiency": 3,
        "task-project-management": 2,
    }
    assert all(c["parent_id"] == 4 for c in plan["planned_children"])
    by_id = {r["article_id"]: r for r in plan["articles"]}
    assert by_id[1]["planned_child_category"] is None
    assert by_id[1]["action"] == "keep_parent_only"
    assert by_id[1]["planned_category_ids"] == [4]
    for article_id in range(2, 26):
        row = by_id[article_id]
        assert row["action"] == "add_child"
        assert row["planned_category_names"][0] == "業務効率化"
        assert len(row["planned_category_names"]) == 2  # 親 + 子 1 つ
    assert plan["parent_category"]["expected_count_after"] == 25


def test_the_semantic_placements_that_matter() -> None:
    mapping = assignment()
    assert mapping[1] is None
    assert {mapping[i]["key"] for i in (10, 11)} == {"automation"}  # Make は AI ではない
    assert {mapping[i]["key"] for i in (13, 15)} == {"task"}
    assert {mapping[i]["key"] for i in (19, 20, 21, 22, 23, 24, 25)} == {"ai"}
    assert {mapping[i]["key"] for i in (4, 5, 14)} == {"crm"}
    assert {mapping[i]["key"] for i in (2, 3, 6, 7, 8, 9, 12)} == {"meeting"}
    rows = {r["article_id"]: r for r in _plan(_world())["articles"]}
    assert rows[25]["human_review_required"] is True  # cluster B との食い違い
    assert sum(r["human_review_required"] for r in rows.values()) == 1


def test_a_duplicate_or_missing_assignment_is_refused(monkeypatch) -> None:
    children = copy.deepcopy(tp.CHILDREN)
    duplicated = (*children[:-1], {**children[-1], "article_ids": (13, 15, 2)})
    monkeypatch.setattr(tp, "CHILDREN", duplicated)
    with pytest.raises(TaxonomyPlanError, match="assigned twice"):
        assignment()
    missing = (*children[:-1], {**children[-1], "article_ids": (13,)})
    monkeypatch.setattr(tp, "CHILDREN", missing)
    with pytest.raises(TaxonomyPlanError, match="missing \\[15\\]"):
        assignment()


def test_the_parent_is_resolved_by_name_not_by_a_fixed_id() -> None:
    plan = _plan(_world(parent_id=17))
    assert plan["ready"] is True
    assert plan["parent_category"]["id"] == 17
    assert all(c["parent_id"] == 17 for c in plan["planned_children"])
    with pytest.raises(TaxonomyPlanError, match="exactly 1 category"):
        resolve_parent([{"id": 1, "name": "未分類", "parent": 0}])
    with pytest.raises(TaxonomyPlanError, match="exactly 1 category"):
        resolve_parent(
            [{"id": 4, "name": "業務効率化", "parent": 0}, {"id": 9, "name": "業務効率化"}]
        )
    with pytest.raises(TaxonomyPlanError, match="top-level"):
        resolve_parent([{"id": 4, "name": "業務効率化", "parent": 2}])


@pytest.mark.parametrize(
    ("kind", "term"),
    [
        ("category", {"id": 30, "name": "別名", "slug": "crm-sales-efficiency", "parent": 0}),
        ("tag", {"id": 31, "name": "別名", "slug": "meeting-transcription"}),
        ("tag", {"id": 32, "name": "AI・生成AI", "slug": "ai"}),
    ],
)
def test_a_slug_or_name_collision_blocks_the_plan(kind, term) -> None:
    world = _world(extra_categories=[term]) if kind == "category" else _world(tags=[term])
    found = slug_collisions(world["categories"], world["tags"])
    assert found and next(iter(found.values()))[0]["kind"] == kind
    plan = _plan(world)
    assert plan["ready"] is False and plan["checks"]["no_slug_collision"] is False


def test_slug_rules() -> None:
    for child in tp.CHILDREN:
        assert slug_problems(child["slug"]) == []
    assert slug_problems("AI-Tools") and slug_problems("ai_tools")
    assert slug_problems("ai-tools-2026") and slug_problems("crm-v2")


def test_a_category_in_the_post_url_blocks_any_category_write() -> None:
    world = _world(link=lambda s: f"{BASE}/gyomu-koritsuka/{s}/")
    plan = _plan(world)
    assert plan["ready"] is False
    assert plan["checks"]["permalink_has_no_category"] is False
    assert "the URL contains a category" in plan["problems"][0]
    assert permalink_problems(_world()["posts"], base_url=BASE, categories=[]) == []


def test_current_categories_other_than_the_parent_block_the_plan() -> None:
    world = _world()
    world["posts"][4]["categories"] = [4, 1]
    plan = _plan(world)
    assert plan["ready"] is False
    assert plan["checks"]["current_categories_parent_only"] is False


def test_a_title_mismatch_with_the_application_blocks_the_plan() -> None:
    world = _world()
    world["posts"][6]["title"] = {"raw": "別のタイトル"}
    plan = _plan(world)
    assert plan["ready"] is False
    assert any("article 7: title differs" in p for p in plan["problems"])


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"title": {"raw": "変わった"}}, "title drifted"),
        ({"slug": "renamed"}, "slug drifted"),
        ({"link": f"{BASE}/renamed/"}, "URL drifted"),
        ({"categories": [4, 99]}, "categories drifted"),
    ],
)
def test_drift_before_a_future_write_fails_closed(change, message) -> None:
    world = _world()
    row = _plan(world)["articles"][1]
    post = {**world["posts"][1], **change}
    assert any(message in p for p in drift_problems(row, post, parent_id=4))
    assert drift_problems(row, world["posts"][1], parent_id=4) == []
    assert "parent category changed" in drift_problems(row, world["posts"][1], parent_id=5)


class _ReadOnlyWP:
    target_base_url = BASE

    def __init__(self, world):
        self.world = world
        self.reads = []

    def list_categories(self):
        self.reads.append("categories")
        return copy.deepcopy(self.world["categories"])

    def list_tags(self):
        self.reads.append("tags")
        return copy.deepcopy(self.world["tags"])

    def list_post_states(self):
        self.reads.append("posts")
        return copy.deepcopy(self.world["posts"])

    def __getattr__(self, name):
        raise AssertionError(f"the taxonomy plan must not call {name}")


def test_the_cli_writes_the_plan_and_performs_no_wordpress_write(tmp_path, capsys) -> None:
    from scripts.plan_taxonomy import main

    world = _world()
    wp = _ReadOnlyWP(world)
    code = main(["--out-dir", str(tmp_path)], client=wp, articles=world["articles"])
    assert code == 0
    assert sorted(wp.reads) == ["categories", "posts", "tags"]
    plan = json.loads((tmp_path / "w2-category-plan.json").read_text("utf-8"))
    assert plan["taxonomy_version"] == "w2-taxonomy/1" and plan["ready"] is True
    assert (tmp_path / "w2-category-plan.md").exists()
    assert "no category was created" in capsys.readouterr().out


def test_the_client_lists_categories_and_tags_with_get_only() -> None:
    import httpx

    from app.config.settings import Settings
    from app.wordpress.client import WordPressClient

    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=[{"id": 4}], headers={"X-WP-TotalPages": "1"})

    settings = Settings(
        _env_file=None, wordpress_base_url=BASE, wordpress_username="u", wordpress_app_password="p"
    )
    client = WordPressClient(settings, transport=httpx.MockTransport(handler))
    assert client.list_categories() == [{"id": 4}]
    assert client.list_tags() == [{"id": 4}]
    assert {r.method for r in seen} == {"GET"}
    assert [r.url.path for r in seen] == ["/wp-json/wp/v2/categories", "/wp-json/wp/v2/tags"]
    assert all(r.url.params["hide_empty"] == "false" for r in seen)  # 空のカテゴリも見る
