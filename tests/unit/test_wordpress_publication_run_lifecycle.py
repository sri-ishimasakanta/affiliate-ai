"""WordPressPublicationRun の status lifecycle helper の検証。"""

from __future__ import annotations

import pytest

from app.models.wordpress_publication_run import (
    WP_PUBRUN_CANCELLED,
    WP_PUBRUN_FAILED,
    WP_PUBRUN_PREPARED,
    WP_PUBRUN_RUNNING,
    WP_PUBRUN_SUCCEEDED,
    WP_PUBRUN_TERMINAL_STATUSES,
    wp_publication_run_transition_allowed,
)


@pytest.mark.parametrize(
    ("cur", "tgt"),
    [
        (WP_PUBRUN_PREPARED, WP_PUBRUN_RUNNING),
        (WP_PUBRUN_RUNNING, WP_PUBRUN_SUCCEEDED),
        (WP_PUBRUN_RUNNING, WP_PUBRUN_FAILED),
        (WP_PUBRUN_PREPARED, WP_PUBRUN_CANCELLED),
    ],
)
def test_allowed_transitions(cur: str, tgt: str) -> None:
    assert wp_publication_run_transition_allowed(cur, tgt) is True


@pytest.mark.parametrize(
    ("cur", "tgt"),
    [
        (WP_PUBRUN_FAILED, WP_PUBRUN_RUNNING),
        (WP_PUBRUN_SUCCEEDED, WP_PUBRUN_RUNNING),
        (WP_PUBRUN_CANCELLED, WP_PUBRUN_RUNNING),
        (WP_PUBRUN_PREPARED, WP_PUBRUN_SUCCEEDED),
        (WP_PUBRUN_PREPARED, WP_PUBRUN_FAILED),
        (WP_PUBRUN_RUNNING, WP_PUBRUN_PREPARED),
        (WP_PUBRUN_RUNNING, WP_PUBRUN_CANCELLED),
        (WP_PUBRUN_PREPARED, WP_PUBRUN_PREPARED),  # 同一 status も不可
        (WP_PUBRUN_SUCCEEDED, WP_PUBRUN_FAILED),
    ],
)
def test_rejected_transitions(cur: str, tgt: str) -> None:
    assert wp_publication_run_transition_allowed(cur, tgt) is False


def test_terminal_states_have_no_outgoing() -> None:
    for st in WP_PUBRUN_TERMINAL_STATUSES:
        for tgt in (
            WP_PUBRUN_PREPARED,
            WP_PUBRUN_RUNNING,
            WP_PUBRUN_SUCCEEDED,
            WP_PUBRUN_FAILED,
            WP_PUBRUN_CANCELLED,
        ):
            assert wp_publication_run_transition_allowed(st, tgt) is False
