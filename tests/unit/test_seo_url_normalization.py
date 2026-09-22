"""app.seo.url_normalization の単体テスト (C5.1)。

同じページの表記揺れ (末尾スラッシュ / www / scheme / percent-encoding) は吸収し、
**本当に別の URL** は決して同一視しないことを pin する。
"""

from __future__ import annotations

from app.seo.url_normalization import canonical_host, normalize_url_key, same_url

_A1_ENCODED = (
    "https://bizfluxlab.com/%e6%a5%ad%e5%8b%99%e5%8a%b9%e7%8e%87%e5%8c%96-"
    "%e3%83%84%e3%83%bc%e3%83%ab-%e3%81%8a%e3%81%99%e3%81%99%e3%82%81-roundup/"
)
_A1_DECODED = "https://bizfluxlab.com/業務効率化-ツール-おすすめ-roundup/"


def test_trailing_slash_is_absorbed() -> None:
    assert same_url("https://x.test/a/", "https://x.test/a")


def test_scheme_is_absorbed() -> None:
    assert same_url("http://x.test/a/", "https://x.test/a/")


def test_www_is_absorbed() -> None:
    assert same_url("https://www.x.test/a/", "https://x.test/a/")


def test_host_case_is_absorbed() -> None:
    assert same_url("https://X.TEST/a/", "https://x.test/a/")


def test_percent_encoding_is_absorbed() -> None:
    assert same_url(_A1_ENCODED, _A1_DECODED)


def test_percent_encoding_case_is_absorbed() -> None:
    assert same_url(_A1_ENCODED, _A1_ENCODED.replace("%e6", "%E6"))


def test_different_paths_are_not_merged() -> None:
    assert not same_url("https://x.test/a/", "https://x.test/b/")


def test_nearly_identical_paths_are_not_merged() -> None:
    assert not same_url("https://x.test/rpa-tools/", "https://x.test/rpa-tool/")


def test_query_string_is_significant() -> None:
    assert not same_url("https://x.test/a/", "https://x.test/a/?page=2")


def test_different_hosts_are_not_merged() -> None:
    assert not same_url("https://x.test/a/", "https://y.test/a/")


def test_root_path_normalizes_consistently() -> None:
    assert normalize_url_key("https://x.test") == normalize_url_key("https://x.test/")


def test_unparseable_input_returns_none() -> None:
    assert normalize_url_key("") is None
    assert normalize_url_key(None) is None
    assert normalize_url_key("/relative/only") is None


def test_none_never_matches() -> None:
    assert not same_url(None, None)
    assert not same_url("https://x.test/a/", None)


def test_canonical_host_strips_www_only_at_the_front() -> None:
    assert canonical_host("WWW.x.test") == "x.test"
    assert canonical_host("shop.www.x.test") == "shop.www.x.test"
