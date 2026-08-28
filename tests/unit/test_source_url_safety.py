"""app/article/source_url_safety.py の検証。"""

import pytest

from app.article.source_url_safety import UrlSafetyError, validate_and_canonicalize


def test_https_required() -> None:
    with pytest.raises(UrlSafetyError):
        validate_and_canonicalize("http://www.make.com/pricing")


def test_userinfo_rejected() -> None:
    with pytest.raises(UrlSafetyError):
        validate_and_canonicalize("https://user:pass@example.com/")


@pytest.mark.parametrize(
    "url",
    [
        "https://make.com/?token=abc",
        "https://make.com/?api_key=xxx",
        "https://make.com/?secret=y",
        "https://make.com/?password=z",
    ],
)
def test_credential_query_rejected(url: str) -> None:
    with pytest.raises(UrlSafetyError):
        validate_and_canonicalize(url)


def test_tracking_query_is_stripped_not_rejected() -> None:
    out = validate_and_canonicalize(
        "https://www.make.com/en/pricing?utm_source=x&ref=y&plan=team#frag"
    )
    assert out == "https://www.make.com/en/pricing?plan=team"
    assert "utm_" not in out and "ref=" not in out and "#" not in out


@pytest.mark.parametrize(
    "url",
    [
        "https://make.pxf.io/abc",
        "https://go.partnerstack.com/x",
        "https://track.example.com/",
        "https://bit.ly/xyz",
    ],
)
def test_known_tracking_hosts_rejected(url: str) -> None:
    with pytest.raises(UrlSafetyError):
        validate_and_canonicalize(url)


def test_blocked_hosts_from_affiliate_tracking() -> None:
    with pytest.raises(UrlSafetyError):
        validate_and_canonicalize(
            "https://aff.example.test/go", blocked_hosts=frozenset({"aff.example.test"})
        )


def test_clean_official_url_passes_and_gets_path_slash() -> None:
    assert validate_and_canonicalize("https://www.make.com") == "https://www.make.com/"
    assert (
        validate_and_canonicalize("https://www.make.com/en/pricing")
        == "https://www.make.com/en/pricing"
    )
