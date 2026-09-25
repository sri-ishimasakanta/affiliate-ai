"""W1.5H: 本番の適用を 1 記事ずつ行う道具 (偽の WordPress と偽の公開ページ。通信しない)。

pin する契約:

- 既定は読むだけ (書き込み 0)。``--execute`` のときだけ、次の 1 記事だけを書く。
- 書く前に: 計画の post・タイトル・slug・featured_media・modified_gmt・ローカルの SHA-256・
  media の名前と byte の衝突・試作 4 件と media 95 / 99 を確かめる。1 つでも違えば書かない。
- 書いたあと: REST の読み戻しと公開ページ (キャッシュを避けた URL) を確かめる。
- 失敗した記事があれば、次の記事には進まない。
- 公開ページの確認だけの道 (試作の記事の確認) は WordPress の client を使わない。
"""

from __future__ import annotations

import copy
import hashlib
import json
import struct
from pathlib import Path

import pytest

from app.wordpress.client import WordPressUploadedMedia
from scripts.rollout_featured_image import main

_BASE = "https://bizfluxlab.com"
_UPLOADS = f"{_BASE}/wp-content/uploads/2026/09"
_PILOTS = {78: 100, 74: 98, 76: 97, 72: 96}


def _webp(salt: bytes) -> bytes:
    body = b"\x00\x00\x00\x00" + (1199).to_bytes(3, "little") + (674).to_bytes(3, "little")
    chunk = b"VP8X" + struct.pack("<I", len(body)) + body
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk + b"\x00" * 16 + salt


ARTICLES = [
    {
        "article_id": 7,
        "post_id": 50,
        "slug": "ai-meeting-notes",
        "file": "featured-7-a.webp",
        "title": "AI議事録｜意味・種類・選び方の基礎知識",
        "alt": "AI議事録の仕組みを表す概念図",
    },
    {
        "article_id": 2,
        "post_id": 36,
        "slug": "ai-meeting-notes-tools",
        "file": "featured-2-b.webp",
        "title": "AI議事録おすすめ｜選び方と目的別の比較",
        "alt": "AI議事録の選び方を表す図",
    },
]


class FakeWP:
    """必要な read / write だけを持つ WordPress。書き込みは ``writes`` に数える。"""

    def __init__(self) -> None:
        self.writes: list[str] = []
        self.posts: dict[int, dict] = {}
        for a in ARTICLES:
            self.posts[a["post_id"]] = {
                "id": a["post_id"],
                "slug": a["slug"],
                "status": "publish",
                "title": {"raw": a["title"]},
                "content": {"raw": "<p>本文</p>"},
                "excerpt": {"raw": ""},
                "categories": [4],
                "tags": [],
                "date_gmt": "2026-09-22T10:00:00",
                "modified_gmt": "2026-09-22T10:11:34",
                "featured_media": 0,
                "link": f"{_BASE}/{a['slug']}/",
            }
        for post_id, media_id in _PILOTS.items():
            self.posts[post_id] = {
                "id": post_id,
                "slug": f"pilot-{post_id}",
                "status": "publish",
                "title": {"raw": "pilot"},
                "featured_media": media_id,
                "link": f"{_BASE}/pilot-{post_id}/",
                "modified_gmt": "2026-09-25T08:20:00",
            }
        self.media: dict[int, dict] = {}
        self.files: dict[str, bytes] = {}
        self._add_media(95, "ChatGPT-Image.png", b"png", mime="image/png")
        for media_id in (96, 97, 98, 99, 100):
            self._add_media(media_id, f"pilot-{media_id}.webp", f"pilot-{media_id}".encode())
        self.next_id = 101

    def _add_media(self, media_id, name, data, *, mime="image/webp"):
        url = f"{_UPLOADS}/{name}"
        self.media[media_id] = {
            "id": media_id,
            "mime_type": mime,
            "post": None,
            "source_url": url,
            "media_details": {"width": 1200, "height": 675, "file": f"2026/09/{name}"},
            "date_gmt": "2026-09-25T08:15:13",
            "modified_gmt": "2026-09-25T08:15:13",
            "alt_text": "",
            "title": {"raw": name},
        }
        self.files[url] = data

    # -- reads
    def find_published_posts_by_slug(self, slug):
        return [
            copy.deepcopy(p)
            for p in self.posts.values()
            if p["slug"] == slug and p["status"] == "publish"
        ]

    def get_post(self, post_id):
        return copy.deepcopy(self.posts[post_id])

    def list_post_states(self):
        return [copy.deepcopy(p) for p in self.posts.values()]

    def list_media_items(self):
        return [copy.deepcopy(m) for m in self.media.values()]

    def get_media(self, media_id):
        return copy.deepcopy(self.media[media_id])

    def fetch_media_file(self, url):
        return self.files[url]

    # -- writes
    def upload_featured_image_exact(self, image, *, filename, mime_type):
        self.writes.append("upload")
        media_id = self.next_id
        self.next_id += 1
        self._add_media(media_id, filename, image)
        return WordPressUploadedMedia(
            id=media_id,
            source_url=self.media[media_id]["source_url"],
            mime_type=mime_type,
            width=1200,
            height=675,
        )

    def update_media_text_exact(self, media_id, payload_json):
        self.writes.append("media_text")
        payload = json.loads(payload_json)
        self.media[media_id]["alt_text"] = payload["alt_text"]
        self.media[media_id]["title"] = {"raw": payload["title"]}
        return copy.deepcopy(self.media[media_id])

    def set_featured_media_exact(self, post_id, payload_json):
        self.writes.append("featured")
        self.posts[post_id]["featured_media"] = json.loads(payload_json)["featured_media"]
        self.posts[post_id]["modified_gmt"] = "2026-09-26T09:00:00"
        return copy.deepcopy(self.posts[post_id])


