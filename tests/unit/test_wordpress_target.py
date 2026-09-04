"""app/wordpress/target.py の pure テスト。"""

from __future__ import annotations

import pytest

from app.exceptions import WordPressTargetError
from app.wordpress.target import (
    canonicalize_wordpress_base_url,
    compute_target_request_identity_hash,
)

_RIH = "788c7f091072bc659bc40e63186b3c0cf88f959dbaf833205adad4a7fc1b2e14"


def test_trailing_slash_removed() -> None:
    assert canonicalize_wordpress_base_url("https://example.com/") == "https://example.com"
    assert canonicalize_wordpress_base_url("https://example.com") == "https://example.com"


def test_subdirectory_preserved() -> None:
    assert (
        canonicalize_wordpress_base_url("https://example.com/blog/")
        == "https://example.com/blog"
    )
    assert (
        canonicalize_wordpress_base_url("https://example.com/wp/site")
        == "https://example.com/wp/site"
    )


def test_host_and_scheme_lowercased_port_kept() -> None:
    assert (
        canonicalize_wordpress_base_url("HTTPS://Example.COM:8443/Blog/")
        == "https://example.com:8443/Blog"
    )


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "ftp://example.com",
        "https://user:pass@example.com",
        "https://example.com/?x=1",
        "https://example.com/#frag",
        "https:///pathonly",
        "https://example.com/wp-json",
        "https://example.com/wp-json/",
    ],
)
def test_rejects_invalid(bad: str) -> None:
    with pytest.raises(WordPressTargetError):
        canonicalize_wordpress_base_url(bad)


def test_same_canonical_target_same_hash() -> None:
    a = compute_target_request_identity_hash(
        request_identity_hash=_RIH, target_base_url="https://example.com"
    )
    b = compute_target_request_identity_hash(
        request_identity_hash=_RIH, target_base_url="https://example.com"
    )
    assert a == b and len(a) == 64


def test_target_hash_changes_on_domain_or_path_or_rih() -> None:
    base = compute_target_request_identity_hash(
        request_identity_hash=_RIH, target_base_url="https://example.com"
    )
    assert (
        compute_target_request_identity_hash(
            request_identity_hash=_RIH, target_base_url="https://example.org"
        )
        != base
    )
    assert (
        compute_target_request_identity_hash(
            request_identity_hash=_RIH, target_base_url="https://example.com/blog"
        )
        != base
    )
    assert (
        compute_target_request_identity_hash(
            request_identity_hash="0" * 64, target_base_url="https://example.com"
        )
        != base
    )
