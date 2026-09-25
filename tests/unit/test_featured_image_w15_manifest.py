"""W1.5A: 残り 21 記事の featured image の設計 (manifest) の検査。

WordPress にも DB にも触らない。manifest の JSON だけを読む。

pin する契約:

- 対象は article 1〜19・21・22 の 21 記事。W1.4 の試作 (20 / 23 / 24 / 25) を含まない。
- どの記事も ``status: planned`` (この段階では何も適用していない)。
- キャンバスは 1200×675 で、試作と同じ余白・ラベル予約域を使う。
- 見出しは 1〜2 行で、列の幅に収まる。サイズは 96〜112px (90px 未満は禁止)。
- モチーフは余白の内側・ラベル予約域の外。
- 画像生成の文は日本語を含まず、ロゴ・製品画面・人物を頼まない。共通の禁止語を必ず付ける。
- 見出し・補助語に金額や日付を入れない。見出しがタイトルそのものではない。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "docs" / "operations" / "featured-image-w1.5-manifest.json"
PILOT = ROOT / "docs" / "operations" / "featured-image-pilot-manifest.json"

EXPECTED_IDS = set(range(1, 20)) | {21, 22}
PILOT_IDS = {20, 23, 24, 25}
REQUIRED = (
    "article_id",
    "slug",
    "title",
    "family",
    "headline",
    "sublabel",
    "motif",
    "composition",
    "accent",
    "alt_text",
    "media_title",
    "generation_prompt",
    "typesetting",
    "negative_constraints",
    "status",
)
JAPANESE = re.compile(r"[぀-ヿ㐀-鿿＀-￯]")
PRICE_OR_DATE = re.compile(r"[$¥￥円]|\d{4}|\d+\s*[月日]")
REQUESTED_BUT_FORBIDDEN = re.compile(
    r"\b(logo|screenshot|photo\w*|robot\w*|humanoid|people|person|faces?|hands?)\b", re.I
)


@pytest.fixture(scope="module")
def doc() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def articles(doc) -> list[dict]:
    return doc["articles"]


def _estimated_width(text: str, size: int) -> float:
    # 全角は 1em、半角は多めに 0.7em で見積もる (実測値とは別の、書体に依らない上限の目安)。
    return sum(size * (0.7 if ord(ch) < 128 else 1.0) for ch in text)


def test_the_manifest_covers_exactly_the_remaining_21_articles(doc, articles) -> None:
    assert doc["schema"] == "featured-image-w1.5/1"
    ids = [a["article_id"] for a in articles]
    assert len(ids) == 21
    assert len(set(ids)) == 21
    assert set(ids) == EXPECTED_IDS
    assert not set(ids) & PILOT_IDS
    assert set(doc["excluded_article_ids"]["ids"]) == PILOT_IDS
    for key in ("slug", "title", "planned_file", "alt_text"):
        values = [a[key] for a in articles]
        assert len(set(values)) == len(values), f"duplicate {key}"
    headlines = [a["headline"]["text"] for a in articles]
    assert len(set(headlines)) == len(headlines), "duplicate headline"


def test_every_record_is_complete_and_only_planned(articles) -> None:
    for a in articles:
        for key in REQUIRED:
            assert a.get(key) not in (None, "", [], {}), f"article {a['article_id']}: {key}"
        assert a["status"] == "planned"
        assert "wordpress" not in a  # 適用の記録はまだ無い
        assert a["media_title"] == f"{a['title']} アイキャッチ"
        assert re.fullmatch(rf"featured-{a['article_id']}-[a-z0-9-]+\.webp", a["planned_file"])


def test_the_canvas_matches_the_w1_pilot_system(doc) -> None:
    canvas, pilot = doc["canvas"], json.loads(PILOT.read_text(encoding="utf-8"))["canvas"]
    assert (canvas["width"], canvas["height"]) == (1200, 675)
    assert canvas["outer_margin_px"] == pilot["outer_margin_px"]
    label = canvas["reserved_label_area_px"]
    assert (label["x"], label["y"], label["width"], label["height"]) == (0, 0, 560, 170)
    assert canvas["base_colors"]["background"] == "#F5F7FA"
    assert canvas["base_colors"]["text"] == "#12263F"
    assert "WebP" in canvas["format"]


def test_headlines_fit_the_column_at_an_allowed_size(articles) -> None:
    for a in articles:
        t, lines = a["typesetting"], a["headline"]["lines"]
        size, column = t["headline_size_px"], t["column_width_px"]
        assert 1 <= len(lines) <= 2
        assert "".join(lines) == a["headline"]["text"]
        assert 96 <= size <= 112 and size >= 90
        assert column <= a["composition"]["headline_column_px"][2]
        assert len(t["headline_measured_width_px"]) == len(lines)
        for line, measured in zip(lines, t["headline_measured_width_px"], strict=True):
            assert 0 < measured <= column, f"article {a['article_id']}: {line}"
            assert _estimated_width(line, size) <= column, f"article {a['article_id']}: {line}"
        assert 0 < t["sublabel_measured_width_px"] <= column
        assert 40 <= t["sublabel_size_px"] <= 48


def test_headlines_are_short_hooks_not_titles_prices_or_dates(articles) -> None:
    for a in articles:
        text = a["headline"]["text"]
        assert text != a["title"]
        assert text != a["title"].split("｜")[0]
        assert "｜" not in text
        for value in (text, a["sublabel"], *a["headline"]["backup_candidates"]):
            assert not PRICE_OR_DATE.search(value), f"article {a['article_id']}: {value}"


def test_the_motif_stays_inside_the_margins_and_out_of_the_label_zone(articles) -> None:
    for a in articles:
        c = a["composition"]
        x, y, w, h = c["motif_box_px"]
        assert x >= 660 and x + w <= 1200 - 72, a["article_id"]
        assert y >= 60 and y + h <= 675 - 60, a["article_id"]
        cx, cy = c["motif_centre_px"]
        assert x <= cx <= x + w and y <= cy <= y + h
        assert not (cx < 560 and cy < 170)
        assert c["label_keep_out_px"] == [0, 0, 560, 170]


def test_accents_follow_the_family(doc, articles) -> None:
    families = doc["families"]
    for a in articles:
        family = families[a["family"]]
        color = a["accent"]["color"]
        assert re.fullmatch(r"#[0-9A-F]{6}", color)
        assert color == family["accent"]
        assert a["article_id"] in family["article_ids"]
        assert a["typesetting"]["headline_mark"]["color"] == color
        assert a["typesetting"]["accent_bar"] == {
            "x": 0,
            "y": 663,
            "width": 1200,
            "height": 12,
            "color": color,
        }


def test_generation_prompts_never_ask_for_text_logos_or_people(doc, articles) -> None:
    shared = doc["shared_prompt"]
    assert {"text", "logo", "product screenshot", "people", "faces", "robot"} <= set(
        shared["negative"]
    )
    for a in articles:
        prompt = a["generation_prompt"]
        assert not JAPANESE.search(prompt), a["article_id"]
        assert prompt.endswith(shared["positive_suffix"].replace("{ACCENT}", a["accent"]["color"]))
        assert not REQUESTED_BUT_FORBIDDEN.search(prompt), a["article_id"]
        assert a["negative_constraints"][: len(shared["negative"])] == shared["negative"]


def test_batches_cover_each_article_once_in_groups_of_three_to_five(doc, articles) -> None:
    seen = [i for b in doc["batches"] for i in b["article_ids"]]
    assert sorted(seen) == sorted(a["article_id"] for a in articles)
    assert all(3 <= len(b["article_ids"]) <= 5 for b in doc["batches"])
    for a in articles:
        batch = next(b for b in doc["batches"] if a["article_id"] in b["article_ids"])
        assert a["batch"] == batch["batch"]


def test_distinguish_from_points_at_real_articles(articles) -> None:
    known = {a["article_id"] for a in articles} | PILOT_IDS
    for a in articles:
        for other in a.get("distinguish_from", []):
            assert other["article_id"] in known
            assert other["article_id"] != a["article_id"]
            assert other["rule"]
