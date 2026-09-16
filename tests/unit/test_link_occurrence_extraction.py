"""app.wordpress.link_occurrence.extract_link_occurrences — external-link occurrence
extraction end-to-end via the real (unchanged) render_wordpress_html renderer (D-D2)。
"""

from __future__ import annotations

from app.wordpress.link_occurrence import extract_link_occurrences
from app.wordpress.renderer import RENDERER_VERSION, render_wordpress_html

_BODY_HASH = "b" * 64


def _occurrences(markdown: str):
    rendered = render_wordpress_html(markdown)
    return extract_link_occurrences(
        external_links=rendered.external_links,
        canonical_body_hash=_BODY_HASH,
        renderer_version=RENDERER_VERSION,
    ), rendered


def test_zero_external_links() -> None:
    occ, _ = _occurrences("# Title\n\nNo links here.\n")
    assert occ == []


def test_one_external_link() -> None:
    occ, _ = _occurrences("See [tool](https://official.example.test/a).\n")
    assert len(occ) == 1
    assert occ[0].occurrence_ordinal == 0
    assert occ[0].original_href == "https://official.example.test/a"
    assert occ[0].original_host == "official.example.test"


def test_multiple_external_links_ordinal_ascending() -> None:
    md = (
        "[one](https://official.example.test/a) "
        "[two](https://official.example.test/b) "
        "[three](https://official.example.test/c)\n"
    )
    occ, _ = _occurrences(md)
    assert [o.occurrence_ordinal for o in occ] == [0, 1, 2]
    assert [o.original_href for o in occ] == [
        "https://official.example.test/a",
        "https://official.example.test/b",
        "https://official.example.test/c",
    ]


def test_duplicate_url_occurrences_not_collapsed() -> None:
    md = (
        "[first](https://official.example.test/x) and later "
        "[again](https://official.example.test/x)\n"
    )
    occ, _ = _occurrences(md)
    assert len(occ) == 2
    assert occ[0].original_href == occ[1].original_href == "https://official.example.test/x"
    assert occ[0].occurrence_ordinal == 0
    assert occ[1].occurrence_ordinal == 1
    assert occ[0].occurrence_identity_hash != occ[1].occurrence_identity_hash


def test_same_anchor_text_different_urls() -> None:
    md = (
        "[click here](https://official.example.test/a) "
        "[click here](https://official.example.test/b)\n"
    )
    occ, _ = _occurrences(md)
    assert len(occ) == 2
    assert occ[0].original_href == "https://official.example.test/a"
    assert occ[1].original_href == "https://official.example.test/b"


def test_same_url_different_anchor_text() -> None:
    md = (
        "[label one](https://official.example.test/x) "
        "[label two](https://official.example.test/x)\n"
    )
    occ, _ = _occurrences(md)
    assert len(occ) == 2
    assert occ[0].original_href == occ[1].original_href
    assert occ[0].occurrence_ordinal != occ[1].occurrence_ordinal


def test_internal_links_mixed_with_external_links() -> None:
    md = (
        "[internal](/local/page) "
        "[external one](https://official.example.test/a) "
        "[internal again](#section) "
        "[external two](https://official.example.test/b)\n"
    )
    occ, _ = _occurrences(md)
    assert len(occ) == 2
    assert occ[0].original_href == "https://official.example.test/a"
    assert occ[1].original_href == "https://official.example.test/b"


def test_internal_links_do_not_consume_external_ordinal() -> None:
    md = (
        "[internal](/local/page) "
        "[external](https://official.example.test/a)\n"
    )
    occ, _ = _occurrences(md)
    assert len(occ) == 1
    assert occ[0].occurrence_ordinal == 0  # internal link は ordinal 0 を消費しない


def test_tables_and_headings_do_not_disturb_order() -> None:
    md = (
        "# Heading\n\n"
        "[link one](https://official.example.test/a)\n\n"
        "| col a | col b |\n"
        "| --- | --- |\n"
        "| x | y |\n\n"
        "## Subheading\n\n"
        "[link two](https://official.example.test/b)\n\n"
        "[link three](https://official.example.test/c)\n"
    )
    occ, rendered = _occurrences(md)
    assert rendered.table_count == 1
    assert [o.original_href for o in occ] == [
        "https://official.example.test/a",
        "https://official.example.test/b",
        "https://official.example.test/c",
    ]
    assert [o.occurrence_ordinal for o in occ] == [0, 1, 2]


def test_original_href_preserved_exactly() -> None:
    weird_href = "https://official.example.test/Tool-A?x=1&y=2#frag"
    md = f"[tool]({weird_href})\n"
    occ, _ = _occurrences(md)
    assert len(occ) == 1
    assert occ[0].original_href == weird_href  # 1 文字も変更されていない


def test_bare_url_in_text_is_also_extracted_as_external_occurrence() -> None:
    """renderer は本文中の裸 URL を明示 link 化してから anchor 化する
    (_bare_urls_to_markdown_links) — occurrence 抽出はその最終結果を使う。"""

    md = "Official site: https://official.example.test/bare\n"
    occ, _ = _occurrences(md)
    assert len(occ) == 1
    assert occ[0].original_href == "https://official.example.test/bare"
