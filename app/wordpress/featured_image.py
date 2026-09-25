"""W1.4: 1 記事に featured image を設定する (検査と手順。WordPress とは client 経由でだけ話す)。

手順 (1 記事ずつ):

1. ローカルの画像を検査する (manifest の SHA-256・WebP の署名・1200×675・0 バイトでない)。
2. slug で **公開済みの post を 1 件だけ** 探し、タイトルが manifest と完全一致すること、
   ``featured_media`` が 0 (未設定) であることを確かめる。どれかが違えば書かない。
3. 画像を 1 回だけ upload し、media を read-back で確かめる (MIME・寸法・同一オリジン)。
4. media の ``alt_text`` / ``title`` を設定する。
5. post の ``featured_media`` だけを設定する (exact ``{"featured_media": id}``)。
6. post を read-back して、``featured_media`` が一致し、タイトル・slug・状態・本文・カテゴリ・
   タグが変わっていないことを確かめる。

同じ slug で 2 回 upload しない: 結果の記録 (``applied/<slug>.json``) があれば止まる。
upload の結果が不明 (timeout など) のときも記録して止まる (media library を人が確かめる)。
記録には credential も署名付き URL も入れない。
"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

EXPECTED_SIZE = (1200, 675)
MIME_WEBP = "image/webp"


class FeaturedImageError(RuntimeError):
    """書く前に止めるべき状態 (書き込みはしていない)。"""


@dataclass(frozen=True)
class ApplyItem:
    article_id: int
    slug: str
    title: str
    file: str
    alt_text: str
    sha256: str
    width: int
    height: int
    mime_type: str

    @classmethod
    def from_dict(cls, data: dict) -> ApplyItem:
        return cls(
            article_id=int(data["article_id"]),
            slug=str(data["slug"]),
            title=str(data["title"]),
            file=str(data["file"]),
            alt_text=str(data["alt_text"]),
            sha256=str(data["sha256"]),
            width=int(data["width"]),
            height=int(data["height"]),
            mime_type=str(data["mime_type"]),
        )


def load_manifest(path: Path) -> list[ApplyItem]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != "featured-image-wordpress-apply/1":
        raise FeaturedImageError("unexpected apply manifest schema")
    if document.get("approved") is not True:
        raise FeaturedImageError("the apply manifest is not approved")
    return [ApplyItem.from_dict(item) for item in document["items"]]


def webp_dimensions(data: bytes) -> tuple[int, int]:
    """WebP の寸法をヘッダーから読む (VP8 / VP8L / VP8X)。WebP でなければ例外。"""

    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise FeaturedImageError("the file is not a WebP image")
    chunk = data[12:16]
    body = data[20:]
    if chunk == b"VP8X":
        width = 1 + int.from_bytes(body[4:7], "little")
        height = 1 + int.from_bytes(body[7:10], "little")
        return width, height
    if chunk == b"VP8L":
        if body[0] != 0x2F:
            raise FeaturedImageError("invalid VP8L signature")
        bits = int.from_bytes(body[1:5], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8 ":
        if body[3:6] != b"\x9d\x01\x2a":
            raise FeaturedImageError("invalid VP8 start code")
        width, height = struct.unpack("<HH", body[6:10])
        return width & 0x3FFF, height & 0x3FFF
    raise FeaturedImageError(f"unknown WebP chunk {chunk!r}")


def verify_local(item: ApplyItem, directory: Path) -> bytes:
    path = directory / item.file
    if not path.is_file():
        raise FeaturedImageError(f"{item.file} does not exist")
    data = path.read_bytes()
    if not data:
        raise FeaturedImageError(f"{item.file} is empty")
    if hashlib.sha256(data).hexdigest() != item.sha256:
        raise FeaturedImageError(f"{item.file} SHA-256 does not match the manifest")
    if item.mime_type != MIME_WEBP:
        raise FeaturedImageError(f"{item.file} is not declared as {MIME_WEBP}")
    size = webp_dimensions(data)
    if size != (item.width, item.height) or size != EXPECTED_SIZE:
        raise FeaturedImageError(f"{item.file} is {size}, expected {EXPECTED_SIZE}")
    return data


def post_fingerprint(post: dict) -> dict:
    """変わってはいけない項目の写し (本文は hash だけ)。"""

    def raw(field):
        value = post.get(field)
        return value.get("raw") if isinstance(value, dict) else value

    content = raw("content") or ""
    return {
        "id": post.get("id"),
        "slug": post.get("slug"),
        "status": post.get("status"),
        "title": raw("title"),
        "date_gmt": post.get("date_gmt"),
        "categories": sorted(post.get("categories") or []),
        "tags": sorted(post.get("tags") or []),
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "excerpt_sha256": hashlib.sha256((raw("excerpt") or "").encode("utf-8")).hexdigest(),
    }


def resolve_post(client, item: ApplyItem) -> dict:
    """slug で公開済みの post をちょうど 1 件見つけ、書いてよい状態かを確かめる。"""

    posts = client.find_published_posts_by_slug(item.slug)
    if len(posts) != 1:
        raise FeaturedImageError(
            f"expected exactly 1 published post for {item.slug}, got {len(posts)}"
        )
    post = posts[0]
    title = post.get("title", {}).get("raw") if isinstance(post.get("title"), dict) else None
    if title != item.title:
        raise FeaturedImageError(f"title mismatch for {item.slug}: {title!r}")
    if post.get("slug") != item.slug or post.get("status") != "publish":
        raise FeaturedImageError(f"{item.slug} is not a published post with that exact slug")
    if post.get("featured_media") not in (0, None):
        raise FeaturedImageError(
            f"{item.slug} already has featured_media={post.get('featured_media')}; not overwriting"
        )
    return post


def apply_one(client, item: ApplyItem, directory: Path, record_dir: Path) -> dict:
    """1 記事に適用する。途中で止まったら、そこまでの事実を記録して例外を上げる。"""

    record_path = record_dir / f"{item.slug}.json"
    if record_path.exists():
        raise FeaturedImageError(f"{item.slug} already has an apply record; not uploading again")
    image = verify_local(item, directory)
    post = resolve_post(client, item)
    before = post_fingerprint(post)
    record = {
        "schema": "featured-image-apply-record/1",
        "article_id": item.article_id,
        "slug": item.slug,
        "wordpress_post_id": post["id"],
        "file": item.file,
        "sha256": item.sha256,
        "original_featured_media": post.get("featured_media"),
        "started_at": _now(),
        "steps": [],
    }
    record_dir.mkdir(parents=True, exist_ok=True)

    def save(result: str) -> None:
        record["result"] = result
        record["finished_at"] = _now()
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", "utf-8")

    try:
        uploaded = client.upload_featured_image_exact(
            image, filename=item.file, mime_type=item.mime_type
        )
        record["media_id"] = uploaded.id
        record["media_url"] = uploaded.source_url
        record["steps"].append("uploaded")

        media = client.get_media(uploaded.id)
        details = media.get("media_details") or {}
        if media.get("mime_type") != item.mime_type or (
            details.get("width"),
            details.get("height"),
        ) != (item.width, item.height):
            raise FeaturedImageError("the uploaded media does not match the file (MIME/size)")
        record["steps"].append("media_verified")

        media_title = f"{item.title} アイキャッチ"
        client.update_media_text_exact(
            uploaded.id,
            json.dumps({"alt_text": item.alt_text, "title": media_title}, ensure_ascii=False),
        )
        text = client.get_media(uploaded.id)
        if text.get("alt_text") != item.alt_text:
            raise FeaturedImageError("the media alt_text did not read back as set")
        record["media_title"] = media_title
        record["alt_text"] = item.alt_text
        record["steps"].append("media_text_set")

        client.set_featured_media_exact(post["id"], json.dumps({"featured_media": uploaded.id}))
        record["steps"].append("featured_media_set")

        after_post = client.get_post(post["id"])
        if after_post.get("featured_media") != uploaded.id:
            raise FeaturedImageError("featured_media did not read back as the uploaded media")
        after = post_fingerprint(after_post)
        if after != before:
            changed = sorted(k for k in before if before[k] != after[k])
            raise FeaturedImageError(f"unexpected post changes: {changed}")
        record["final_featured_media"] = after_post.get("featured_media")
        record["post_link"] = after_post.get("link")
        record["steps"].append("post_verified")
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        save("stopped")
        raise
    save("applied")
    return record


def snapshot_posts(client) -> list[dict]:
    """全 post の「変わってはいけない項目」と featured_media / modified_gmt の写し。"""

    rows = []
    for post in client.list_post_states():
        rows.append(
            {
                **post_fingerprint(post),
                "featured_media": post.get("featured_media"),
                "modified_gmt": post.get("modified_gmt"),
            }
        )
    return sorted(rows, key=lambda r: r["id"])


def compare_snapshots(before: list[dict], after: list[dict]) -> dict:
    old = {r["id"]: r for r in before}
    new = {r["id"]: r for r in after}
    changed = {}
    for post_id in sorted(old.keys() | new.keys()):
        a, b = old.get(post_id), new.get(post_id)
        if a != b:
            fields = sorted(k for k in (a or b) if (a or {}).get(k) != (b or {}).get(k))
            changed[post_id] = fields
    return {"posts_before": len(before), "posts_after": len(after), "changed": changed}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


__all__ = [
    "ApplyItem",
    "FeaturedImageError",
    "apply_one",
    "compare_snapshots",
    "load_manifest",
    "post_fingerprint",
    "resolve_post",
    "snapshot_posts",
    "verify_local",
    "webp_dimensions",
]
