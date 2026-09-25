"""W1.4 featured image: WordPressClient の exact 契約と、1 記事ずつの適用手順。

実際の WordPress へは一切通信しない (httpx.MockTransport と偽の client を使う)。

pin する契約:

- upload は画像の生バイトを 1 回だけ送る。MIME は webp / png のみ。再送しない。
- media の更新は exact ``{"alt_text","title"}``、post の更新は exact ``{"featured_media": int}``。
  本文・タイトル・状態・カテゴリを変える第 2 のキーは構造的に送れない。
- 公開済み・slug 1 件・タイトル完全一致・featured_media 未設定のときだけ書く。
- 同じ slug で 2 回 upload しない。途中で止まったら記録を残す。
- read-back で featured_media の一致と、他の項目が変わっていないことを確かめる。
"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import httpx
import pytest

from app.config.settings import Settings
from app.exceptions import WordPressAmbiguousOutcomeError
from app.wordpress.client import WordPressClient, WordPressUploadedMedia
from app.wordpress.featured_image import (
    ApplyItem,
    FeaturedImageError,
    apply_one,
    compare_snapshots,
    post_fingerprint,
    verify_local,
    webp_dimensions,
)

_BASE = "https://wp.example.test"


def _client(handler) -> WordPressClient:
    settings = Settings(
        _env_file=None,
        wordpress_base_url=_BASE,
        wordpress_username="wp-user-secret",
        wordpress_app_password="aaaa bbbb cccc dddd",
    )
    return WordPressClient(settings, transport=httpx.MockTransport(handler))


def _vp8x(width: int, height: int) -> bytes:
    body = (
        b"\x00\x00\x00\x00" + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    )
    chunk = b"VP8X" + struct.pack("<I", len(body)) + body
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk + b"\x00" * 16


# == client: upload ============================================================
def test_upload_sends_raw_bytes_once_with_image_headers() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            201,
            json={
                "id": 501,
                "source_url": f"{_BASE}/wp-content/uploads/2026/09/featured-25.webp",
                "mime_type": "image/webp",
                "media_details": {"width": 1200, "height": 675},
            },
        )

    image = _vp8x(1200, 675)
    result = _client(handler).upload_featured_image_exact(
        image, filename="featured-25.webp", mime_type="image/webp"
    )
    assert len(seen) == 1
    request = seen[0]
    assert (request.method, request.url.path) == ("POST", "/wp-json/wp/v2/media")
    assert request.content == image
    assert request.headers["content-type"] == "image/webp"
    assert request.headers["content-disposition"] == 'attachment; filename="featured-25.webp"'
    assert result == WordPressUploadedMedia(
        id=501,
        source_url=f"{_BASE}/wp-content/uploads/2026/09/featured-25.webp",
        mime_type="image/webp",
        width=1200,
        height=675,
    )


@pytest.mark.parametrize(
    ("filename", "mime"),
    [
        ("featured.gif", "image/gif"),
        ("featured.png", "image/webp"),
        ("../featured.webp", "image/webp"),
        ('a"b.webp', "image/webp"),
        (".webp", "image/webp"),
    ],
)
def test_upload_refuses_other_types_and_unsafe_names(filename, mime) -> None:
    def handler(request):  # pragma: no cover - must not be called
        raise AssertionError("no request may be sent")

    with pytest.raises(ValueError):
        _client(handler).upload_featured_image_exact(b"x", filename=filename, mime_type=mime)


def test_an_upload_timeout_is_ambiguous_and_not_retried() -> None:
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(WordPressAmbiguousOutcomeError):
        _client(handler).upload_featured_image_exact(
            _vp8x(1200, 675), filename="f.webp", mime_type="image/webp"
        )
    assert len(calls) == 1


# == client: exact payloads =====================================================
@pytest.mark.parametrize(
    "payload",
    [
        {"alt_text": "a"},
        {"alt_text": "a", "title": "t", "caption": "c"},
        {"alt_text": "a", "title": ""},
        {"alt_text": "a", "title": "t", "post": 5},
    ],
)
def test_media_text_update_accepts_only_alt_and_title(payload) -> None:
    with pytest.raises(ValueError):
        _client(lambda r: httpx.Response(200)).update_media_text_exact(9, json.dumps(payload))


@pytest.mark.parametrize(
    "payload",
    [
        {"featured_media": 0},
        {"featured_media": -1},
        {"featured_media": True},
        {"featured_media": "5"},
        {"featured_media": 5, "title": "x"},
        {"featured_media": 5, "content": "x"},
        {"featured_media": 5, "status": "draft"},
        {"featured_media": 5, "categories": [1]},
    ],
)
def test_featured_media_update_accepts_only_featured_media(payload) -> None:
    with pytest.raises(ValueError):
        _client(lambda r: httpx.Response(200)).set_featured_media_exact(7, json.dumps(payload))


def test_featured_media_update_sends_the_exact_body() -> None:
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"id": 7, "featured_media": 501})

    _client(handler).set_featured_media_exact(7, '{"featured_media": 501}')
    assert seen[0].url.path == "/wp-json/wp/v2/posts/7"
    assert json.loads(seen[0].content) == {"featured_media": 501}


# == client: read-only ==========================================================
def test_published_lookup_and_state_listing_are_get_only() -> None:
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.params.get("slug"):
            return httpx.Response(200, json=[{"id": 78, "slug": "ai-business-efficiency"}])
        page = int(request.url.params["page"])
        return httpx.Response(200, json=[{"id": page}], headers={"X-WP-TotalPages": "2"})

    client = _client(handler)
    assert client.find_published_posts_by_slug("ai-business-efficiency")[0]["id"] == 78
    assert [p["id"] for p in client.list_post_states()] == [1, 2]
    assert {r.method for r in seen} == {"GET"}
    assert seen[0].url.params["status"] == "publish"


# == local verification =========================================================
def _item(tmp_path: Path, data: bytes, **overrides) -> ApplyItem:
    (tmp_path / "f.webp").write_bytes(data)
    base = dict(
        article_id=25,
        slug="ai-business-efficiency",
        title="AI業務効率化｜要点と実務上の注意点",
        file="f.webp",
        alt_text="AI業務効率化の要点と注意点を表す業務フロー図",
        sha256=hashlib.sha256(data).hexdigest(),
        width=1200,
        height=675,
        mime_type="image/webp",
    )
    base.update(overrides)
    return ApplyItem(**base)


def test_webp_dimensions_are_read_from_the_header() -> None:
    assert webp_dimensions(_vp8x(1200, 675)) == (1200, 675)
    with pytest.raises(FeaturedImageError):
        webp_dimensions(b"\x89PNG" + b"\x00" * 40)


def test_local_verification_stops_on_checksum_or_size(tmp_path: Path) -> None:
    good = _vp8x(1200, 675)
    assert verify_local(_item(tmp_path, good), tmp_path) == good
    with pytest.raises(FeaturedImageError, match="SHA-256"):
        verify_local(_item(tmp_path, good, sha256="0" * 64), tmp_path)
    small = _vp8x(1200, 630)
    with pytest.raises(FeaturedImageError, match="expected"):
        verify_local(_item(tmp_path, small), tmp_path)


# == apply_one ==================================================================
class _FakeWP:
    def __init__(self, post: dict) -> None:
        self.post = dict(post)
        self.calls: list[str] = []
        self.media: dict = {}

    def find_published_posts_by_slug(self, slug):
        self.calls.append("find")
        return [dict(self.post)] if self.post["slug"] == slug else []

    def upload_featured_image_exact(self, image, *, filename, mime_type):
        self.calls.append("upload")
        self.media = {
            "id": 501,
            "mime_type": mime_type,
            "media_details": {"width": 1200, "height": 675},
            "alt_text": "",
        }
        return WordPressUploadedMedia(
            id=501, source_url=f"{_BASE}/u/{filename}", mime_type=mime_type, width=1200, height=675
        )

    def get_media(self, media_id):
        return dict(self.media)

    def update_media_text_exact(self, media_id, payload_json):
        self.calls.append("media_text")
        self.media.update(json.loads(payload_json))
        return dict(self.media)

    def set_featured_media_exact(self, post_id, payload_json):
        self.calls.append("featured")
        self.post["featured_media"] = json.loads(payload_json)["featured_media"]
        return dict(self.post)

    def get_post(self, post_id):
        return dict(self.post)


def _post(**overrides):
    post = {
        "id": 78,
        "slug": "ai-business-efficiency",
        "status": "publish",
        "title": {"raw": "AI業務効率化｜要点と実務上の注意点"},
        "content": {"raw": "<p>本文</p>"},
        "excerpt": {"raw": ""},
        "categories": [3],
        "tags": [],
        "date_gmt": "2026-09-22T10:00:00",
        "featured_media": 0,
        "link": f"{_BASE}/ai-business-efficiency/",
    }
    post.update(overrides)
    return post


def test_apply_one_uploads_once_sets_featured_media_and_verifies(tmp_path: Path) -> None:
    item = _item(tmp_path, _vp8x(1200, 675))
    wp = _FakeWP(_post())
    record = apply_one(wp, item, tmp_path, tmp_path / "applied")
    assert wp.calls == ["find", "upload", "media_text", "featured"]
    assert record["result"] == "applied"
    assert (record["wordpress_post_id"], record["media_id"], record["final_featured_media"]) == (
        78,
        501,
        501,
    )
    assert wp.media["alt_text"] == item.alt_text
    assert record["steps"] == [
        "uploaded",
        "media_verified",
        "media_text_set",
        "featured_media_set",
        "post_verified",
    ]
    saved = json.loads((tmp_path / "applied" / "ai-business-efficiency.json").read_text("utf-8"))
    assert "password" not in json.dumps(saved).lower()

    # 同じ slug でもう一度: upload しない。
    with pytest.raises(FeaturedImageError, match="already has an apply record"):
        apply_one(wp, item, tmp_path, tmp_path / "applied")
    assert wp.calls.count("upload") == 1


@pytest.mark.parametrize(
    ("post", "message"),
    [
        (_post(featured_media=12), "already has featured_media=12"),
        (_post(title={"raw": "別のタイトル"}), "title mismatch"),
        (_post(slug="other"), "expected exactly 1 published post"),
    ],
)
def test_apply_one_stops_before_any_write(tmp_path: Path, post, message) -> None:
    item = _item(tmp_path, _vp8x(1200, 675))
    wp = _FakeWP(post)
    with pytest.raises(FeaturedImageError, match=message):
        apply_one(wp, item, tmp_path, tmp_path / "applied")
    assert "upload" not in wp.calls
    assert not (tmp_path / "applied" / "ai-business-efficiency.json").exists()


def test_an_unexpected_post_change_is_detected_and_recorded(tmp_path: Path) -> None:
    item = _item(tmp_path, _vp8x(1200, 675))

    class _Drifting(_FakeWP):
        def get_post(self, post_id):
            return {**self.post, "status": "draft"}

    with pytest.raises(FeaturedImageError, match="unexpected post changes"):
        apply_one(_Drifting(_post()), item, tmp_path, tmp_path / "applied")
    record = json.loads((tmp_path / "applied" / "ai-business-efficiency.json").read_text("utf-8"))
    assert record["result"] == "stopped"
    assert record["media_id"] == 501  # 何が起きたかを残す


def test_snapshots_report_only_changed_posts() -> None:
    before = [
        {**post_fingerprint(_post(id=1)), "featured_media": 0, "modified_gmt": "a"},
        {**post_fingerprint(_post(id=2)), "featured_media": 0, "modified_gmt": "a"},
    ]
    after = [before[0], {**before[1], "featured_media": 501, "modified_gmt": "b"}]
    assert compare_snapshots(before, after)["changed"] == {2: ["featured_media", "modified_gmt"]}


# == reuse an existing, byte-identical media (no upload) ========================
class _ReuseWP(_FakeWP):
    def __init__(self, post, *, file_bytes, attached=None, featured_elsewhere=False):
        super().__init__(post)
        self.file_bytes = file_bytes
        self.media = {
            "id": 98,
            "mime_type": "image/webp",
            "media_details": {"width": 1200, "height": 675},
            "post": attached,
            "alt_text": "",
            "source_url": f"{_BASE}/wp-content/uploads/2026/09/f.webp",
        }
        self.featured_elsewhere = featured_elsewhere

    def fetch_media_file(self, source_url):
        self.calls.append("fetch")
        return self.file_bytes

    def list_post_states(self):
        return [{"id": 99, "featured_media": 98 if self.featured_elsewhere else 0}]


def test_reusing_an_identical_media_skips_the_upload(tmp_path: Path) -> None:
    data = _vp8x(1200, 675)
    item = _item(tmp_path, data)
    wp = _ReuseWP(_post(), file_bytes=data)
    record = apply_one(wp, item, tmp_path, tmp_path / "applied", existing_media_id=98)
    assert "upload" not in wp.calls
    assert wp.calls == ["find", "fetch", "media_text", "featured"]
    assert (record["media_id"], record["media_reused"], record["final_featured_media"]) == (
        98,
        True,
        98,
    )
    assert record["steps"][0] == "existing_media_verified"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"file_bytes": _vp8x(1200, 675) + b"x"}, "does not match the approved SHA-256"),
        ({"file_bytes": None, "attached": 55}, "attached to post 55"),
        ({"file_bytes": None, "featured_elsewhere": True}, "already the featured image"),
    ],
)
def test_a_media_that_is_not_provably_the_approved_image_is_never_used(
    tmp_path: Path, kwargs, message
) -> None:
    data = _vp8x(1200, 675)
    item = _item(tmp_path, data)
    if kwargs.get("file_bytes") is None:
        kwargs["file_bytes"] = data
    wp = _ReuseWP(_post(), **kwargs)
    with pytest.raises(FeaturedImageError, match=message):
        apply_one(wp, item, tmp_path, tmp_path / "applied", existing_media_id=98)
    assert not {"upload", "media_text", "featured"} & set(wp.calls)


def test_fetching_a_media_file_is_limited_to_same_origin_uploads() -> None:
    def handler(request):  # pragma: no cover - must not be called
        raise AssertionError("no request may be sent")

    with pytest.raises(ValueError):
        _client(handler).fetch_media_file("https://evil.example/wp-content/uploads/x.webp")
    with pytest.raises(ValueError):
        _client(handler).fetch_media_file(f"{_BASE}/wp-admin/x.webp")
