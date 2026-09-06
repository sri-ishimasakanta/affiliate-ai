"""app/affiliate/link_identity.py — deterministic link identity hash。"""

from __future__ import annotations

import hashlib

from app.affiliate.link_identity import compute_link_identity_hash
from app.article.draft_input_canonical import canonical_json

_URL = "https://affiliate.example.test/track/x?a8mat=ABC123&sub=1"


def test_identity_is_deterministic_and_64_hex() -> None:
    a = compute_link_identity_hash(
        article_id=1, affiliate_program_id=2, destination_url=_URL
    )
    b = compute_link_identity_hash(
        article_id=1, affiliate_program_id=2, destination_url=_URL
    )
    assert a == b and len(a) == 64
    assert all(c in "0123456789abcdef" for c in a)


def test_identity_binds_exactly_article_program_destination() -> None:
    expected = hashlib.sha256(
        canonical_json(
            {
                "article_id": 1,
                "affiliate_program_id": 2,
                "destination_url": _URL,
            }
        ).encode("utf-8")
    ).hexdigest()
    assert (
        compute_link_identity_hash(
            article_id=1, affiliate_program_id=2, destination_url=_URL
        )
        == expected
    )


def test_identity_changes_when_exact_destination_string_changes() -> None:
    base = compute_link_identity_hash(
        article_id=1, affiliate_program_id=2, destination_url=_URL
    )
    # query param 並び替えだけでも別 identity (exact string binding)
    reordered = "https://affiliate.example.test/track/x?sub=1&a8mat=ABC123"
    assert (
        compute_link_identity_hash(
            article_id=1, affiliate_program_id=2, destination_url=reordered
        )
        != base
    )
    # trailing slash 追加でも別
    assert (
        compute_link_identity_hash(
            article_id=1, affiliate_program_id=2, destination_url=_URL + "&x=2"
        )
        != base
    )


def test_identity_changes_per_article_and_program() -> None:
    base = compute_link_identity_hash(
        article_id=1, affiliate_program_id=2, destination_url=_URL
    )
    assert (
        compute_link_identity_hash(
            article_id=9, affiliate_program_id=2, destination_url=_URL
        )
        != base
    )
    assert (
        compute_link_identity_hash(
            article_id=1, affiliate_program_id=9, destination_url=_URL
        )
        != base
    )