def fake_pages(wp: FakeWP, *, stale=False, calls=None):
    """WordPress の今の状態から公開ページを作る。``stale`` なら一覧がいつまでも NO IMAGE。"""

    def page_for(post):
        media = wp.media.get(post.get("featured_media") or 0)
        if not media:
            return '<html><meta property="og:image" content="x/no-image.png"></html>'
        src = media["source_url"]
        return (
            f'<meta property="og:image" content="{src}">'
            f'<meta name="twitter:image" content="{src}">'
            '<meta name="twitter:card" content="summary_large_image">'
            f'<figure class="eye-catch" itemprop="image"><img width="1200" src="{src}" '
            f'class="eye-catch-image" alt="{media["alt_text"]}"></figure>'
        )

    def http_get(url, *, bust):
        if calls is not None:
            calls.append((url, bust))
        if url.startswith(f"{_BASE}/category/"):
            if "/page/" in url:
                return 404, ""
            cards = []
            for post in wp.posts.values():
                media = wp.media.get(post.get("featured_media") or 0)
                img = (
                    media["source_url"].replace(".webp", "-640x360.webp")
                    if media and not stale
                    else f"{_BASE}/no-image-320.png"
                )
                cards.append(
                    f'<a href="{post["link"]}" class="entry-card-wrap a-wrap"><img src="{img}"></a>'
                )
            return 200, "".join(cards)
        post = next((p for p in wp.posts.values() if p["link"] == url), None)
        return (200, page_for(post)) if post else (404, "")

    return http_get


def _setup(tmp_path: Path, *, approved=True):
    directory = tmp_path / "w1.5"
    (directory / "batch-1").mkdir(parents=True)
    items = []
    for a in ARTICLES:
        data = _webp(a["file"].encode())
        (directory / "batch-1" / a["file"]).write_bytes(data)
        items.append(
            {
                "article_id": a["article_id"],
                "slug": a["slug"],
                "title": a["title"],
                "file": a["file"],
                "source": f"batch-1/{a['file']}",
                "alt_text": a["alt"],
                "sha256": hashlib.sha256(data).hexdigest(),
                "width": 1200,
                "height": 675,
                "mime_type": "image/webp",
                "media_action": "upload",
                "planned_media_id": None,
                "wordpress_post_id": a["post_id"],
                "media_title": f"{a['title']} アイキャッチ",
                "original_featured_media": 0,
                "original_modified_gmt": "2026-09-22T10:11:34",
            }
        )
    manifest = {
        "schema": "featured-image-wordpress-apply/1",
        "approved": approved,
        "rollout_order": [a["article_id"] for a in ARTICLES],
        "items": items,
    }
    (directory / "wordpress-apply-manifest.json").write_text(json.dumps(manifest), "utf-8")
    return directory


