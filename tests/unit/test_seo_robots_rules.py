"""app.seo.robots_rules の単体テスト (C5.1)。

本番の robots.txt (wp-admin 除外 + admin-ajax 許可 + Sitemap 宣言) が、記事 URL を
一切ブロックしないことを pin する。管理領域の除外は **期待どおりの挙動** であり、
ここで弱めることはしない。
"""

from __future__ import annotations

from app.seo.robots_rules import parse_robots_txt

_PRODUCTION = """User-agent: *
Disallow: /wp-admin/
Allow: /wp-admin/admin-ajax.php

Sitemap: https://bizfluxlab.com/wp-sitemap.xml
"""


def test_production_robots_allows_every_article_path() -> None:
    robots = parse_robots_txt(_PRODUCTION)
    for path in ("/rpa-tools/", "/ai-agents/", "/generative-ai-guidelines/", "/"):
        assert robots.is_allowed(path)


def test_production_robots_blocks_admin_but_allows_admin_ajax() -> None:
    robots = parse_robots_txt(_PRODUCTION)
    assert not robots.is_allowed("/wp-admin/")
    assert not robots.is_allowed("/wp-admin/options.php")
    assert robots.is_allowed("/wp-admin/admin-ajax.php")


def test_sitemap_declaration_is_collected() -> None:
    assert parse_robots_txt(_PRODUCTION).sitemaps == ["https://bizfluxlab.com/wp-sitemap.xml"]


def test_sitemap_is_group_independent() -> None:
    text = "Sitemap: https://x.test/s.xml\nUser-agent: *\nDisallow: /a/\n"
    robots = parse_robots_txt(text)
    assert robots.sitemaps == ["https://x.test/s.xml"]
    assert not robots.is_allowed("/a/")


def test_longest_match_wins() -> None:
    robots = parse_robots_txt("User-agent: *\nDisallow: /a/\nAllow: /a/keep/\n")
    assert not robots.is_allowed("/a/other/")
    assert robots.is_allowed("/a/keep/page/")


def test_allow_wins_on_equal_length() -> None:
    robots = parse_robots_txt("User-agent: *\nDisallow: /a/\nAllow: /a/\n")
    assert robots.is_allowed("/a/")


def test_wildcard_and_anchor_are_supported() -> None:
    robots = parse_robots_txt("User-agent: *\nDisallow: /*.php$\n")
    assert not robots.is_allowed("/wp-login.php")
    assert robots.is_allowed("/wp-login.php/extra")


def test_rules_for_other_agents_are_ignored() -> None:
    robots = parse_robots_txt("User-agent: BadBot\nDisallow: /\n")
    assert robots.is_allowed("/rpa-tools/")


def test_empty_disallow_means_allow_all() -> None:
    robots = parse_robots_txt("User-agent: *\nDisallow:\n")
    assert robots.is_allowed("/anything/")


def test_comments_are_stripped() -> None:
    robots = parse_robots_txt("User-agent: *  # all\nDisallow: /a/ # private\n")
    assert not robots.is_allowed("/a/")


def test_no_rules_means_everything_is_allowed() -> None:
    assert parse_robots_txt("").is_allowed("/rpa-tools/")
