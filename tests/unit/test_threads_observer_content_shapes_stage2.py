"""T6.5B.5: 段階 2 の dry-run (2026-09-29) で見つけた本文の形の直し (偽のページだけ)。

- 本文の行の **中** の ``role=button`` (リンクの後ろの「翻訳」) を本文に入れない
- 添付のメディアの入れ物の奥の文字 (動画の上の Instagram の名前の札) を本文に入れない
- 書いた人の文字 (見えない区切りの文字を含む) は変えない
"""

from __future__ import annotations

from pathlib import Path

from app.social.threads.observer import selectors as sel
from app.social.threads.observer.normalize import TEXT_CLEAN, normalize_body
from app.social.threads.observer.parser import parse_page
from tests.support.threads_observer_pages import card, page

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "threads_observer" / (
    "card_shapes_2026-09-29_stage2.html"
)


def _one(html: str):
    [post] = parse_page(page(html), limit=5).posts
    return post


def test_the_two_live_shapes_parse_to_authored_text_only() -> None:
    html = FIXTURE.read_text(encoding="utf-8")
    assert "翻訳" in html and html.count("shape_badge") > 1  # 画面の部品は fixture に本当にある
    posts = {p.external_post_key: p for p in parse_page(html, limit=10).posts}
    badge, translate = posts["threads:SHAPEVID6"], posts["threads:SHAPETRN7"]
    assert badge.body_text == "合成の文字3"
    assert (badge.media_type, badge.likes, badge.replies) == ("video", 4, None)
    assert translate.body_text == "合成の文字4\n合成の文字5\nexample.invalid/page…"
    assert (translate.media_type, translate.likes, translate.replies) == ("image", 54, 2)
    for post in posts.values():
        assert "翻訳" not in post.body_text and "shape_badge" not in post.body_text
        assert normalize_body(post.body_text).text_quality == TEXT_CLEAN


def test_a_translate_control_inside_the_last_line_is_excluded() -> None:
    post = _one(card("f", "T1", "記事の紹介\nexample.invalid/x…", translate=True))
    assert post.body_text == "記事の紹介\nexample.invalid/x…"


def test_authored_translate_wording_is_kept() -> None:
    assert _one(card("f", "T2", "翻訳の仕事をしています")).body_text == "翻訳の仕事をしています"


def test_a_name_badge_over_a_video_is_excluded() -> None:
    post = _one(card("v", "V1", "簡単な料理です", video_overlay="v_cooking"))
    assert post.body_text == "簡単な料理です"
    assert post.media_type == "video"


def test_authored_text_naming_the_same_account_is_kept() -> None:
    post = _one(card("v", "V2", "v_cooking の新作です\n二行目", video_overlay="v_cooking"))
    assert post.body_text == "v_cooking の新作です\n二行目"


def test_text_posts_and_image_posts_are_unchanged() -> None:
    assert _one(card("t", "P1", "一行目\n二行目")).body_text == "一行目\n二行目"
    image = _one(card("t", "P2", "写真つき\n二行目", image=True))
    assert (image.body_text, image.media_type) == ("写真つき\n二行目", "image")
    video = _one(card("t", "P3", "動画つき", video=True))
    assert (video.body_text, video.media_type) == ("動画つき", "video")


def test_authored_invisible_separators_are_left_as_written() -> None:
    # 2026-09-29: 書いた人が見えない文字 (U+2061) の行で区切っていた。画面の部品ではないので残す。
    post = _one(card("n", "N1", "⁡\n本文です\n⁡\n#タグ", video=True))
    assert post.body_text == "⁡\n本文です\n⁡\n#タグ"


def test_versions_record_the_stage2_fix() -> None:
    assert sel.COLLECTOR_VERSION == "t6.5b-collector-6"
    assert sel.SELECTOR_VERSION == "threads-web-verified-2026-09-29-v4"
    assert sel.surface_verification("for_you")["version"] == sel.SELECTOR_VERSION
