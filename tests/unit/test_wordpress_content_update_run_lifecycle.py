"""WordPressContentUpdateRun の status lifecycle helper の検証 (D-D5B / D-D5B.1)。

D-D5A.1 §6 で確定: ``prepared`` を持たず ``running`` から直接始まる 4-state
(``AffiliateTargetProjectionPushRun`` と同じパターン)。
"""

from __future__ import annotations

import pytest

from app.models.wordpress_content_update_run import (
    WP_CONTENT_UPDATE_FAILED,
    WP_CONTENT_UPDATE_OUTCOME_UNKNOWN,
    WP_CONTENT_UPDATE_RUNNING,
    WP_CONTENT_UPDATE_STATUSES,
    WP_CONTENT_UPDATE_SUCCEEDED,
    WP_CONTENT_UPDATE_TERMINAL_STATUSES,
    WordPressContentUpdateRun,
    wp_content_update_run_transition_allowed,
)


def test_exactly_four_statuses() -> None:
    assert WP_CONTENT_UPDATE_STATUSES == {
        WP_CONTENT_UPDATE_RUNNING,
        WP_CONTENT_UPDATE_SUCCEEDED,
        WP_CONTENT_UPDATE_FAILED,
        WP_CONTENT_UPDATE_OUTCOME_UNKNOWN,
    }
    assert "prepared" not in WP_CONTENT_UPDATE_STATUSES


@pytest.mark.parametrize(
    ("cur", "tgt"),
    [
        (WP_CONTENT_UPDATE_RUNNING, WP_CONTENT_UPDATE_SUCCEEDED),
        (WP_CONTENT_UPDATE_RUNNING, WP_CONTENT_UPDATE_FAILED),
        (WP_CONTENT_UPDATE_RUNNING, WP_CONTENT_UPDATE_OUTCOME_UNKNOWN),
    ],
)
def test_allowed_transitions(cur: str, tgt: str) -> None:
    assert wp_content_update_run_transition_allowed(cur, tgt) is True


@pytest.mark.parametrize(
    ("cur", "tgt"),
    [
        (WP_CONTENT_UPDATE_SUCCEEDED, WP_CONTENT_UPDATE_RUNNING),
        (WP_CONTENT_UPDATE_FAILED, WP_CONTENT_UPDATE_RUNNING),
        (WP_CONTENT_UPDATE_OUTCOME_UNKNOWN, WP_CONTENT_UPDATE_RUNNING),
        (WP_CONTENT_UPDATE_SUCCEEDED, WP_CONTENT_UPDATE_FAILED),
        (WP_CONTENT_UPDATE_FAILED, WP_CONTENT_UPDATE_SUCCEEDED),
        (WP_CONTENT_UPDATE_OUTCOME_UNKNOWN, WP_CONTENT_UPDATE_SUCCEEDED),
        (WP_CONTENT_UPDATE_OUTCOME_UNKNOWN, WP_CONTENT_UPDATE_FAILED),
        (WP_CONTENT_UPDATE_RUNNING, WP_CONTENT_UPDATE_RUNNING),  # 同一 status も不可
        (WP_CONTENT_UPDATE_SUCCEEDED, WP_CONTENT_UPDATE_SUCCEEDED),
        ("prepared", WP_CONTENT_UPDATE_RUNNING),  # prepared という起点は無い
    ],
)
def test_rejected_transitions(cur: str, tgt: str) -> None:
    assert wp_content_update_run_transition_allowed(cur, tgt) is False


def test_terminal_states_have_no_outgoing() -> None:
    for st in WP_CONTENT_UPDATE_TERMINAL_STATUSES:
        for tgt in (
            WP_CONTENT_UPDATE_RUNNING,
            WP_CONTENT_UPDATE_SUCCEEDED,
            WP_CONTENT_UPDATE_FAILED,
            WP_CONTENT_UPDATE_OUTCOME_UNKNOWN,
        ):
            assert wp_content_update_run_transition_allowed(st, tgt) is False


def test_no_reset_or_retry_transition_from_terminal() -> None:
    # retry は新しい run を append する -- 既存 terminal run を running へ戻す経路は無い
    for st in WP_CONTENT_UPDATE_TERMINAL_STATUSES:
        assert wp_content_update_run_transition_allowed(st, WP_CONTENT_UPDATE_RUNNING) is False


# ==================== D-D5B.1 §9: ORM-level preflight evidence nullability =====
def test_orm_expected_and_observed_raw_hash_columns_are_not_null() -> None:
    """running は preflight 成功後にのみ作られるため、両方の raw hash 列は
    ORM metadata レベルで NOT NULL -- D-D5B では observed 側が誤って nullable
    だった (D-D5B.1 で修正)。"""

    cols = WordPressContentUpdateRun.__table__.columns
    assert cols["expected_pre_update_wordpress_raw_content_hash"].nullable is False
    assert cols["observed_pre_update_wordpress_raw_content_hash"].nullable is False


def test_orm_observed_modified_gmt_stays_nullable() -> None:
    # provider タイムスタンプは informational/audit only -- normative gate にしない。
    cols = WordPressContentUpdateRun.__table__.columns
    assert cols["observed_pre_update_modified_gmt_raw"].nullable is True
