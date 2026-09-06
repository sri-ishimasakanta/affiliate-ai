"""app/affiliate/destination_safety.py — validate-only affiliate destination 検証。"""

from __future__ import annotations

import pytest

from app.affiliate.destination_safety import (
    AffiliateDestinationError,
    normalize_host,
    validate_destination_url,
)

_OK = "https://sub.affiliate.example.test/track/abc?a8mat=ABC123&utm_source=x#frag"


def test_https_accepted_and_url_returned_unchanged() -> None:
    facts = validate_destination_url(_OK)
    # exact 文字列を無改変で返す (canonicalize しない)
    assert facts.destination_url == _OK
    assert facts.destination_host == "sub.affiliate.example.test"


def test_affiliate_query_and_fragment_preserved_exactly() -> None:
    facts = validate_destination_url(_OK)
    assert "a8mat=ABC123" in facts.destination_url
    assert "utm_source=x" in facts.destination_url  # tracking param を除去しない
    assert facts.destination_url.endswith("#frag")  # fragment を落とさない


@pytest.mark.parametrize(
    "url",
    [
        "http://affiliate.example.test/x",
        "javascript:alert(1)",
        "data:text/html,<b>x</b>",
        "file:///etc/passwd",
        "blob:https://affiliate.example.test/abc",
        "vbscript:msgbox(1)",
        "ftp://affiliate.example.test/x",
        "//affiliate.example.test/x",  # scheme-relative
        "affiliate.example.test/x",  # no scheme
    ],
)
def test_non_https_schemes_rejected(url: str) -> None:
    with pytest.raises(AffiliateDestinationError):
        validate_destination_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://user@affiliate.example.test/x",
        "https://user:pass@affiliate.example.test/x",
        "https://affiliate.example.test@evil.example/x",
    ],
)
def test_userinfo_rejected(url: str) -> None:
    with pytest.raises(AffiliateDestinationError):
        validate_destination_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://affiliate.example.test/ a",
        "https://affiliate.example.test/\tx",
        "https://affiliate.example.test/\x01x",
        "https://affiliate.example.test/x\n",
    ],
)
def test_whitespace_and_control_chars_rejected(url: str) -> None:
    with pytest.raises(AffiliateDestinationError):
        validate_destination_url(url)


@pytest.mark.parametrize("url", ["https:///x", "https://", "https://:443/x"])
def test_missing_or_invalid_host_rejected(url: str) -> None:
    with pytest.raises(AffiliateDestinationError):
        validate_destination_url(url)


def test_explicit_port_443_accepted() -> None:
    facts = validate_destination_url("https://affiliate.example.test:443/x")
    assert facts.destination_host == "affiliate.example.test"


@pytest.mark.parametrize(
    "url",
    [
        "https://affiliate.example.test:80/x",
        "https://affiliate.example.test:8443/x",
        "https://affiliate.example.test:notaport/x",
    ],
)
def test_other_explicit_ports_rejected(url: str) -> None:
    with pytest.raises(AffiliateDestinationError):
        validate_destination_url(url)


def test_host_normalization_lowercase_and_idna() -> None:
    assert normalize_host("Example.COM") == "example.com"
    idn = normalize_host("日本語.example")
    assert idn.startswith("xn--") and idn.isascii() and idn == idn.lower()
    assert (
        validate_destination_url("https://PX.Affiliate.Example.Test/x").destination_host
        == "px.affiliate.example.test"
    )


def test_trailing_dot_host_rejected() -> None:
    with pytest.raises(AffiliateDestinationError):
        validate_destination_url("https://affiliate.example.test./x")


@pytest.mark.parametrize(
    "url",
    [
        "https://bizfluxlab.com/go/abc",
        "https://www.bizfluxlab.com/x",
    ],
)
def test_self_host_rejected(url: str) -> None:
    with pytest.raises(AffiliateDestinationError):
        validate_destination_url(url)


def test_error_message_does_not_echo_the_url() -> None:
    secret = "https://affiliate.example.test/x?a8mat=SUPERSECRETTRACKID"
    with pytest.raises(AffiliateDestinationError) as exc:
        validate_destination_url("http://" + secret[8:])
    assert "SUPERSECRETTRACKID" not in str(exc.value)
