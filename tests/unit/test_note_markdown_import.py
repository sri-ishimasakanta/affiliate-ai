"""有料の記事の Markdown の取り込みと表示 (2026-10-01、折り返しの行の不具合の修正)。

不具合: 取り込みが、折り返した行 (Markdown では同じ段落・同じ項目) を別々の段落にしていた。
番号つきの項目が途中で切れ、note で番号が 1 から振り直され、``**`` が文字のまま残った。

pin する契約:

- 空行が段落・項目の区切り。折り返した行は同じ段落・同じ項目につなぐ
  (日本語どうしは空白なし)。
- 番号つき・印つきの項目は行の先頭の印で始まる。見出し・表の行・コードの枠は独立。コードの枠の
  改行はそのまま。
- 表示: 続く番号は 1 つの ``<ol>``、``**…**`` は ``<strong>``。照らし合わせでは太字は書式
  として扱う。
- 本物の手引き (0.2.0) の「進め方」は 1〜4、「ファイルと生成物の承認」は 1〜6 の 1 つの番号つきの
  箇条書きになる。商品の資産の行はすべて有料の部分に入る。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.social.note import review
from tests.unit.test_note_review import _draft

REPO = Path(__file__).resolve().parents[2]
GUIDE = REPO / "products/approval-gated-automation-kit/assets/guide.md"


def _section(markdown: str) -> list[str]:
    return review.sections_from_markdown(markdown)[0]["paragraphs"]


def test_a_soft_wrapped_paragraph_is_one_paragraph() -> None:
    paras = _section("## 見出し\n\n一行目の文章が、途中で\n折り返されている。English words\n"
                     "continue here.\n")
    assert paras == ["一行目の文章が、途中で折り返されている。English words continue here."]


def test_blank_lines_separate_blocks() -> None:
    assert _section("## h\n\n一つ目。\n\n二つ目。\n") == ["一つ目。", "二つ目。"]


def test_wrapped_ordered_and_unordered_items_stay_one_item() -> None:
    paras = _section("## h\n\n1. 最初の項目は\n   ここまで続く。\n2. 二つ目。\n\n"
                     "- 印の項目も\n  続く。\n"
                     "- [ ] 確認の箱も\n      続く。\n・点の項目\n")
    assert paras == ["1. 最初の項目はここまで続く。", "2. 二つ目。", "- 印の項目も続く。",
                     "- [ ] 確認の箱も続く。", "・点の項目"]


def test_headings_tables_and_code_blocks_stay_separate() -> None:
    code = '```json\n{\n  "a": 1,\n  "b": [2, 3]\n}\n```'
    paras = _section(f"## h\n\n前の文章。\n### 小見出し\n| A | B |\n|---|---|\n{code}\n"
                     "後の文章。\n")
    assert paras == ["前の文章。", "### 小見出し", "| A | B |", "|---|---|", code, "後の文章。"]
    assert json.loads(code.split("\n", 1)[1].rsplit("\n", 1)[0]) == {"a": 1, "b": [2, 3]}


def _paid_draft(tmp_path, paid_markdown: str):
    _root, _path, draft = _draft(tmp_path)
    draft.sections = review.sections_from_markdown(paid_markdown)
    return draft


def test_ordered_lists_and_bold_render_as_html_and_round_trip(tmp_path) -> None:
    draft = _paid_draft(tmp_path, "## h\n\n1. **提出**: 出す。\n2. **承認**: 見る\n   と決める。\n"
                                  "3. 三つ目。\n\n普通の **太字** の文。\n\n- **印**: 項目\n")
    blocks = review.display_blocks(draft)
    ordered = [b for b in blocks if b["type"] == "ordered"]
    assert len(ordered) == 1 and ordered[0]["numbers"] == [1, 2, 3]
    html = review.paste_html(draft)
    assert ("<ol><li><strong>提出</strong>: 出す。</li>"
            "<li><strong>承認</strong>: 見ると決める。</li>") in html
    assert "<p>普通の <strong>太字</strong> の文。</p>" in html and "**" not in html
    assert "<li><strong>印</strong>: 項目</li>" in html
    assert review.canonical_lines(blocks) == review.body_lines(draft)  # 本文の形に戻る


def test_publication_check_treats_bold_as_formatting(tmp_path) -> None:
    draft = _paid_draft(tmp_path, "## h\n\n1. **提出**: 出す。\n\n普通の **太字** の文。\n")
    page = review.display_blocks(draft)
    plain = json.loads(json.dumps(page, ensure_ascii=False).replace("**", ""))  # 太字は書式
    assert review.compare_published(draft, plain)["match"] is True
    fragment = review.strong_to_markdown("普通の <strong>太字</strong> の文。")
    assert fragment == "普通の **太字** の文。"
    changed = json.loads(json.dumps(plain, ensure_ascii=False).replace("出す。", "出した。"))
    assert review.compare_published(draft, changed)["match"] is False  # 文字の違いは違い


def test_existing_heading_and_bullet_normalization_still_hold(tmp_path) -> None:
    draft = _paid_draft(tmp_path, "## 前提\n\n・一つ目\n・二つ目\n\n### 小見出し\n")
    blocks = review.display_blocks(draft)
    assert {"type": "list", "items": ["一つ目", "二つ目"]} in blocks
    assert {"type": "subheading", "text": "### 小見出し"} in blocks
    assert review.compare_published(draft, blocks)["match"] is True


def _guide_markdown() -> str:
    lines = [("#" + line) if re.match(r"^#{1,5} ", line) else line
             for line in GUIDE.read_text("utf-8").splitlines()]
    return "\n".join(lines) + "\n"


def test_the_real_guide_has_continuous_ordered_lists() -> None:
    sections = review.sections_from_markdown(_guide_markdown())
    paras = sections[0]["paragraphs"]
    steps = paras[paras.index("### 進め方") + 1:paras.index("### ファイルと生成物の承認を固定する")]
    assert [p.split(".", 1)[0] for p in steps] == ["1", "2", "3", "4"]
    assert steps[0].startswith("1. 書き込み先を一覧にし")
    assert "並んでいるわけではありません" in steps[0]
    assert "として扱います。D は" in steps[0] and len(steps) == 4
    flow_start = paras.index("そこで、次の流れにします。") + 1
    flow = paras[flow_start:paras.index("### よくある落とし穴")]
    assert [p.split(".", 1)[0] for p in flow] == ["1", "2", "3", "4", "5", "6"]
    assert flow[1].startswith("2. **承認**")
    assert "「どこから来たか」の記録として残すだけにします。" in flow[1]
    # 段落の途中の切れ目が無い (前は 23 か所)
    prose = [p for p in paras if not re.match(r"^(### |- |\d+\. |\|)", p)]
    assert all(re.search(r"[。：:)）」]$", p) for p in prose), prose


def test_the_real_guide_lines_are_all_contained_after_joining() -> None:
    paras = review.sections_from_markdown(_guide_markdown())[0]["paragraphs"]
    text = "\n\n".join(paras)
    source = [line.strip() for line in GUIDE.read_text("utf-8").splitlines()
              if line.strip() and not line.startswith("#")]
    assert [line for line in source if line not in text] == []
