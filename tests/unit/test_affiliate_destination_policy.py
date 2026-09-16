"""app/affiliate/destination_policy.py — independent exact-host allowlist。"""

from __future__ import annotations

import pytest

from app.affiliate.destination_policy import (
    DEFAULT_DESTINATION_HOST_POLICY,
    is_host_approved,
)

_POLICY = {
    "a8": frozenset({"px.affiliate.example.test"}),
    "moshimo": frozenset({"af.moshimo.example.test"}),
    "empty_provider": frozenset(),
}


def test_production_default_policy_is_fail_closed_except_explicit_grants() -> None:
    # D-F1: production policy はもう完全に空ではない (Make が最初の承認済み
    # provider) が、それ以外は依然として fail closed のまま -- 未承認の
    # provider/host には一切影響しないこと。
    assert not is_host_approved(
        provider="a8", destination_host="px.affiliate.example.test"
    )
    assert not is_host_approved(
        provider="moshimo", destination_host="af.moshimo.example.test"
    )
    assert not is_host_approved(provider="unknown-asp", destination_host="example.test")


def test_production_default_policy_has_only_make_approved() -> None:
    # D-F1 が追加した唯一の実 provider/host。将来 D-F フェーズが新しい provider
    # を追加するまで、これ以外のキーが production policy に存在しないこと。
    assert set(DEFAULT_DESTINATION_HOST_POLICY.keys()) == {"make"}
    assert DEFAULT_DESTINATION_HOST_POLICY["make"] == frozenset({"www.make.com"})


def test_production_make_exact_host_approved() -> None:
    assert is_host_approved(provider="make", destination_host="www.make.com")


@pytest.mark.parametrize(
    "host",
    [
        "make.com",  # www 無し (§4: 別 host として扱う)
        "sub.www.make.com",  # サブドメイン
        "evilmake.com",  # lookalike
        "www.make.com.evil.example",  # suffix trick
        "",  # 空 host
        "WWW.MAKE.COM",  # 未正規化 (呼び出し側が正規化する契約 -- ここでは false)
    ],
)
def test_production_make_non_exact_host_rejected(host: str) -> None:
    assert not is_host_approved(provider="make", destination_host=host)


def test_production_unknown_provider_with_make_host_rejected() -> None:
    # www.make.com が policy に存在していても、provider が違えば承認しない
    # (host だけで自動承認しない -- exact (provider, host) ペアのみ)。
    assert not is_host_approved(provider="unknown-asp", destination_host="www.make.com")


def test_exact_host_match_accepted() -> None:
    assert is_host_approved(
        provider="a8",
        destination_host="px.affiliate.example.test",
        policy=_POLICY,
    )


def test_subdomain_and_suffix_confusion_rejected() -> None:
    for host in (
        "evil.px.affiliate.example.test",  # extra subdomain
        "affiliate.example.test",  # parent
        "px.affiliate.example.test.evil.test",  # suffix trick
        "xpx.affiliate.example.test",  # prefix trick
        "PX.affiliate.example.test",  # not normalized (caller must normalize)
    ):
        assert not is_host_approved(provider="a8", destination_host=host, policy=_POLICY)


def test_unknown_provider_fails_closed() -> None:
    assert not is_host_approved(
        provider="unknown-asp",
        destination_host="px.affiliate.example.test",
        policy=_POLICY,
    )


def test_empty_provider_host_set_fails_closed() -> None:
    assert not is_host_approved(
        provider="empty_provider",
        destination_host="px.affiliate.example.test",
        policy=_POLICY,
    )


def test_none_provider_fails_closed() -> None:
    assert not is_host_approved(
        provider=None, destination_host="px.affiliate.example.test", policy=_POLICY
    )


def test_tracking_url_host_does_not_authorize_itself() -> None:
    # 「tracking_url に出てきた host だから承認」というショートカットは存在しない。
    seen_in_tracking_url = "aff.brand-new-network.example.test"
    assert not is_host_approved(
        provider="a8", destination_host=seen_in_tracking_url, policy=_POLICY
    )
    # policy にその provider が無ければ、その host が tracking_url に何度出ても False。
    assert not is_host_approved(
        provider="brand-new-network",
        destination_host=seen_in_tracking_url,
        policy=_POLICY,
    )