def _run(directory, wp, *args, http_get=None, capsys=None):
    code = main(
        ["--dir", str(directory), *args],
        client=wp,
        http_get=http_get or fake_pages(wp),
        sleep=lambda _s: None,
    )
    out = capsys.readouterr().out if capsys else ""
    return code, out


def _result(directory, article_id):
    return json.loads((directory / "rollout" / f"article-{article_id}.json").read_text("utf-8"))


# == read-only by default =======================================================
def test_the_default_mode_writes_nothing(tmp_path, capsys) -> None:
    directory, wp = _setup(tmp_path), FakeWP()
    code, out = _run(directory, wp, "next", capsys=capsys)
    assert code == 0
    assert wp.writes == []
    assert '"result": "ready"' in out and "read-only" in out
    assert not (directory / "rollout").exists()  # 読むだけの確認は結果を残さない
    assert not (directory / "applied").exists()


def test_execute_applies_exactly_the_next_article_and_verifies_it(tmp_path) -> None:
    directory, wp = _setup(tmp_path), FakeWP()
    code, _ = _run(directory, wp, "next", "--execute")
    assert code == 0
    assert wp.writes == ["upload", "media_text", "featured"]  # 1 記事ぶんだけ
    result = _result(directory, 7)
    assert (result["result"], result["stage"]) == ("applied", "done")
    assert result["rest"]["media_id"] == 101 and all(result["rest"]["checks"].values())
    assert all(result["public"]["checks"].values())
    assert wp.posts[50]["featured_media"] == 101
    assert wp.posts[36]["featured_media"] == 0  # 次の記事には触れていない
    assert not (directory / "rollout" / "article-2.json").exists()

    # もう一度: 順番どおり次の 1 記事 (article 2) だけ。
    code, _ = _run(directory, wp, "next", "--execute")
    assert code == 0 and wp.writes.count("upload") == 2
    assert _result(directory, 2)["result"] == "applied"
    code, _ = _run(directory, wp, "next", "--execute")
    assert code == 0 and wp.writes.count("upload") == 2  # 全部済み: 何もしない


# == fail closed before any write ===============================================
def _drift_title(wp):
    wp.posts[50]["title"] = {"raw": "別のタイトル"}


def _drift_slug(wp):
    wp.posts[50]["slug"] = "renamed"


def _drift_featured(wp):
    wp.posts[50]["featured_media"] = 555


def _drift_modified(wp):
    wp.posts[50]["modified_gmt"] = "2026-09-26T00:00:00"


def _collision_same_name(wp):
    wp._add_media(300, "featured-7-a.webp", b"other")


def _collision_suffix(wp):
    wp._add_media(300, "featured-7-a-1.webp", b"other")


def _pilot_changed(wp):
    wp.posts[78]["featured_media"] = 99


def _media_99_touched(wp):
    wp.media[99]["post"] = 50


@pytest.mark.parametrize(
    ("drift", "reason"),
    [
        (_drift_title, "title differs"),
        (_drift_slug, "slug resolves to []"),
        (_drift_featured, "featured_media is 555"),
        (_drift_modified, "modified_gmt changed"),
        (_collision_same_name, "media collision"),
        (_collision_suffix, "media collision"),
        (_pilot_changed, "pilot mapping changed"),
        (_media_99_touched, "media 99"),
    ],
)
def test_any_drift_stops_before_a_write(tmp_path, drift, reason) -> None:
    directory, wp = _setup(tmp_path), FakeWP()
    drift(wp)
    code, _ = _run(directory, wp, "next", "--execute")
    assert code == 2
    assert wp.writes == []
    result = _result(directory, 7)
    assert result["result"] == "stopped" and result["stage"] == "pre"
    assert reason in result["reason"]


