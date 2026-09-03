"""DraftPromptPreviewService の read-only / Snapshot-only / safety テスト。"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.article.draft_prompt_package import EditorialOverridesV1
from app.exceptions import DraftGenerationNotReadyError, EntityNotFoundError
from app.models import ArticleFact, DraftGenerationRun
from app.services.draft_prompt_preview_service import DraftPromptPreviewService
from tests.support.draft_generation_fixture import default_overrides, frozen_scenario


def _svc(session: Session) -> DraftPromptPreviewService:
    return DraftPromptPreviewService(session)


def test_preview_is_read_only_and_deterministic(session: Session) -> None:
    fs = frozen_scenario(session, n_tools=7)
    ov = default_overrides(fs)
    out1 = _svc(session).preview(fs.article_id, snapshot_id=fs.snapshot_id, overrides=ov)
    out2 = _svc(session).preview(fs.article_id, snapshot_id=fs.snapshot_id, overrides=ov)
    assert out1["prompt_input_hash"] == out2["prompt_input_hash"]
    assert out1["rendered_prompt_hash"] == out2["rendered_prompt_hash"]
    assert session.scalar(select(func.count()).select_from(DraftGenerationRun)) == 0


def test_preview_validation_summary_is_safe(session: Session) -> None:
    fs = frozen_scenario(session, n_tools=7)
    out = _svc(session).preview(
        fs.article_id, snapshot_id=fs.snapshot_id, overrides=default_overrides(fs)
    )
    vs = out["validation_summary"]
    assert vs["forbidden_structural_keys"] == 0
    assert vs["secret_keys"] == 0
    assert vs["snapshot_binding_valid"] is True
    assert vs["rendered_hash_valid"] is True
    assert vs["comparison_tools"] == 7
    assert vs["commission_to_llm"] is False
    # fixture: 7 tools * 1 unknown (ai_features)
    assert vs["unknown_fact_restrictions"] == 7


def test_preview_uses_snapshot_not_live_facts(session: Session) -> None:
    fs = frozen_scenario(session, n_tools=3)
    ov = default_overrides(fs)
    h1 = _svc(session).preview(
        fs.article_id, snapshot_id=fs.snapshot_id, overrides=ov
    )["prompt_input_hash"]
    # live ArticleFact を書き換えても preview hash は不変 (Snapshot が source)
    row = session.scalars(
        select(ArticleFact).where(ArticleFact.fact_key == "pricing_summary").limit(1)
    ).one()
    row.fact_value = "LIVE CHANGED"
    session.commit()
    h2 = _svc(session).preview(
        fs.article_id, snapshot_id=fs.snapshot_id, overrides=ov
    )["prompt_input_hash"]
    assert h1 == h2


def test_preview_rejects_snapshot_of_other_article(session: Session) -> None:
    fs = frozen_scenario(session, n_tools=2)
    other = frozen_scenario(session, n_tools=2, suffix="b")
    with pytest.raises(DraftGenerationNotReadyError):
        _svc(session).preview(
            other.article_id,
            snapshot_id=fs.snapshot_id,
            overrides=default_overrides(other),
        )


def test_preview_rejects_missing_snapshot(session: Session) -> None:
    fs = frozen_scenario(session, n_tools=2)
    with pytest.raises(EntityNotFoundError):
        _svc(session).preview(
            fs.article_id, snapshot_id=999999, overrides=default_overrides(fs)
        )


def test_preview_package_has_no_commission_or_planning_role(session: Session) -> None:
    fs = frozen_scenario(session, n_tools=7)
    out = _svc(session).preview(
        fs.article_id, snapshot_id=fs.snapshot_id, overrides=default_overrides(fs)
    )
    pkg = out["prompt_package"]

    def keys(o):
        if isinstance(o, dict):
            for k, v in o.items():
                yield k
                yield from keys(v)
        elif isinstance(o, list):
            for it in o:
                yield from keys(it)

    allk = set(keys(pkg))
    assert allk.isdisjoint(
        {"commission_type", "commission_value", "provider", "planning_role", "tracking_url"}
    )
    # unknown / not_researched が保持されている
    make = pkg["comparison_tools"][0]
    assert any(u["fact_key"] == "ai_features" for u in make["unknown_fact_keys"])


def test_preview_editorial_overrides_extra_forbidden() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        EditorialOverridesV1(
            primary="Make", comparison_set_size=7, unexpected="nope"
        )
