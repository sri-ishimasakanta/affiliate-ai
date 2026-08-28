"""DraftInputSnapshotBuilder の read-only / 決定論 / grid / hash 境界テスト。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.article.fact_keys import FactKey
from app.exceptions import DraftInputNotReadyError
from app.models import (
    AffiliateProgram,
    Article,
    ArticleAffiliateProgram,
    ArticleFact,
    DraftInputSnapshot,
    Source,
)
from app.services.draft_input_snapshot_builder import DraftInputSnapshotBuilder
from tests.support.draft_input_fixture import build_scenario

_ALL_KEYS = [str(k) for k in FactKey]


def _build(session: Session, scenario):
    return DraftInputSnapshotBuilder(session).build(scenario.article_id, now=scenario.now)


def test_builder_is_read_only(session: Session) -> None:
    sc = build_scenario(session, n_tools=7)
    before = {
        t: session.scalar(select(func.count()).select_from(m))
        for t, m in {
            "sources": Source, "facts": ArticleFact, "links": ArticleAffiliateProgram,
            "snapshots": DraftInputSnapshot,
        }.items()
    }
    _build(session, sc)
    after = {
        t: session.scalar(select(func.count()).select_from(m))
        for t, m in {
            "sources": Source, "facts": ArticleFact, "links": ArticleAffiliateProgram,
            "snapshots": DraftInputSnapshot,
        }.items()
    }
    assert before == after
    assert after["snapshots"] == 0


def test_grid_is_119_cells_with_state_breakdown(session: Session) -> None:
    sc = build_scenario(session, n_tools=7, with_unknown=True)
    r = _build(session, sc)
    assert len(r.payload["tools"]) == 7
    total_cells = sum(len(t["cells"]) for t in r.payload["tools"])
    assert total_cells == 7 * 17 == 119
    c = r.payload["audit"]["counts"]
    assert c["verified"] == 42  # 7 tools * 6 required
    assert c["unknown"] == 7  # 7 tools * 1
    assert c["not_researched"] == 70  # 119 - 49
    assert c["present_latest_facts"] == 49
    # 各 tool cell は FactKey 定義順
    for tool in r.payload["tools"]:
        assert [cell["fact_key"] for cell in tool["cells"]] == _ALL_KEYS


def test_missing_cells_are_explicit_not_researched(session: Session) -> None:
    sc = build_scenario(session, n_tools=2, with_unknown=False)
    r = _build(session, sc)
    tool = r.payload["tools"][0]
    missing = [c for c in tool["cells"] if c["state"] == "not_researched"]
    assert len(missing) == 11  # 17 - 6 required
    for c in missing:
        assert c["fact_id"] is None
        assert c["value"] is None
        assert c["fresh"] is None
        assert c["claim_allowed"] is False
        assert c["source"] is None


def test_unknown_preserved_and_in_do_not_claim(session: Session) -> None:
    sc = build_scenario(session, n_tools=2, with_unknown=True)
    r = _build(session, sc)
    tool = r.payload["tools"][0]
    ai = next(c for c in tool["cells"] if c["fact_key"] == "ai_features")
    assert ai["state"] == "unknown"
    assert ai["value"] is None
    assert ai["unknown_reason"] == "not stated on official pages"
    assert ai["claim_allowed"] is False
    assert "ai_features" in tool["do_not_claim"]
    assert "ai_features" not in tool["usable_claims"]


def test_claim_partition_17_disjoint(session: Session) -> None:
    sc = build_scenario(session, n_tools=7)
    r = _build(session, sc)
    for tool in r.payload["tools"]:
        u, d = set(tool["usable_claims"]), set(tool["do_not_claim"])
        assert u | d == set(_ALL_KEYS)
        assert not (u & d)
        for cell in tool["cells"]:
            assert (cell["claim_allowed"]) == (cell["fact_key"] in u)


def test_sources_referenced_only_and_sorted(session: Session) -> None:
    sc = build_scenario(session, n_tools=2)
    # 参照されない Source を 1 件足す
    art = session.get(Article, sc.article_id)
    orphan = Source(
        article_id=art.id, source_type="official_help",
        source_url="https://example.com/unreferenced", title="x",
        checked_at=sc.now - timedelta(days=2),
    )
    session.add(orphan)
    session.commit()
    r = _build(session, sc)
    ids = [s["id"] for s in r.payload["sources"]]
    assert orphan.id not in ids
    assert ids == sorted(ids)


def test_primary_and_planning_role_separated(session: Session) -> None:
    sc = build_scenario(session, n_tools=7)
    r = _build(session, sc)
    sel = r.payload["selection"]
    assert sel["primary_affiliate_program_id"] == sc.primary_program_id
    assert sel["authority"] == "human_confirmed_article_affiliate_program.is_primary"
    primary_entry = next(
        e for e in r.payload["comparison_set"]
        if e["affiliate_program_id"] == sc.primary_program_id
    )
    assert primary_entry["is_primary"] is True
    assert "planning_role" in primary_entry  # advisory, 別キー
    non_primary = [e for e in r.payload["comparison_set"] if not e["is_primary"]]
    assert all(e["is_primary"] is False for e in non_primary)
    assert len([e for e in r.payload["comparison_set"] if e["is_primary"]]) == 1


def test_article_title_slug_are_persisted_not_plan(session: Session) -> None:
    sc = build_scenario(session, n_tools=2)
    art = session.get(Article, sc.article_id)
    r = _build(session, sc)
    assert r.payload["article"]["title"] == art.title
    assert r.payload["article"]["slug"] == art.slug
    # plan 由来の working_title / proposed_slug は audit にのみ入る
    assert "working_title" in r.payload["audit"]["plan"]
    assert "title" not in r.payload["plan"]
    assert "proposed_slug" not in r.payload["plan"]


def test_commission_canonicalized_in_payload(session: Session) -> None:
    sc = build_scenario(session, n_tools=2)
    r = _build(session, sc)
    for e in r.payload["comparison_set"]:
        assert isinstance(e["commission_value"], str)
        assert e["commission_value"].count(".") == 1
        assert len(e["commission_value"].split(".")[1]) == 4


def test_deterministic_hash_across_rebuilds(session: Session) -> None:
    sc = build_scenario(session, n_tools=7)
    h1 = _build(session, sc).content_hash
    h2 = DraftInputSnapshotBuilder(session).build(sc.article_id, now=sc.now).content_hash
    assert h1 == h2


def test_hash_ignores_built_at_within_freshness_bucket(session: Session) -> None:
    sc = build_scenario(session, n_tools=3)
    r1 = DraftInputSnapshotBuilder(session).build(sc.article_id, now=sc.now)
    r2 = DraftInputSnapshotBuilder(session).build(
        sc.article_id, now=sc.now + timedelta(hours=1)
    )
    assert r1.content_hash == r2.content_hash
    assert r1.payload["audit"]["built_at"] != r2.payload["audit"]["built_at"]


def test_hash_ignores_unreferenced_source_addition(session: Session) -> None:
    sc = build_scenario(session, n_tools=3)
    h1 = _build(session, sc).content_hash
    art = session.get(Article, sc.article_id)
    session.add(
        Source(
            article_id=art.id, source_type="official_help",
            source_url="https://example.com/extra", title="extra",
            checked_at=sc.now - timedelta(days=1),
        )
    )
    session.commit()
    h2 = _build(session, sc).content_hash
    assert h1 == h2


def test_hash_changes_on_article_title(session: Session) -> None:
    sc = build_scenario(session, n_tools=3)
    h1 = _build(session, sc).content_hash
    art = session.get(Article, sc.article_id)
    art.title = "Different Title"
    session.commit()
    assert _build(session, sc).content_hash != h1


def test_hash_changes_on_fact_value(session: Session) -> None:
    sc = build_scenario(session, n_tools=3)
    h1 = _build(session, sc).content_hash
    row = session.scalars(
        select(ArticleFact).where(ArticleFact.fact_key == "pricing_summary").limit(1)
    ).one()
    row.fact_value = "totally different pricing"
    session.commit()
    assert _build(session, sc).content_hash != h1


def test_hash_changes_on_primary_change(session: Session) -> None:
    sc = build_scenario(session, n_tools=3)
    h1 = _build(session, sc).content_hash
    links = session.scalars(
        select(ArticleAffiliateProgram)
        .where(ArticleAffiliateProgram.article_id == sc.article_id)
        .order_by(ArticleAffiliateProgram.id)
    ).all()
    links[0].is_primary = False
    links[1].is_primary = True
    session.commit()
    assert _build(session, sc).content_hash != h1


def test_hash_changes_on_commission_change(session: Session) -> None:
    sc = build_scenario(session, n_tools=3)
    h1 = _build(session, sc).content_hash
    prog = session.get(AffiliateProgram, sc.program_ids[0])
    prog.commission_value = 99.0
    session.commit()
    assert _build(session, sc).content_hash != h1


def test_hash_changes_on_affiliate_status_change(session: Session) -> None:
    sc = build_scenario(session, n_tools=3)
    h1 = _build(session, sc).content_hash
    prog = session.get(AffiliateProgram, sc.program_ids[1])
    prog.status = "paused"
    session.commit()
    assert _build(session, sc).content_hash != h1


def test_missing_source_reference_raises(session: Session) -> None:
    sc = build_scenario(session, n_tools=2)
    row = session.scalars(select(ArticleFact).limit(1)).one()
    row.source_id = 999999  # 存在しない Source
    session.commit()
    with pytest.raises(DraftInputNotReadyError):
        _build(session, sc)


def test_article_without_keyword_raises(session: Session) -> None:
    sc = build_scenario(session, n_tools=2)
    art = session.get(Article, sc.article_id)
    art.keyword_id = None
    session.commit()
    with pytest.raises(DraftInputNotReadyError):
        _build(session, sc)


def test_gate_status_reports_soft_failures_without_raising(session: Session) -> None:
    sc = build_scenario(session, n_tools=2, article_body="draft in progress")
    r = _build(session, sc)  # 例外を投げず gate_status で返す
    assert r.can_freeze is False
    assert "article_body_present" in r.gate_status["failed_gates"]