def test_a_byte_identical_media_already_in_the_library_stops(tmp_path) -> None:
    directory, wp = _setup(tmp_path), FakeWP()
    data = (directory / "batch-1" / "featured-7-a.webp").read_bytes()
    wp._add_media(300, "renamed.webp", data)
    code, _ = _run(directory, wp, "next", "--execute")
    assert code == 2 and wp.writes == []
    assert "same bytes [300]" in _result(directory, 7)["reason"]


def test_a_local_file_that_is_not_the_approved_one_stops(tmp_path) -> None:
    directory, wp = _setup(tmp_path), FakeWP()
    path = directory / "batch-1" / "featured-7-a.webp"
    path.write_bytes(path.read_bytes() + b"changed")
    code, _ = _run(directory, wp, "next", "--execute")
    assert code == 2 and wp.writes == []
    assert "SHA-256" in _result(directory, 7)["reason"]


def test_an_unapproved_manifest_is_refused(tmp_path, capsys) -> None:
    directory, wp = _setup(tmp_path, approved=False), FakeWP()
    code, out = _run(directory, wp, "next", "--execute", capsys=capsys)
    assert code == 2 and wp.writes == [] and "not approved" in out


# == stop after a failure =======================================================
def test_a_public_check_failure_stops_and_blocks_the_next_article(tmp_path, capsys) -> None:
    directory, wp = _setup(tmp_path), FakeWP()
    code, _ = _run(directory, wp, "next", "--execute", http_get=fake_pages(wp, stale=True))
    assert code == 2
    result = _result(directory, 7)
    assert (result["result"], result["stage"]) == ("stopped", "public")
    assert "category_card_image" in result["reason"]
    assert result["public"]["attempt"] == 6  # 待ってから確かめ直した (読むだけ)
    assert wp.writes == ["upload", "media_text", "featured"]  # 書いたことは記録に残る
    assert result["apply_record"]["media_id"] == 101

    # 失敗が残っている間は、次の記事に進まない (書き込みも読み取りの確認もしない)。
    code, out = _run(directory, wp, "next", "--execute", capsys=capsys)
    assert code == 2 and "stopped; a human must resolve" in out
    assert wp.writes == ["upload", "media_text", "featured"]
    assert wp.posts[36]["featured_media"] == 0


def test_a_rest_readback_failure_stops_before_the_public_check(tmp_path) -> None:
    directory, wp = _setup(tmp_path), FakeWP()

    class Mislabelled(FakeWP):
        def update_media_text_exact(self, media_id, payload_json):
            super().update_media_text_exact(media_id, payload_json)
            self.media[media_id]["title"] = {"raw": "wrong"}
            return copy.deepcopy(self.media[media_id])

    wp = Mislabelled()
    code, _ = _run(directory, wp, "next", "--execute")
    assert code == 2
    result = _result(directory, 7)
    assert (result["stage"], result["result"]) == ("rest", "stopped")
    assert "media_title" in result["reason"]


def test_status_reports_progress_without_touching_wordpress(tmp_path, capsys) -> None:
    directory, wp = _setup(tmp_path), FakeWP()
    _run(directory, wp, "next", "--execute")
    code, out = _run(directory, wp, "status", capsys=capsys)
    assert code == 0
    assert json.loads(out.strip().splitlines()[-1]) == {"done": [7], "stopped": [], "pending": [2]}


# == the pilot verification path is read-only ===================================
def test_check_public_uses_only_public_pages(tmp_path, capsys) -> None:
    wp = FakeWP()
    wp.media[100]["alt_text"] = "pilot alt"
    calls = []

    class NoClient:
        def __getattr__(self, name):  # pragma: no cover - must not be used
            raise AssertionError(f"check-public must not use the WordPress client ({name})")

    code = main(
        [
            "check-public",
            "--link",
            f"{_BASE}/pilot-78/",
            "--stem",
            "pilot-100",
            "--alt",
            "pilot alt",
        ],
        client=NoClient(),
        http_get=fake_pages(wp, calls=calls),
        sleep=lambda _s: None,
    )
    assert code == 0
    assert '"ok": true' in capsys.readouterr().out
    assert wp.writes == []
    assert {bust for _url, bust in calls} == {True, False}  # キャッシュを避けた URL とふつうの URL
