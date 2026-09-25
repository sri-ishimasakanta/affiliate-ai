"""W1.5G: 承認済み 21 枚の適用の計画 (読むだけ)。実際の WordPress へは一切通信しない。

pin する契約:

- 承認の記録 (review plan の表) は 21 枚ちょうど。試作 (20/23/24/25) を含まない。
- media はファイル名だけで再利用しない。SHA-256 が一致し、どこにも添付されず、どの post の
  featured image でもない 1 つだけを再利用する。名前が同じで byte が違う・読めないものは人が見る。
- 今の featured_media が 0 でなければ上書きを計画しない (人が見る)。
- 日本語の slug は WordPress が返すパーセントエンコードの形で manifest に入れる。
- 計画は WordPress に書かない (書く method を呼べば失敗する client で確かめる)。
- apply の道具は、manifest の計画 (upload / reuse / human_review) と食い違えば何も書かない。
"""

from __future__ import annotations

import copy
import hashlib
import json
import struct
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

from app.config.settings import Settings
from app.wordpress.client import WordPressClient
from app.wordpress.featured_image import (
    ApplyItem,
    FeaturedImageError,
    apply_one,
    media_action_problem,
    resolve_post,
    verify_local,
)
from app.wordpress.featured_image_batch import build_batch_package, manifest_sha256
from app.wordpress.featured_image_rollout import (
    ACTION_REUSE,
    ACTION_REVIEW,
    ACTION_UPLOAD,
    EXPECTED_ARTICLE_IDS,
    FEATURED_EXPECTED,
    FEATURED_NONE,
    FEATURED_UNEXPECTED,
    PILOT_MAPPING,
    RolloutPlanError,
    apply_entry,
    audit,
    classify_featured,
    filename_matches,
    parse_approved_hashes,
    plan_media,
)

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "docs" / "operations" / "featured-image-w1.5-manifest.json"
REVIEW_PLAN = ROOT / "docs" / "operations" / "featured-image-w1.5-review-plan.md"
_BASE = "https://wp.example.test"
JAPANESE_SLUG = "業務効率化-ツール-おすすめ-roundup"
ENCODED_SLUG = quote(JAPANESE_SLUG, safe="-").lower()
SHA = "a" * 64


def _vp8x(width: int, height: int, salt: bytes = b"") -> bytes:
    body = (
        b"\x00\x00\x00\x00" + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    )
    chunk = b"VP8X" + struct.pack("<I", len(body)) + body
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk + b"\x00" * 16 + salt


def _media(media_id: int, name: str, *, post=None, mime="image/webp", size=(1200, 675)) -> dict:
    return {
        "id": media_id,
        "mime_type": mime,
        "post": post,
        "source_url": f"{_BASE}/wp-content/uploads/2026/09/{name}",
        "media_details": {"width": size[0], "height": size[1], "file": f"2026/09/{name}"},
        "date_gmt": "2026-09-25T08:15:13",
        "modified_gmt": "2026-09-25T08:15:13",
    }


def _plan(media, hashes, featured=None, file="featured-7-ai-meeting-notes.webp"):
    return plan_media(
        planned_file=file,
        sha256=SHA,
        width=1200,
        height=675,
        media_items=media,
        media_sha256=hashes,
        featured_by_media=featured or {},
    )


# == approval record ===========================================================
def test_the_review_plan_records_exactly_the_21_approved_images() -> None:
    approved = parse_approved_hashes(REVIEW_PLAN.read_text(encoding="utf-8"))
    assert set(approved) == EXPECTED_ARTICLE_IDS
    assert not set(approved) & {20, 23, 24, 25}
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    planned = {a["article_id"]: a["planned_file"] for a in manifest["articles"]}
    assert {i: r["file"] for i, r in approved.items()} == planned
    assert len({r["sha256"] for r in approved.values()}) == 21


def test_a_duplicate_approval_row_is_refused() -> None:
    row = f"| 7 | featured-7-ai-meeting-notes.webp | `{SHA}` |\n"
    with pytest.raises(RolloutPlanError, match="twice"):
        parse_approved_hashes(row + row)


