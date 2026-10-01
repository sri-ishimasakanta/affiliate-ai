"""note に貼る表示の形 (2026-10-01): 見出しと箇条書きを、本文の文字と分けて扱う。

pin する契約:

- 本文の文字 (「・」の印つきの段落) と ``content_hash`` は今までと同じ (承認の意味は変えない)。
- 表示 (``display_blocks``) では、続く「・」の段落を 1 つの箇条書きにまとめ、印を外す。
- ``canonical_lines`` で表示の形から承認した本文の行に戻せる (公開ページの照合に使う)。
- ``paste_html`` は見出しを h2、箇条書きを ul / li にし、文字はエスケープする。
- ``packet`` は ``.txt`` に加えて ``.paste.html`` を書く。
"""

from __future__ import annotations

import json

from app.social.note import review
from tests.unit.test_note_channel import NOW
from tests.unit.test_note_review import _draft

BULLETS = """# 箇条書きのある題名

## 前提

最初の段落。

・一つ目 <b>
・二つ目

あいだの段落。

・三つ目

## 学び

・最後の項目
"""


def _bulleted(tmp_path):
    root, path, draft = _draft(tmp_path)
    review.apply_edit(draft, BULLETS, commissions_known=False, editor="claude", now=NOW)
    return root, path, draft


def test_display_blocks_group_bullets_without_touching_the_hash(tmp_path) -> None:
    _root, _path, draft = _bulleted(tmp_path)
    before = draft.content_hash
    blocks = review.display_blocks(draft)
    assert blocks == [
        {"type": "heading", "text": "前提"},
        {"type": "paragraph", "text": "最初の段落。"},
        {"type": "list", "items": ["一つ目 <b>", "二つ目"]},
        {"type": "paragraph", "text": "あいだの段落。"},
        {"type": "list", "items": ["三つ目"]},
        {"type": "heading", "text": "学び"},
        {"type": "list", "items": ["最後の項目"]},
    ]
    assert draft.sections[0]["paragraphs"][1] == "・一つ目 <b>"  # 本文の文字はそのまま
    assert draft.content_hash == before
    assert review.canonical_lines(blocks) == review.body_lines(draft)


def test_a_published_page_with_native_lists_matches_the_approved_body(tmp_path) -> None:
    _root, _path, draft = _bulleted(tmp_path)
    page = review.display_blocks(draft)  # note がネイティブの箇条書きで表示した形
    assert review.compare_published(draft, page) == {"match": True, "lines": 8}
    changed = json.loads(json.dumps(page))
    changed[2]["items"][1] = "二つめ"
    result = review.compare_published(draft, changed)
    assert result["match"] is False and result["expected"] == "・二つ目"
    shorter = page[:-1]
    assert review.compare_published(draft, shorter)["match"] is False


def test_paste_html_uses_native_headings_and_lists(tmp_path) -> None:
    _root, _path, draft = _bulleted(tmp_path)
    text = review.paste_html(draft)
    assert "<h2>前提</h2>" in text and "<h2>学び</h2>" in text
    assert "<ul><li>一つ目 &lt;b&gt;</li><li>二つ目</li></ul>" in text
    assert "<li>・" not in text and "<h1>" not in text  # 題名は note の題名の欄に入れる
    assert review.plain_text(draft).count("・") == 4  # .txt は今までどおり


def test_the_packet_writes_the_paste_html(tmp_path) -> None:
    from scripts.manage_note_piece import main

    root, path, draft = _bulleted(tmp_path)
    path.write_text(json.dumps(draft.as_dict(), ensure_ascii=False), encoding="utf-8")
    assert main(["packet", draft.id], root=root, now=NOW) == 0
    html = path.with_suffix(".paste.html").read_text("utf-8")
    assert "<ul><li>最後の項目</li></ul>" in html
    assert path.with_suffix(".txt").read_text("utf-8") == review.plain_text(draft)
