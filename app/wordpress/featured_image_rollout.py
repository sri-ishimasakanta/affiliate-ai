"""W1.5: 承認済みの 21 枚を WordPress に適用する前の計画 (読むだけの確認の判定)。

WordPress とは話さない (呼び出し側の CLI が読んだ値を渡す)。ここでは次を決める:

- 承認の記録 (review plan の SHA-256 の表) の読み取り
- media の扱い: 新しく upload / byte 単位で同じ既存の media を再利用 / 人が確かめる
- 今の ``featured_media`` の分類: 未設定 / 既に承認済みの画像 / 想定外の画像
- 適用の manifest の 1 行と、21/21 の監査

ファイル名だけで再利用しない。SHA-256 が一致し、どこにも添付されず、どの post の
featured image でもない media だけを再利用の候補にする。media 99 は触らない。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

EXPECTED_ARTICLE_IDS = frozenset(set(range(1, 20)) | {21, 22})
PILOT_ARTICLE_IDS = frozenset({20, 23, 24, 25})
# W1.4 で適用済み (post → media)。W1.5 では読んで確かめるだけ。
PILOT_MAPPING = {78: 100, 74: 98, 76: 97, 72: 96}
# 使わない media (W1.4 の重複。削除・添付・変更・再利用をしない)。
UNTOUCHABLE_MEDIA_IDS = frozenset({99})

ACTION_UPLOAD = "upload"
ACTION_REUSE = "reuse"
ACTION_REVIEW = "human_review"

FEATURED_NONE = "none"
FEATURED_EXPECTED = "already_expected"
FEATURED_UNEXPECTED = "unexpected"

_APPROVED_ROW = re.compile(
    r"^\|\s*(\d+)\s*\|\s*(featured-[a-z0-9-]+\.webp)\s*\|\s*`([0-9a-f]{64})`\s*\|\s*$", re.M
)


class RolloutPlanError(ValueError):
    """計画を作れない (人が確かめる)。"""


def parse_approved_hashes(markdown: str) -> dict[int, dict]:
    """review plan の「承認した画像」の表 → article_id → {file, sha256}。重複は止める。"""

    approved: dict[int, dict] = {}
    for match in _APPROVED_ROW.finditer(markdown):
        article_id = int(match.group(1))
        if article_id in approved:
            raise RolloutPlanError(f"article {article_id} appears twice in the approval table")
        approved[article_id] = {"file": match.group(2), "sha256": match.group(3)}
    return approved


def media_file_name(media: Mapping) -> str:
    """media の元のファイル名 (``media_details.file`` か ``source_url`` の最後)。"""

    details = media.get("media_details") if isinstance(media.get("media_details"), dict) else {}
    raw = details.get("file") or urlsplit(str(media.get("source_url") or "")).path
    return unquote(PurePosixPath(str(raw)).name)


def filename_matches(planned_file: str, media: Mapping) -> bool:
    """同じ名前か、WordPress が衝突のときに付ける ``-<数字>`` だけが違う名前。"""

    stem = PurePosixPath(planned_file).stem
    name = PurePosixPath(media_file_name(media))
    return name.suffix == PurePosixPath(planned_file).suffix and bool(
        re.fullmatch(rf"{re.escape(stem)}(-\d+)?(-scaled)?", name.stem)
    )


def plan_media(
    *,
    planned_file: str,
    sha256: str,
    width: int,
    height: int,
    media_items: Iterable[Mapping],
    media_sha256: Mapping[int, str | None],
    featured_by_media: Mapping[int, list[int]],
) -> dict:
    """1 枚の media の扱いを決める。

    - 同じ SHA-256 の media がちょうど 1 つで、webp・寸法が合い、どこにも添付されず、どの
      post の featured image でもなく、触らない media でもない → ``reuse``
    - 同じ SHA-256 の media はあるが条件を満たさない、または複数 → ``human_review``
    - 名前が同じ (か ``-1`` など) media があるが byte が一致しない・確かめられない
      → ``human_review``
    - どれも無い → ``upload``
    """

    items = list(media_items)
    by_bytes = [m for m in items if media_sha256.get(m.get("id")) == sha256]
    by_name = [m for m in items if filename_matches(planned_file, m)]
    unverified = [m["id"] for m in by_name if media_sha256.get(m.get("id")) is None]
    result = {
        "byte_identical_media_ids": sorted(m["id"] for m in by_bytes),
        "filename_collision_media_ids": sorted(m["id"] for m in by_name),
        "unverified_media_ids": sorted(unverified),
        "media_id": None,
    }
    if by_bytes:
        problems = []
        if len(by_bytes) > 1:
            problems.append("more than one byte-identical media")
        media = by_bytes[0]
        media_id = media["id"]
        details = media.get("media_details") or {}
        if media_id in UNTOUCHABLE_MEDIA_IDS:
            problems.append(f"media {media_id} must not be touched")
        if media.get("mime_type") != "image/webp":
            problems.append(f"media {media_id} is not image/webp")
        if (details.get("width"), details.get("height")) != (width, height):
            problems.append(f"media {media_id} is not {width}x{height}")
        if media.get("post") not in (None, 0):
            problems.append(f"media {media_id} is attached to post {media.get('post')}")
        if featured_by_media.get(media_id):
            problems.append(
                f"media {media_id} is already featured by {featured_by_media[media_id]}"
            )
        if problems:
            return {**result, "action": ACTION_REVIEW, "reason": "; ".join(problems)}
        return {
            **result,
            "action": ACTION_REUSE,
            "media_id": media_id,
            "reason": f"media {media_id} is byte-identical (SHA-256), unattached and unused",
        }
    if by_name:
        return {
            **result,
            "action": ACTION_REVIEW,
            "reason": "a media with the same file name exists but is not byte-identical"
            + (" (bytes could not be read)" if unverified else ""),
        }
    return {
        **result,
        "action": ACTION_UPLOAD,
        "reason": "no existing media with these bytes or name",
    }


def classify_featured(
    featured_media: int | None, *, sha256: str, media_sha256: Mapping[int, str | None]
) -> str:
    """今の ``featured_media`` が、未設定か・承認済みの画像そのものか・想定外か。"""

    if featured_media in (None, 0):
        return FEATURED_NONE
    if media_sha256.get(featured_media) == sha256:
        return FEATURED_EXPECTED
    return FEATURED_UNEXPECTED


def apply_entry(
    *,
    article: Mapping,
    batch: int,
    local: Mapping,
    post: Mapping,
    media_plan: Mapping,
    featured_state: str,
) -> dict:
    """適用の manifest の 1 行 (``featured-image-wordpress-apply/1`` の項目 + W1.5 の記録)。"""

    featured = post.get("featured_media") or 0
    if featured_state != FEATURED_NONE:
        action = ACTION_REVIEW
        reason = (
            f"the post already has featured_media={featured} ({featured_state}); not overwriting"
        )
    else:
        action, reason = media_plan["action"], media_plan["reason"]
    return {
        # apply の道具が読む項目
        "article_id": article["article_id"],
        "slug": post["slug"],
        "title": article["title"],
        "file": local["file"],
        "source": local["source"],
        "alt_text": article["alt_text"],
        "sha256": local["sha256"],
        "width": local["width"],
        "height": local["height"],
        "mime_type": "image/webp",
        "media_action": action,
        "planned_media_id": media_plan["media_id"] if action == ACTION_REUSE else None,
        # 記録 (道具は読まない)
        "batch": batch,
        "application_slug": article["slug"],
        "wordpress_slug": post["slug"],
        "wordpress_slug_decoded": unquote(post["slug"]),
        "wordpress_post_id": post["id"],
        "media_title": article["media_title"],
        "media_reason": reason,
        "media_candidates": {
            "byte_identical": media_plan["byte_identical_media_ids"],
            "filename_collision": media_plan["filename_collision_media_ids"],
        },
        "current_featured_media": featured,
        "featured_state": featured_state,
        "original_featured_media": featured,
        "expected_featured_media_after_apply": (
            media_plan["media_id"]
            if action == ACTION_REUSE
            else "new upload (id assigned by WordPress)"
        )
        if action != ACTION_REVIEW
        else None,
        "original_modified": post.get("modified"),
        "original_modified_gmt": post.get("modified_gmt"),
        "date": post.get("date"),
        "categories": list(post.get("categories") or []),
        "tags": list(post.get("tags") or []),
        "link": post.get("link"),
        "human_approved_image": bool(local.get("approved")),
        "apply_status": "planned",
    }


def audit(entries: list[Mapping], *, pilots: Mapping[int, int | None], media_99: Mapping) -> dict:
    """適用の前に 21/21 でそろっていることを確かめる。1 つでも欠ければ ``ready`` は False。"""

    ids = [e["article_id"] for e in entries]
    checks = {
        "exactly_21_articles": len(ids) == 21 and set(ids) == EXPECTED_ARTICLE_IDS,
        "no_pilot_articles": not set(ids) & PILOT_ARTICLE_IDS,
        "unique_posts": len({e["wordpress_post_id"] for e in entries}) == len(entries),
        "all_images_approved": all(e["human_approved_image"] for e in entries),
        "all_sha256_fixed": all(re.fullmatch(r"[0-9a-f]{64}", e["sha256"]) for e in entries),
        "featured_media_known": all(e["featured_state"] in _FEATURED_STATES for e in entries),
        "no_existing_featured_image": all(e["featured_state"] == FEATURED_NONE for e in entries),
        "media_plan_known": all(e["media_action"] in _ACTIONS for e in entries),
        "no_human_review_needed": all(e["media_action"] != ACTION_REVIEW for e in entries),
        "rollback_recorded": all("original_featured_media" in e for e in entries),
        "pilot_mappings_unchanged": dict(pilots) == PILOT_MAPPING,
        "media_99_untouched": bool(media_99.get("untouched")),
        "media_99_not_planned": all(
            e.get("planned_media_id") not in UNTOUCHABLE_MEDIA_IDS for e in entries
        ),
    }
    return {"checks": checks, "ready": all(checks.values())}


_FEATURED_STATES = (FEATURED_NONE, FEATURED_EXPECTED, FEATURED_UNEXPECTED)
_ACTIONS = (ACTION_UPLOAD, ACTION_REUSE, ACTION_REVIEW)