# == media planning ============================================================
def test_file_name_matching_includes_the_wordpress_collision_suffix() -> None:
    planned = "featured-7-ai-meeting-notes.webp"
    assert filename_matches(planned, _media(1, planned))
    assert filename_matches(planned, _media(1, "featured-7-ai-meeting-notes-1.webp"))
    assert not filename_matches(planned, _media(1, "featured-7-ai-meeting-notes-free.webp"))
    assert not filename_matches(planned, _media(1, "featured-7-ai-meeting-notes.png"))


def test_no_existing_media_means_upload() -> None:
    plan = _plan([_media(5, "other.webp")], {5: "b" * 64})
    assert (plan["action"], plan["media_id"]) == (ACTION_UPLOAD, None)


@pytest.mark.parametrize("known_sha", [None, "b" * 64])
def test_the_same_file_name_without_identical_bytes_is_never_reused(known_sha) -> None:
    plan = _plan([_media(5, "featured-7-ai-meeting-notes.webp")], {5: known_sha})
    assert plan["action"] == ACTION_REVIEW
    assert plan["media_id"] is None
    assert plan["filename_collision_media_ids"] == [5]


def test_a_byte_identical_unused_media_is_reused_even_under_another_name() -> None:
    plan = _plan([_media(5, "something-else.webp")], {5: SHA})
    assert (plan["action"], plan["media_id"]) == (ACTION_REUSE, 5)


@pytest.mark.parametrize(
    ("media", "featured", "message"),
    [
        ([_media(5, "x.webp", post=40)], {}, "attached to post 40"),
        ([_media(5, "x.webp")], {5: [36]}, "already featured by [36]"),
        ([_media(99, "x.webp")], {}, "must not be touched"),
        ([_media(5, "x.webp"), _media(6, "y.webp")], {}, "more than one"),
        ([_media(5, "x.webp", size=(640, 360))], {}, "not 1200x675"),
    ],
)
def test_a_byte_identical_media_that_is_not_safe_goes_to_a_human(media, featured, message):
    hashes = {m["id"]: SHA for m in media}
    plan = _plan(media, hashes, featured)
    assert plan["action"] == ACTION_REVIEW
    assert message in plan["reason"]


def test_featured_state_classification() -> None:
    hashes = {100: SHA, 101: "b" * 64}
    assert classify_featured(0, sha256=SHA, media_sha256=hashes) == FEATURED_NONE
    assert classify_featured(None, sha256=SHA, media_sha256=hashes) == FEATURED_NONE
    assert classify_featured(100, sha256=SHA, media_sha256=hashes) == FEATURED_EXPECTED
    assert classify_featured(101, sha256=SHA, media_sha256=hashes) == FEATURED_UNEXPECTED
    assert classify_featured(102, sha256=SHA, media_sha256=hashes) == FEATURED_UNEXPECTED


def _entry(article_id=1, featured=0, state=FEATURED_NONE, action=ACTION_UPLOAD, post_id=25):
    article = {
        "article_id": article_id,
        "slug": JAPANESE_SLUG,
        "title": "業務効率化ツールおすすめ｜選び方と目的別に比較",
        "alt_text": "alt",
        "media_title": "t アイキャッチ",
    }
    local = {
        "file": "f.webp",
        "source": "batch-3/f.webp",
        "sha256": SHA,
        "width": 1200,
        "height": 675,
        "approved": True,
    }
    post = {
        "id": post_id,
        "slug": ENCODED_SLUG,
        "featured_media": featured,
        "modified_gmt": "2026-09-16T15:54:53",
        "categories": [3],
    }
    media_plan = {
        "action": action,
        "reason": "r",
        "media_id": 7 if action == ACTION_REUSE else None,
        "byte_identical_media_ids": [],
        "filename_collision_media_ids": [],
    }
    return apply_entry(
        article=article,
        batch=3,
        local=local,
        post=post,
        media_plan=media_plan,
        featured_state=state,
    )


