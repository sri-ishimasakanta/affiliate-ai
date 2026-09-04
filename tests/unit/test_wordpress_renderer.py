"""app/wordpress/renderer.py の pure テスト。"""

from __future__ import annotations

from app.wordpress.renderer import RENDERER_VERSION, render_wordpress_html

_MD = """## 見出し2

本文です。詳細は https://www.make.com/ を参照。全角？は許可。

### 小見出し3

- 項目1
- 項目2

1. 番号1
2. 番号2

> PR／広告：本記事は広告を含みます。

| ツール | 料金 |
|---|---|
| Make | ¥0 |
| Todoist | ¥672 |
"""


def test_renderer_version_and_hash_shape() -> None:
    r = render_wordpress_html(_MD)
    assert r.renderer_version == RENDERER_VERSION == "wordpress_html_v1"
    assert len(r.html_hash) == 64


def test_deterministic_same_markdown_same_hash() -> None:
    assert render_wordpress_html(_MD).html_hash == render_wordpress_html(_MD).html_hash


def test_meaningful_source_change_changes_hash() -> None:
    a = render_wordpress_html(_MD).html_hash
    b = render_wordpress_html(_MD.replace("見出し2", "見出しX2")).html_hash
    assert a != b


def test_japanese_utf8_deterministic() -> None:
    hs = {render_wordpress_html(_MD).html_hash for _ in range(5)}
    assert len(hs) == 1


def test_headings_h2_h3_no_h1() -> None:
    r = render_wordpress_html("# H1む\n\n## H2\n\n### H3\n")
    # source の H1 は <h2> へ降格 (body から <h1> は出さない)
    assert r.h1_count == 0
    assert "<h1" not in r.html
    assert r.h2_count >= 1 and r.h3_count >= 1


def test_blockquote_and_lists() -> None:
    r = render_wordpress_html(_MD)
    assert "<blockquote>" in r.html
    assert "<ul>" in r.html and "<ol>" in r.html
    assert "<li>項目1</li>" in r.html


def test_table_semantic_and_mobile_wrapper() -> None:
    r = render_wordpress_html(_MD)
    assert r.table_count == 1
    assert "<table>" in r.html and "<thead>" in r.html and "<tbody>" in r.html
    assert "<th>" in r.html and "<td>" in r.html
    assert '<div class="wp-table-scroll" style="overflow-x:auto"><table>' in r.html


def test_bare_url_becomes_anchor_with_safe_rel() -> None:
    r = render_wordpress_html("本文 https://www.make.com/ 参照。\n")
    assert r.external_links == ["https://www.make.com/"]
    assert (
        '<a href="https://www.make.com/" target="_blank" rel="noopener noreferrer">'
        in r.html
    )
    # 公式外部リンクに nofollow / sponsored は付けない
    assert "nofollow" not in r.html
    assert "sponsored" not in r.html


def test_bare_url_stops_at_japanese_punctuation() -> None:
    md = "公式サイト（https://www.make.com/）でご確認ください（掲載準備中です）。\n"
    r = render_wordpress_html(md)
    assert r.external_links == ["https://www.make.com/"]
    assert 'href="https://www.make.com/"' in r.html
    assert "でご確認ください" not in r.html.split('href="')[1].split('"')[0]


def test_url_query_string_preserved() -> None:
    r = render_wordpress_html("https://ex.com/go?a=1&b=2#x 参照\n")
    assert r.external_links == ["https://ex.com/go?a=1&b=2#x"]


def test_14_bare_urls_become_14_anchors_hrefs_unchanged() -> None:
    urls = [
        "https://www.make.com/", "https://www.make.com/en/pricing",
        "https://www.hubspot.com/", "https://www.hubspot.com/pricing/marketing",
        "https://clickup.com/", "https://clickup.com/pricing",
        "https://monday.com/", "https://monday.com/pricing",
        "https://www.pipedrive.com/", "https://www.pipedrive.com/en/pricing",
        "https://reclaim.ai/", "https://reclaim.ai/pricing",
        "https://todoist.com/", "https://todoist.com/pricing",
    ]
    md = "\n\n".join(f"参照 {u} です。" for u in urls) + "\n"
    r = render_wordpress_html(md)
    assert r.external_links == urls
    for u in urls:
        assert f'<a href="{u}" target="_blank" rel="noopener noreferrer">' in r.html


def test_raw_source_html_is_escaped() -> None:
    r = render_wordpress_html("普通の段落 <b>bold</b> と <div>x</div>\n")
    assert "<b>bold</b>" not in r.html
    assert "&lt;b&gt;" in r.html
