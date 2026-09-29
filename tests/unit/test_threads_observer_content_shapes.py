"""T6.5B.3a: Stage 1 の dry-run (2026-09-29) で見つけた投稿の形 (本文・メディア) の直し。

- 本文の範囲 (時刻のリンク〜指標の列) で「ピン留め済み」「他1件を見る」を除く (文字では除かない)
- 本物の ``<button>`` の中 (閲覧数のカード) を除く
- アイコンつきの ``/@meta.ai`` の札を DOM で除く (ふつうの言及・文字は残す)
- 本文の中の画像 (GIF のスタンプ) は添付のメディアにしない (置き場所で決める)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.social.threads.features import extract
from app.social.threads.observer import selectors as sel
from app.social.threads.observer.normalize import (
    FLAG_META_AI_LABEL_REMOVED,
    TEXT_CLEAN,
    normalize_body,
)
from app.social.threads.observer.parser import parse_page
from tests.support.threads_observer_pages import card, page

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "threads_observer" / (
    "card_shapes_2026-09-29.html"
)
UI_LABELS = ("ピン留め済み", "他1件を見る", "閲覧数", "99万", "30日", "08/30 - 2026/09/28",
             "meta.ai")


def _one(html: str):
    [post] = parse_page(page(html), limit=5).posts
    return post


# -- 本物の形 (合成の fixture) -----------------------------------------------------------


def test_the_five_live_shapes_parse_to_authored_text_only() -> None:
    html = FIXTURE.read_text(encoding="utf-8")
    for label in UI_LABELS:
        assert label in html, label  # 画面の部品は fixture に本当にある
    result = parse_page(html, limit=10)
    posts = {p.external_post_key: p for p in result.posts}
    assert posts["threads:SHAPEPIN1"].body_text == "固定した投稿の一行目です"
    assert posts["threads:SHAPETHR2"].body_text == "続きの投稿の本文です"
    assert posts["threads:SHAPEMAI3"].body_text == "札のあとの本文です"
    assert posts["threads:SHAPECRD4"].body_text == "一行目の本文\n二行目の本文\n三行目の本文"
    assert posts["threads:SHAPEGIF5"].body_text == "スタンプつきの本文\n二行目の本文"
    for post in result.posts:
        for label in UI_LABELS:
            assert label not in post.body_text, (post.external_post_key, label)
        assert post.features["numeric_facts_count"] == 0
        assert normalize_body(post.body_text).text_quality == TEXT_CLEAN
    media = {k: p.media_type for k, p in posts.items()}
    assert media == {"threads:SHAPEPIN1": "image", "threads:SHAPETHR2": "none",
                     "threads:SHAPEMAI3": "none", "threads:SHAPECRD4": "none",
                     "threads:SHAPEGIF5": "none"}  # fmt: skip
    assert posts["threads:SHAPEGIF5"].media_diagnostics["inline_media_ignored_count"] == 2
    assert posts["threads:SHAPECRD4"].media_diagnostics["embedded_card_images_ignored"] == 1
    assert posts["threads:SHAPEPIN1"].media_diagnostics["media_detection_source"] == "picture"
    # 指標は変わらない (本文の直しで数を読み違えない)。
    assert (posts["threads:SHAPEPIN1"].likes, posts["threads:SHAPEPIN1"].replies) == (1808, 16)


# -- ピン留め済み ---------------------------------------------------------------------------


def test_the_pinned_ui_label_is_excluded() -> None:
    post = _one(card("a", "P1", "固定した投稿", pinned=True))
    assert post.body_text == "固定した投稿"
    assert extract("ピン留め済み\n固定した投稿").body_length > post.features["body_length"]


def test_authored_pinned_wording_is_kept() -> None:
    post = _one(card("a", "P2", "ピン留め済みって表示された", pinned=True))
    assert post.body_text == "ピン留め済みって表示された"
    assert _one(card("a", "P3", "ピン留め済み")).body_text == "ピン留め済み"


# -- 他1件を見る -----------------------------------------------------------------------------


@pytest.mark.parametrize("label", ["他1件を見る", "他2件を見る", "他3件を見る"])
def test_the_continuation_control_is_excluded(label: str) -> None:
    post = _one(card("b", "C1", "続きの投稿", continuation=label))
    assert post.body_text == "続きの投稿"


def test_authored_continuation_wording_is_kept() -> None:
    post = _one(card("b", "C2", "返信は他1件を見るとわかる", continuation="他1件を見る"))
    assert post.body_text == "返信は他1件を見るとわかる"


# -- <button> -------------------------------------------------------------------------------


def test_a_real_button_subtree_is_excluded_and_its_numbers_not_counted() -> None:
    post = _one(card("c", "B1", "前の本文\n後の本文", stats_card=True))
    assert post.body_text == "前の本文\n後の本文"
    assert post.features["numeric_facts_count"] == 0
    assert post.media_type == "none"
    for label in ("閲覧数", "99万", "30日", "08/30"):
        assert label not in post.body_text


def test_a_role_button_subtree_is_still_excluded() -> None:
    inner = '<div role="button"><span dir="auto"><span>ボタンの文字</span></span></div>'
    post = _one(card("c", "B2", "本文", inner=inner))
    assert post.body_text == "本文"


def test_a_button_nested_inside_the_body_span_is_excluded() -> None:
    html = card("c", "B3", "本文").replace(
        "<span>本文</span>", "<span>前</span><button><span>カードの文字 123</span></button>"
        "<span>後</span>")  # fmt: skip
    post = _one(html)
    assert post.body_text == "前後"
    assert post.features["numeric_facts_count"] == 0


# -- meta.ai ----------------------------------------------------------------------------------


def test_the_meta_ai_ui_link_is_excluded_at_extraction() -> None:
    post = _one(card("d", "M1", "伸びなかったリールはどうしたらいい？", meta_ai=True))
    assert post.body_text == "伸びなかったリールはどうしたらいい？"
    assert normalize_body(post.body_text).text_quality == TEXT_CLEAN
    assert normalize_body(post.body_text).normalization_flags == ()


def test_authored_meta_ai_text_and_mentions_are_kept() -> None:
    assert _one(card("d", "M2", "今日はmeta.aiで調べた")).body_text == "今日はmeta.aiで調べた"
    mention = card("d", "M3", "本文").replace(
        "<span>本文</span>",
        '<span>相談先は</span><a href="/@meta.ai" role="link"><span>@meta.ai</span></a>'
        '<span>と</span><a href="/@someone" role="link"><span>@someone</span></a>')  # fmt: skip
    # アイコンの無い言及 (書いた人の @) は札ではない → 残す。
    assert _one(mention).body_text == "相談先は@meta.aiと@someone"


def test_the_historical_normalizer_still_cleans_old_rows() -> None:
    old = normalize_body("meta.ai トライアルリールは全体公開すべき？")
    assert old.analysis_body == "トライアルリールは全体公開すべき？"
    assert old.normalization_flags == (FLAG_META_AI_LABEL_REMOVED,)


# -- GIF のスタンプと添付のメディア ------------------------------------------------------------


@pytest.mark.parametrize("stickers", [1, 2])
def test_inline_gif_stickers_are_not_attached_media(stickers: int) -> None:
    post = _one(card("e", f"G{stickers}", "スタンプつき", inline_gifs=stickers))
    assert post.media_type == "none"
    assert post.media_diagnostics["inline_media_ignored_count"] == stickers
    assert post.body_text == "スタンプつき"


def test_a_real_attached_image_is_still_an_image() -> None:
    assert _one(card("e", "I1", "写真つき", image=True)).media_type == "image"
    both = _one(card("e", "I2", "両方", image=True, inline_gifs=2))
    assert both.media_type == "image"
    assert both.media_diagnostics["inline_media_ignored_count"] == 2


def test_the_decision_is_by_placement_not_by_domain() -> None:
    # 添付の入れ物の中なら GIF の URL でも添付。本文の中なら、ふつうの画像の URL でも添付ではない。
    attached_gif = _one(card("e", "D1", "本文", image=True,
                             image_src="https://media1.giphy.com/attached.gif"))  # fmt: skip
    assert attached_gif.media_type == "image"
    inline_cdn = card("e", "D2", "本文").replace(
        "<span>本文</span>", '<span>本文</span><img alt="" src="https://cdn.example/emoji.png">')
    assert _one(inline_cdn).media_type == "none"


def test_media_links_and_link_previews() -> None:
    assert _one(card("f", "L1", "本文", image=True, media_link=True)).media_type == "image"
    preview = _one(card("f", "L2", "本文", link_preview_image=True))
    assert preview.media_type == "none"
    assert preview.media_diagnostics["link_preview_images_ignored"] == 1


def test_versions_record_the_hardening() -> None:
    assert sel.COLLECTOR_VERSION == "t6.5b-collector-4"
    assert sel.SELECTOR_VERSION == "threads-web-verified-2026-09-29-v3"