def test_an_entry_uses_the_wordpress_slug_and_records_the_rollback_state() -> None:
    entry = _entry()
    assert entry["slug"] == entry["wordpress_slug"] == ENCODED_SLUG
    assert entry["application_slug"] == entry["wordpress_slug_decoded"] == JAPANESE_SLUG
    assert (entry["original_featured_media"], entry["original_modified_gmt"]) == (
        0,
        "2026-09-16T15:54:53",
    )
    assert entry["file"] == "f.webp" and entry["source"] == "batch-3/f.webp"
    assert entry["apply_status"] == "planned" and entry["human_approved_image"] is True


def test_an_existing_featured_image_is_never_planned_for_overwrite() -> None:
    entry = _entry(featured=55, state=FEATURED_UNEXPECTED)
    assert entry["media_action"] == ACTION_REVIEW
    assert entry["expected_featured_media_after_apply"] is None
    assert "not overwriting" in entry["media_reason"]


def test_the_audit_is_ready_only_with_21_clean_entries() -> None:
    entries = [_entry(article_id=i, post_id=100 + i) for i in sorted(EXPECTED_ARTICLE_IDS)]
    media_99 = {"untouched": True}
    assert audit(entries, pilots=PILOT_MAPPING, media_99=media_99)["ready"] is True
    assert audit(entries[:-1], pilots=PILOT_MAPPING, media_99=media_99)["ready"] is False
    changed = {**PILOT_MAPPING, 78: 99}
    result = audit(entries, pilots=changed, media_99=media_99)
    assert result["checks"]["pilot_mappings_unchanged"] is False
    review = [*entries[:-1], _entry(article_id=22, post_id=122, action=ACTION_REVIEW)]
    assert audit(review, pilots=PILOT_MAPPING, media_99=media_99)["ready"] is False
    with_pilot = [*entries[:-1], _entry(article_id=25, post_id=200)]
    assert audit(with_pilot, pilots=PILOT_MAPPING, media_99=media_99)["ready"] is False


# == apply tool guards =========================================================
def _item(**overrides) -> ApplyItem:
    base = dict(
        article_id=1,
        slug=ENCODED_SLUG,
        title="業務効率化ツールおすすめ｜選び方と目的別に比較",
        file="f.webp",
        alt_text="alt",
        sha256=SHA,
        width=1200,
        height=675,
        mime_type="image/webp",
    )
    base.update(overrides)
    return ApplyItem(**base)


def test_the_apply_manifest_fields_are_read_and_the_source_path_is_used(tmp_path) -> None:
    data = _vp8x(1200, 675)
    (tmp_path / "batch-3").mkdir()
    (tmp_path / "batch-3" / "f.webp").write_bytes(data)
    item = ApplyItem.from_dict(
        {
            **_item(sha256=hashlib.sha256(data).hexdigest()).__dict__,
            "source": "batch-3/f.webp",
            "media_action": "reuse",
            "planned_media_id": 7,
        }
    )
    assert (item.source, item.media_action, item.planned_media_id) == ("batch-3/f.webp", "reuse", 7)
    assert verify_local(item, tmp_path) == data
    with pytest.raises(FeaturedImageError, match="plain file name"):
        verify_local(_item(file="batch-3/f.webp"), tmp_path)


@pytest.mark.parametrize(
    ("action", "planned", "given", "problem"),
    [
        (None, None, None, None),
        (None, None, 98, None),  # W1.4 の manifest: 人が --media-id を決める
        ("upload", None, None, None),
        ("upload", None, 98, "--media-id is not allowed"),
        ("reuse", 7, 7, None),
        ("reuse", 7, None, "pass --media-id to match"),
        ("reuse", 7, 8, "pass --media-id to match"),
        ("human_review", None, None, "a human must resolve"),
    ],
)
def test_the_media_plan_must_match_what_the_operator_passes(action, planned, given, problem):
    item = _item(media_action=action, planned_media_id=planned)
    result = media_action_problem(item, given)
    assert (result is None) if problem is None else (problem in result)


