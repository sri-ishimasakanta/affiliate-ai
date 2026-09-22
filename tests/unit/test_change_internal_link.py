"""内部リンク提案の生成規則 (C9.1、pure)。

pin する契約:

- 既存の散文は 1 バイトも変わらない (挿入のみ)。
- 追加されるリンクは **1 本だけ**。
- 見出し・リスト・引用・コードブロックの内部には入らない。
- 出典やアフィリエイトのリンクは触られない。
- 同じリンクが既にあれば提案しない。
- ``proposal_hash`` は内容の識別子であり、1 文字違えば別の値になる。
"""

from __future__ import annotations

import hashlib

from app.change.internal_link import (
    added_link_count,
    build_internal_link_proposal,
    compute_proposal_hash,
    has_link_to,
    non_link_text_unchanged,
)

_URL = "https://example.com/target/"
_TITLE = "リンク先の記事"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build(body: str, *, url: str = _URL):
    return build_internal_link_proposal(
        source_article_id=18,
        source_body=body,
        target_article_id=17,
        target_title=_TITLE,
        target_url=url,
        body_hasher=_hash,
    )


_BODY = """# 見出し

導入の段落です。ここに本文があります。

## 事前準備

- 箇条書き
- もう一つ

参考: [公式ページ](https://official.example.jp/pricing) を確認してください。

[広告リンク](https://go.example.com/track/abc)
"""


def test_inserts_exactly_one_link_without_touching_prose() -> None:
    proposal, rejection = _build(_BODY)

    assert rejection is None
    assert added_link_count(_BODY, proposal.proposed_body) == 1
    assert non_link_text_unchanged(_BODY, proposal.proposed_body)


def test_existing_reference_and_affiliate_links_are_untouched() -> None:
    proposal, _ = _build(_BODY)

    assert "[公式ページ](https://official.example.jp/pricing)" in proposal.proposed_body
    assert "[広告リンク](https://go.example.com/track/abc)" in proposal.proposed_body
    # 既存の行そのものが残っている (置換されていない)。
    for line in _BODY.split("\n"):
        assert line in proposal.proposed_body.split("\n")


def test_insertion_point_is_not_inside_a_heading_or_list() -> None:
    proposal, _ = _build(_BODY)
    lines = proposal.proposed_body.split("\n")
    index = lines.index(proposal.inserted_paragraph)

    assert not lines[index - 1].startswith("#")
    assert not lines[index - 1].lstrip().startswith("-")
    # 最初の見出し (## 事前準備) より前に入る。
    assert index < lines.index("## 事前準備")


def test_refuses_when_the_link_already_exists() -> None:
    body = _BODY.replace("導入の段落です。", f"導入の段落です。[既出]({_URL})")
    proposal, rejection = _build(body)

    assert proposal is None
    assert rejection.reason_code == "LINK_ALREADY_PRESENT"


def test_refuses_when_there_is_no_plain_introduction() -> None:
    proposal, rejection = _build("# 見出しだけ\n\n## 次の見出し\n")

    assert proposal is None
    assert rejection.reason_code == "NO_SAFE_INSERTION_POINT"


def test_refuses_when_the_target_has_no_url() -> None:
    proposal, rejection = _build(_BODY, url="")

    assert proposal is None
    assert rejection.reason_code == "TARGET_URL_UNAVAILABLE"


def test_refuses_on_empty_body() -> None:
    proposal, rejection = _build("   \n\n")

    assert proposal is None
    assert rejection.reason_code == "EMPTY_SOURCE_BODY"


def test_refuses_when_the_introduction_is_a_list_item() -> None:
    proposal, rejection = _build("- 箇条書きだけの導入\n\n## 見出し\n")

    assert proposal is None
    assert rejection.reason_code == "NO_SAFE_INSERTION_POINT"


def test_proposal_is_deterministic() -> None:
    first, _ = _build(_BODY)
    second, _ = _build(_BODY)

    assert first.proposed_body == second.proposed_body
    assert first.proposed_body_hash == second.proposed_body_hash


def test_proposal_hash_changes_with_any_field() -> None:
    base = dict(
        change_type="add_internal_link",
        source_article_id=18,
        target_article_id=17,
        source_body_hash="a" * 64,
        proposed_body_hash="b" * 64,
        anchor_text="アンカー",
        inserted_paragraph="段落",
        insertion_line=3,
    )
    original = compute_proposal_hash(**base)

    for field, changed in (
        ("anchor_text", "アンカー2"),
        ("inserted_paragraph", "段落2"),
        ("insertion_line", 4),
        ("target_article_id", 16),
        ("proposed_body_hash", "c" * 64),
    ):
        assert compute_proposal_hash(**{**base, field: changed}) != original


def test_proposal_hash_has_no_field_boundary_collision() -> None:
    """隣接する値の切れ目が変わっただけで同じ hash にならない。"""

    base = dict(
        change_type="add_internal_link",
        source_article_id=18,
        target_article_id=17,
        source_body_hash="a" * 64,
        proposed_body_hash="b" * 64,
        insertion_line=3,
    )
    first = compute_proposal_hash(**base, anchor_text="AB", inserted_paragraph="C")
    second = compute_proposal_hash(**base, anchor_text="A", inserted_paragraph="BC")

    assert first != second


def test_has_link_to_normalizes_urls() -> None:
    body = f"[x]({_URL})"

    assert has_link_to(body, _URL)
    assert has_link_to(body, _URL.rstrip("/"))
    assert not has_link_to(body, "https://example.com/other/")


def test_non_link_text_unchanged_detects_replacement() -> None:
    assert not non_link_text_unchanged("元の文章\n", "書き換えた文章\n")
    assert non_link_text_unchanged("元の文章\n", "元の文章\n\n追記\n")


def test_diff_is_present_and_human_readable() -> None:
    proposal, _ = _build(_BODY)

    assert proposal.unified_diff.startswith("--- article-18 (current)")
    assert proposal.inserted_paragraph in proposal.unified_diff
    assert proposal.warnings == []
