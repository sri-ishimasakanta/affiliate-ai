"""app.wordpress.content_reconciliation の単体テスト (C4.9)。

WordPress の正当な sanitize (属性追加・属性順の変化・改行の差) を吸収しつつ、
本文・リンク先・見出し構造の実質的な変化は検出することを pin する。
"""

from __future__ import annotations

from app.wordpress.content_reconciliation import (
    compare_wordpress_content,
    extract_heading_sequence,
    extract_hrefs,
    normalize_visible_text,
)


def test_normalize_visible_text_strips_tags_entities_and_whitespace() -> None:
    assert normalize_visible_text("<p>a &amp; b</p>\n<p>  c </p>") == "a&bc"


def test_extract_hrefs_preserves_order_and_duplicates() -> None:
    html = (
        '<a href="https://a.test">1</a><a href="https://b.test">2</a><a href="https://a.test">3</a>'
    )
    assert extract_hrefs(html) == ("https://a.test", "https://b.test", "https://a.test")


def test_extract_heading_sequence_is_lowercased_and_ordered() -> None:
    assert extract_heading_sequence("<H2>a</H2><h3>b</h3><h2>c</h2>") == ("h2", "h3", "h2")


def test_added_attributes_are_equivalent() -> None:
    intended = '<p>本文</p><h2>見出し</h2><a href="https://x.test">出典</a>'
    stored = (
        '<p style="line-height:1.8">本文</p>\n'
        '<h2 class="wp-block-heading">見出し</h2>\n'
        '<a href="https://x.test" rel="noopener" target="_blank">出典</a>'
    )
    assert compare_wordpress_content(intended, stored).equivalent is True


def test_changed_text_is_not_equivalent() -> None:
    result = compare_wordpress_content("<p>本文</p>", "<p>別の本文</p>")
    assert result.equivalent is False
    assert result.visible_text_matches is False


def test_swapped_href_is_not_equivalent() -> None:
    intended = '<a href="https://official.test/x">公式</a>'
    stored = '<a href="https://evil.test/x">公式</a>'
    result = compare_wordpress_content(intended, stored)
    assert result.equivalent is False
    assert result.hrefs_match is False
    assert result.visible_text_matches is True


def test_dropped_link_is_not_equivalent() -> None:
    intended = '<p>出典は<a href="https://x.test">ここ</a></p>'
    stored = "<p>出典はここ</p>"
    result = compare_wordpress_content(intended, stored)
    assert result.equivalent is False
    assert result.hrefs_match is False
    assert result.observed_href_count == 0


def test_changed_heading_level_is_not_equivalent() -> None:
    result = compare_wordpress_content("<h2>a</h2>", "<h3>a</h3>")
    assert result.equivalent is False
    assert result.headings_match is False


def test_as_dict_contains_no_content() -> None:
    payload = compare_wordpress_content("<p>秘密の本文</p>", "<p>秘密の本文</p>").as_dict()
    assert "秘密" not in repr(payload)
    assert payload["equivalent"] is True