class _NoWriteWP:
    """書く method を呼んだら失敗する (計画が書かないことを確かめる)。"""

    def __init__(self, posts, media=(), files=None):
        self.posts = posts
        self.media = list(media)
        self.files = files or {}
        self.reads = []

    def find_published_posts_by_slug(self, slug):
        self.reads.append(("find", slug))
        wanted = quote(slug, safe="-%").lower() if "%" not in slug else slug.lower()
        return [
            copy.deepcopy(p)
            for p in self.posts
            if p["status"] == "publish" and p["slug"] in (slug, wanted)
        ]

    def list_post_states(self):
        return copy.deepcopy(self.posts)

    def list_media_items(self):
        return copy.deepcopy(self.media)

    def fetch_media_file(self, url):
        return self.files[url]

    def get_post(self, post_id):
        return copy.deepcopy(next(p for p in self.posts if p["id"] == post_id))

    def __getattr__(self, name):
        if name.endswith("_exact"):
            raise AssertionError(f"the plan must not call {name}")
        raise AttributeError(name)


def test_apply_refuses_a_human_review_item_before_any_call(tmp_path) -> None:
    wp = _NoWriteWP([])
    with pytest.raises(FeaturedImageError, match="a human must resolve"):
        apply_one(wp, _item(media_action="human_review"), tmp_path, tmp_path / "applied")
    assert wp.reads == []


def test_the_percent_encoded_slug_resolves_and_the_decoded_one_does_not_match() -> None:
    post = {
        "id": 25,
        "slug": ENCODED_SLUG,
        "status": "publish",
        "title": {"raw": "業務効率化ツールおすすめ｜選び方と目的別に比較"},
        "featured_media": 0,
    }
    wp = _NoWriteWP([post])
    assert resolve_post(wp, _item())["id"] == 25
    # manifest の日本語の slug のままだと、返ってきた slug と完全一致しないので止まる。
    with pytest.raises(FeaturedImageError, match="exact slug"):
        resolve_post(wp, _item(slug=JAPANESE_SLUG))


def test_the_client_lists_media_with_get_only() -> None:
    seen = []

    def handler(request):
        seen.append(request)
        page = int(request.url.params["page"])
        return httpx.Response(200, json=[{"id": page}], headers={"X-WP-TotalPages": "2"})

    settings = Settings(
        _env_file=None,
        wordpress_base_url=_BASE,
        wordpress_username="u",
        wordpress_app_password="p",
    )
    client = WordPressClient(settings, transport=httpx.MockTransport(handler))
    assert [m["id"] for m in client.list_media_items()] == [1, 2]
    assert {r.method for r in seen} == {"GET"}
    assert seen[0].url.path == "/wp-json/wp/v2/media"
    assert seen[0].url.params["context"] == "view"  # edit だと他の利用者の media が消える


# == the plan end to end (fake WordPress, no writes) ============================
def _fake_rollout(tmp_path: Path, *, featured_override=None):
    """21 枚の合成の画像と、25 件の post を持つ偽の WordPress。"""

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    digest = manifest_sha256(MANIFEST_PATH)
    directory = tmp_path / "w1.5"
    rows = []
    for batch in manifest["batches"]:
        package = build_batch_package(
            manifest, batch["batch"], manifest_path="x", manifest_sha256=digest
        )
        batch_dir = directory / f"batch-{batch['batch']}"
        batch_dir.mkdir(parents=True)
        (batch_dir / "batch-manifest.json").write_text(json.dumps(package), encoding="utf-8")
        composed = {}
        for item in package["items"]:
            data = _vp8x(1200, 675, salt=str(item["article_id"]).encode())
            (batch_dir / item["files"]["final_webp"]).write_bytes(data)
            sha = hashlib.sha256(data).hexdigest()
            composed[str(item["article_id"])] = {"final_webp": {"sha256": sha}}
            rows.append(f"| {item['article_id']} | {item['files']['final_webp']} | `{sha}` |")
        (batch_dir / "compose-report.json").write_text(
            json.dumps({"composed": composed}), encoding="utf-8"
        )
    review = tmp_path / "review.md"
    review.write_text("\n".join(rows) + "\n", encoding="utf-8")
    posts = []
    for index, article in enumerate(manifest["articles"]):
        slug = quote(article["slug"], safe="-").lower()
        posts.append(
            {
                "id": 300 + index,
                "slug": slug,
                "status": "publish",
                "title": {"raw": article["title"]},
                "featured_media": (featured_override or {}).get(article["article_id"], 0),
                "modified_gmt": "2026-09-22T10:00:00",
                "categories": [3],
                "link": f"{_BASE}/{slug}/",
            }
        )
    posts += [
        {"id": pid, "slug": f"pilot-{pid}", "status": "publish", "featured_media": mid}
        for pid, mid in PILOT_MAPPING.items()
    ]
    pilot_media = [_media(mid, f"pilot-{mid}.webp", post=None) for mid in PILOT_MAPPING.values()]
    media = [*pilot_media, _media(99, "featured-25-ai-business-efficiency.webp")]
    files = {m["source_url"]: f"bytes-{m['id']}".encode() for m in media}
    return _NoWriteWP(posts, media, files), directory, review


