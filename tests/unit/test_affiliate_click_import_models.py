"""AffiliateClickImportRun / AffiliateOutboundClick のモデル不変条件。"""

from __future__ import annotations

from app.models.affiliate_click_import_run import (
    ACI_FAILED,
    ACI_RUNNING,
    ACI_STATUSES,
    ACI_SUCCEEDED,
    ACI_TERMINAL_STATUSES,
    ACI_TRANSITIONS,
    FROZEN_FIELDS,
    AffiliateClickImportRun,
    aci_transition_allowed,
)
from app.models.affiliate_outbound_click import AffiliateOutboundClick


def test_status_constants() -> None:
    assert ACI_STATUSES == {ACI_RUNNING, ACI_SUCCEEDED, ACI_FAILED}
    assert ACI_TERMINAL_STATUSES == {ACI_SUCCEEDED, ACI_FAILED}


def test_transitions_only_from_running_and_no_reopen() -> None:
    assert aci_transition_allowed(ACI_RUNNING, ACI_SUCCEEDED)
    assert aci_transition_allowed(ACI_RUNNING, ACI_FAILED)
    # terminal は再遷移不可、同一 status も不可
    assert not aci_transition_allowed(ACI_SUCCEEDED, ACI_FAILED)
    assert not aci_transition_allowed(ACI_FAILED, ACI_SUCCEEDED)
    assert not aci_transition_allowed(ACI_RUNNING, ACI_RUNNING)
    assert ACI_TRANSITIONS[ACI_SUCCEEDED] == frozenset()
    assert ACI_TRANSITIONS[ACI_FAILED] == frozenset()


def test_frozen_fields_are_the_cursor_identity() -> None:
    assert FROZEN_FIELDS == ("requested_since_id", "requested_limit")


def test_run_tablename_and_no_updated_at() -> None:
    assert AffiliateClickImportRun.__tablename__ == "affiliate_click_import_runs"
    cols = set(AffiliateClickImportRun.__table__.columns.keys())
    assert "updated_at" not in cols
    assert "created_at" in cols


def test_click_tablename_and_unique_source_id() -> None:
    assert AffiliateOutboundClick.__tablename__ == "affiliate_outbound_clicks"
    uniques = {
        tuple(c.name for c in uc.columns)
        for uc in AffiliateOutboundClick.__table__.constraints
        if uc.__class__.__name__ == "UniqueConstraint"
    }
    assert ("source_click_id",) in uniques
    # attribution FK は control-plane へ張らない
    fk_targets = {
        fk.column.table.name
        for fk in AffiliateOutboundClick.__table__.foreign_keys
    }
    assert fk_targets == {"affiliate_click_import_runs"}
