"""app/article/source_url_safety.py の検証。"""

import pytest

from app.article.source_url_safety import (
    UrlSafetyError,
    canonicalize_tracking_url,
    validate_and_canonicalize,
)


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


def test_the_affiliate_tracking_url_itself_is_rejected() -> None:
    tracking = canonicalize_tracking_url("https://www.make.com/en/register?pc=bizfluxlab")
    # アフィリエイトコードを持つパラメータは canonical 形に残るので、
    # 「コード付きの URL」だけが Source として弾かれる。
    assert tracking == "https://www.make.com/en/register?pc=bizfluxlab"
    with pytest.raises(UrlSafetyError):
        validate_and_canonicalize(
            "https://www.make.com/en/register?pc=bizfluxlab",
            blocked_urls=frozenset({tracking}),
        )
    # コードが付いていない同じページは、ただのベンダー公式ページなので通る。
    assert (
        validate_and_canonicalize(
            "https://www.make.com/en/register", blocked_urls=frozenset({tracking})
        )
        == "https://www.make.com/en/register"
    )


def test_other_pages_on_a_tracking_host_stay_usable_as_sources() -> None:
    """tracking URL と同じホストでも、別ページは公式 Source として使える。

    ホスト単位で弾くと、そのベンダーの公式料金ページを根拠にできなくなる。
    """
    tracking = canonicalize_tracking_url("https://www.make.com/en/register?pc=bizfluxlab")
    assert (
        validate_and_canonicalize(
            "https://www.make.com/en/pricing", blocked_urls=frozenset({tracking})
        )
        == "https://www.make.com/en/pricing"
    )


def test_known_redirect_hosts_are_still_rejected() -> None:
    for url in (
        "https://go.partnerstack.com/x",
        "https://track.example.test/go",
        "https://example.pxf.io/abc",
    ):
        with pytest.raises(UrlSafetyError):
            validate_and_canonicalize(url)


def test_clean_official_url_passes_and_gets_path_slash() -> None:
    assert validate_and_canonicalize("https://www.make.com") == "https://www.make.com/"
    assert (
        validate_and_canonicalize("https://www.make.com/en/pricing")
        == "https://www.make.com/en/pricing"
    )