def test_the_plan_covers_21_articles_and_writes_nothing(tmp_path) -> None:
    from scripts.plan_featured_image_rollout import build_plan

    wp, directory, review = _fake_rollout(tmp_path)
    plan = build_plan(wp, directory=directory, review_plan_path=review)
    assert plan["problems"] == []
    assert plan["audit"]["ready"] is True, plan["audit"]
    ids = [e["article_id"] for e in plan["items"]]
    assert len(ids) == 21 and set(ids) == EXPECTED_ARTICLE_IDS
    assert ids[:5] == [7, 2, 6, 8, 9]  # 承認したバッチの順
    assert all(e["media_action"] == ACTION_UPLOAD for e in plan["items"])
    article_1 = next(e for e in plan["items"] if e["article_id"] == 1)
    assert article_1["slug"] == ENCODED_SLUG and article_1["application_slug"] == JAPANESE_SLUG
    assert plan["media_99"]["untouched"] is True
    assert ("find", ENCODED_SLUG) in wp.reads  # WordPress の形でもう一度 1 件に決まる
    # apply の道具がこの manifest をそのまま読める。
    from app.wordpress.featured_image import ApplyItem as Item

    items = [Item.from_dict(e) for e in plan["items"]]
    assert all(i.source and "/" not in i.file for i in items)


def test_the_plan_is_not_ready_when_a_post_already_has_a_featured_image(tmp_path) -> None:
    from scripts.plan_featured_image_rollout import build_plan

    wp, directory, review = _fake_rollout(tmp_path, featured_override={9: 555})
    plan = build_plan(wp, directory=directory, review_plan_path=review)
    entry = next(e for e in plan["items"] if e["article_id"] == 9)
    assert (entry["featured_state"], entry["media_action"]) == (FEATURED_UNEXPECTED, ACTION_REVIEW)
    assert plan["audit"]["ready"] is False
    assert plan["approved"] is False


def test_the_plan_stops_on_a_file_that_is_not_the_approved_one(tmp_path) -> None:
    from scripts.plan_featured_image_rollout import build_plan

    wp, directory, review = _fake_rollout(tmp_path)
    target = directory / "batch-1" / "featured-7-ai-meeting-notes.webp"
    target.write_bytes(target.read_bytes() + b"changed")
    plan = build_plan(wp, directory=directory, review_plan_path=review)
    assert plan["audit"]["ready"] is False
    assert any("article 7" in p and "compose report" in p for p in plan["problems"])
    assert any("article 7" in p and "approved" in p for p in plan["problems"])


def test_media_99_changed_since_upload_is_not_untouched(tmp_path) -> None:
    from scripts.plan_featured_image_rollout import build_plan

    wp, directory, review = _fake_rollout(tmp_path)
    for media in wp.media:
        if media["id"] == 99:
            media["modified_gmt"] = "2026-09-26T00:00:00"
    plan = build_plan(wp, directory=directory, review_plan_path=review)
    assert plan["media_99"]["untouched"] is False
    assert plan["audit"]["checks"]["media_99_untouched"] is False
    assert plan["audit"]["ready"] is False
