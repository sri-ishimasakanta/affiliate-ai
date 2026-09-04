"""renderer が source Markdown 由来の危険 HTML を無害化することの検証。"""

from __future__ import annotations

import re

import pytest

from app.wordpress.renderer import render_wordpress_html

_ATTACKS = [
    "<script>alert(1)</script>",
    '<img src=x onerror=alert(1)>',
    "<iframe src=https://evil.example></iframe>",
    "<style>body{display:none}</style>",
    '<a href="javascript:alert(1)">click</a>',
    '<div onclick="steal()">x</div>',
    '[click](javascript:alert(1))',
    "<svg/onload=alert(1)>",
]


@pytest.mark.parametrize("src", _ATTACKS)
def test_dangerous_source_html_produces_no_live_markup(src: str) -> None:
    """source 由来の危険 HTML は escape され、live なタグ/属性/スキームにならない。

    escape された結果テキスト (&lt;img … onerror=…&gt;) に文字列が残るのは無害。
    ここでは *実行可能な* markup が出力に無いことだけを検証する。
    """
    html = render_wordpress_html(src + "\n").html
    low = html.lower()
    # live なタグが出ていない
    for tag in ("<script", "<iframe", "<style", "<svg", "<object", "<embed", "<img"):
        assert tag not in low, tag
    # live な event handler 属性 (実タグに続く on…=) が出ていない
    assert not re.search(r"<[a-z][^>]*\son[a-z]+\s*=", low)
    # 危険スキームの href が出ていない
    for scheme in ('href="javascript:', 'href="data:', 'href="vbscript:'):
        assert scheme not in low
    # source が escape された証跡
    assert "&lt;" in html or render_wordpress_html(src + "\n").external_links == []


def test_markdown_link_with_javascript_scheme_is_neutralised() -> None:
    r = render_wordpress_html("[危険](javascript:alert(1))\n")
    assert "javascript:" not in r.html.lower()
    assert "危険" in r.html
    assert r.external_links == []
