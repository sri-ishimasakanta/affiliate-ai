"""モバイル承認の capability (C8.8、pure)。

pin する契約:

- 256 bit の CSPRNG。推測できず、重複しない。
- 保存するのは digest だけ。生の値からは一方向。
- 提案 hash / 版 / 期限に束縛される。別の依頼へ流用できない。
- 期限切れは決定できない。
- ログ/例外へ出す前に伏せられる。
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

from app.approval.capability import (
    CAPABILITY_ENTROPY_BYTES,
    DEFAULT_TTL_HOURS,
    MAX_TTL_HOURS,
    REASON_BINDING_MISMATCH,
    REASON_EXPIRED,
    REASON_MALFORMED,
    REASON_MISMATCH,
    REASON_OK,
    capability_binding,
    capability_digest,
    generate_capability,
    is_well_formed,
    redact,
    verify_capability,
)

_NOW = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)
_HASH = "b" * 64


def _issue(**overrides):
    kwargs = dict(
        subject_type="change_request",
        subject_id=12,
        subject_hash=_HASH,
        subject_version=1,
        issued_at=_NOW,
    )
    kwargs.update(overrides)
    return generate_capability(**kwargs)


_UNSET = object()


def _verify(issued, presented=_UNSET, *, now=_NOW, **overrides):
    kwargs = dict(
        presented=issued.secret if presented is _UNSET else presented,
        stored_digest=issued.digest,
        stored_binding=issued.binding,
        subject_type="change_request",
        subject_id=12,
        subject_hash=_HASH,
        subject_version=1,
        expires_at=issued.expires_at,
        now=now,
    )
    kwargs.update(overrides)
    return verify_capability(**kwargs)


def test_the_capability_carries_256_bits_of_randomness() -> None:
    assert CAPABILITY_ENTROPY_BYTES == 32

    issued = _issue()
    # token_urlsafe は base64url。復号したバイト数が実際のエントロピー。
    padded = issued.secret + "=" * (-len(issued.secret) % 4)
    assert len(base64.urlsafe_b64decode(padded)) == 32
    assert is_well_formed(issued.secret)


def test_capabilities_do_not_repeat() -> None:
    secrets_seen = {_issue().secret for _ in range(200)}

    assert len(secrets_seen) == 200


def test_only_the_digest_is_storable() -> None:
    issued = _issue()

    assert issued.digest == capability_digest(issued.secret)
    assert len(issued.digest) == 64
    assert issued.secret not in issued.digest
    assert issued.secret not in issued.binding


def test_a_matching_capability_verifies() -> None:
    issued = _issue()

    assert _verify(issued) == (True, REASON_OK)


def test_a_malformed_capability_is_rejected() -> None:
    issued = _issue()

    assert _verify(issued, "too-short")[1] == REASON_MALFORMED
    assert _verify(issued, "")[1] == REASON_MALFORMED
    assert _verify(issued, None)[1] == REASON_MALFORMED


def test_another_requests_capability_cannot_be_reused() -> None:
    first = _issue()
    second = _issue(subject_id=13)

    assert _verify(first, second.secret)[1] == REASON_MISMATCH


def test_a_changed_proposal_hash_breaks_the_binding() -> None:
    """提案が作り直されれば、古いリンクは使えない。"""

    issued = _issue()

    ok, reason = _verify(issued, subject_hash="c" * 64)

    assert ok is False
    assert reason == REASON_BINDING_MISMATCH


def test_a_changed_proposal_version_breaks_the_binding() -> None:
    issued = _issue()

    assert _verify(issued, subject_version=2)[1] == REASON_BINDING_MISMATCH


def test_a_changed_subject_type_breaks_the_binding() -> None:
    issued = _issue()

    assert _verify(issued, subject_type="threads_post")[1] == REASON_BINDING_MISMATCH


def test_an_expired_capability_cannot_decide() -> None:
    issued = _issue()

    assert _verify(issued, now=issued.expires_at)[1] == REASON_EXPIRED
    assert _verify(issued, now=issued.expires_at + timedelta(seconds=1))[1] == REASON_EXPIRED
    assert _verify(issued, now=issued.expires_at - timedelta(seconds=1))[0] is True


def test_the_default_expiry_is_24_hours_and_bounded() -> None:
    assert DEFAULT_TTL_HOURS == 24
    assert _issue().expires_at == _NOW + timedelta(hours=24)
    # 置き忘れたリンクを長生きさせない。
    assert _issue(ttl_hours=1000).expires_at == _NOW + timedelta(hours=MAX_TTL_HOURS)
    assert _issue(ttl_hours=0).expires_at == _NOW + timedelta(hours=1)


def test_the_binding_pins_the_expiry() -> None:
    first = capability_binding(
        subject_type="change_request",
        subject_id=12,
        subject_hash=_HASH,
        subject_version=1,
        expires_at=_NOW,
    )
    second = capability_binding(
        subject_type="change_request",
        subject_id=12,
        subject_hash=_HASH,
        subject_version=1,
        expires_at=_NOW + timedelta(hours=1),
    )

    assert first != second


def test_redact_removes_the_capability_from_text() -> None:
    issued = _issue()
    text = f"failed to open https://bizfluxlab.com/bfl-approval/abc#{issued.secret}"

    cleaned = redact(text, issued.secret)

    assert issued.secret not in cleaned
    assert "[redacted-capability]" in cleaned


def test_redact_also_catches_capability_shaped_strings() -> None:
    """既知の値を渡さなくても、形が一致すれば落とす。"""

    issued = _issue()

    cleaned = redact(f"token={issued.secret}")

    assert issued.secret not in cleaned
