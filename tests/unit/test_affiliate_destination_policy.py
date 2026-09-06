"""app/affiliate/destination_policy.py — independent exact-host allowlist。"""

from __future__ import annotations

from app.affiliate.destination_policy import (
    DEFAULT_DESTINATION_HOST_POLICY,
    is_host_approved,
)

_POLICY = {
    "a8": frozenset({"px.affiliate.example.test"}),
    "moshimo": frozenset({"af.moshimo.example.test"}),
    "empty_provider": frozenset(),
}


def test_production_default_policy_is_empty_fail_closed() -> None:
    assert DEFAULT_DESTINATION_HOST_POLICY == {}
    # production では何も承認されない
    assert not is_host_approved(
        provider="a8", destination_host="px.affiliate.example.test"
    )


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
