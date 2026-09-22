"""公開更新の意図を書き起こす条件 (C9.4、pure)。

``published_update_intent_ok`` ゲートは弱めない。C9 は「人の承認という事実」を
意図として書き起こすだけで、その事実が揃っていなければ文字列を作れない。

pin する契約:

- PLAN (``execute=False``) では意図を作れない。
- 承認が無ければ作れない。
- 承認の hash が提案と一致しなければ作れない。
- 記事本文が承認後に変わっていれば作れない。
- 作れた文字列には、誰が何を承認したかが残る (空文字ではない)。
"""

from __future__ import annotations

import pytest

from app.change.published_update_intent import (
    PublishedUpdateIntentError,
    build_published_update_intent,
)

_HASH = "8" * 64
_SOURCE = "0" * 64

_BASE = dict(
    execute=True,
    change_request_id=1,
    change_type="add_internal_link",
    article_id=18,
    target_article_id=17,
    proposal_hash=_HASH,
    proposal_version=1,
    approval_id=1,
    approved_proposal_hash=_HASH,
    approved_by="human",
    expected_source_body_hash=_SOURCE,
    current_source_body_hash=_SOURCE,
)


def test_intent_records_who_approved_what() -> None:
    intent = build_published_update_intent(**_BASE)

    assert intent.strip()
    assert _HASH in intent
    assert "18" in intent and "17" in intent
    assert "human" in intent


def test_plan_mode_cannot_authorize_a_published_update() -> None:
    with pytest.raises(PublishedUpdateIntentError, match="PLAN never authorizes"):
        build_published_update_intent(**{**_BASE, "execute": False})


def test_an_unapproved_request_cannot_satisfy_the_intent() -> None:
    with pytest.raises(PublishedUpdateIntentError, match="no human approval"):
        build_published_update_intent(
            **{**_BASE, "approval_id": None, "approved_proposal_hash": None}
        )


def test_an_approval_for_another_proposal_cannot_satisfy_the_intent() -> None:
    with pytest.raises(PublishedUpdateIntentError, match="different proposal"):
        build_published_update_intent(**{**_BASE, "approved_proposal_hash": "f" * 64})


def test_a_changed_body_cannot_satisfy_the_intent() -> None:
    with pytest.raises(PublishedUpdateIntentError, match="body changed"):
        build_published_update_intent(**{**_BASE, "current_source_body_hash": "e" * 64})


def test_the_intent_is_never_a_constant_default() -> None:
    """記事や提案が違えば、書き起こされる意図も違う。"""

    first = build_published_update_intent(**_BASE)
    second = build_published_update_intent(
        **{**_BASE, "change_request_id": 2, "article_id": 19, "target_article_id": 21}
    )

    assert first != second
