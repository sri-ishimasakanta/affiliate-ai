"""WordPressDraftRun status lifecycle helper の検証。"""

from __future__ import annotations

import pytest

from app.models.wordpress_draft_run import (
    WP_RUN_CANCELLED,
    WP_RUN_FAILED,
    WP_RUN_PREPARED,
    WP_RUN_RUNNING,
    WP_RUN_SUCCEEDED,
    WP_RUN_TERMINAL_STATUSES,
    wp_run_transition_allowed,
)


@pytest.mark.parametrize(
    ("cur", "tgt"),
    [
        (WP_RUN_PREPARED, WP_RUN_RUNNING),
        (WP_RUN_RUNNING, WP_RUN_SUCCEEDED),
        (WP_RUN_RUNNING, WP_RUN_FAILED),
        (WP_RUN_PREPARED, WP_RUN_CANCELLED),
    ],
)
def test_allowed_transitions(cur: str, tgt: str) -> None:
    assert wp_run_transition_allowed(cur, tgt) is True


@pytest.mark.parametrize(
    ("cur", "tgt"),
    [
        (WP_RUN_PREPARED, WP_RUN_SUCCEEDED),
        (WP_RUN_PREPARED, WP_RUN_FAILED),
        (WP_RUN_SUCCEEDED, WP_RUN_RUNNING),
        (WP_RUN_FAILED, WP_RUN_RUNNING),
        (WP_RUN_CANCELLED, WP_RUN_RUNNING),
        (WP_RUN_RUNNING, WP_RUN_PREPARED),
        (WP_RUN_RUNNING, WP_RUN_CANCELLED),
        (WP_RUN_PREPARED, WP_RUN_PREPARED),  # 同一 status も不可
        (WP_RUN_SUCCEEDED, WP_RUN_FAILED),
    ],
)
def test_rejected_transitions(cur: str, tgt: str) -> None:
    assert wp_run_transition_allowed(cur, tgt) is False


def test_terminal_states_have_no_outgoing() -> None:
    for st in WP_RUN_TERMINAL_STATUSES:
        for tgt in (WP_RUN_PREPARED, WP_RUN_RUNNING, WP_RUN_SUCCEEDED,
                    WP_RUN_FAILED, WP_RUN_CANCELLED):
            assert wp_run_transition_allowed(st, tgt) is False
