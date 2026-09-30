"""N2 / N3: note の記録と手で写した数の記録 (手元の DB だけ。外に問い合わせない)。

pin する契約:

- 同期は既定で書かない。公開済みの記録は戻さない。URL が食い違えば同期しない。
- 流れ・頻度 (目安であって決まりではない)・関係する記事 (``/go/`` は扱わない)。
- 手で写す数: 目録の外・個人の情報らしい参照・負の数・端数の数・範囲の外・推定 (期間が観測の
  後) を拒む。同じ観測は 1 行。値が違えば止まり、直すときは新しい行が古い行を指す。
- 要約は標本の大きさを出し、少なければ比べない。表が無ければ止まる。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.models import Article, ManualMetricEntry, NotePiece
from app.n_track import metrics as mm
from app.services.manual_metrics_service import ManualMetricsService
from app.services.note_ledger_service import NoteLedgerError, NoteLedgerService
from app.social.note import links, review
from tests.unit.test_note_channel import NOW, _repo
from tests.unit.test_note_review import EDITED

POLICY = {"publication_url_hosts": ["note.com"], "weekly_guideline": 2,
          "link_convention": "none"}  # fmt: skip


def _drafts(tmp_path, *, publish=False):
    from scripts.propose_note_content import main

    root = _repo(tmp_path)
    assert main(["--draft", "top", "--no-db"], root=root, now=NOW) == 0
    path = next((root / "reports/note/drafts").glob("*.json"))
    draft = review.draft_from_dict(json.loads(path.read_text("utf-8")))
    if publish:
        review.apply_edit(draft, EDITED, commissions_known=False)
        review.submit(draft)
        review.approve(draft, content_hash=draft.content_hash, approved_by="human", now=NOW)
        review.record_publication(draft, url="https://note.com/u/n/p1", observed_at=NOW,
                                  published_hash=draft.content_hash, now=NOW, policy=POLICY)
        path.write_text(json.dumps(draft.as_dict(), ensure_ascii=False), encoding="utf-8")
    return root, path, draft


def test_sync_plans_then_writes_and_never_downgrades(session, tmp_path) -> None:
    root, path, draft = _drafts(tmp_path, publish=True)
    service = NoteLedgerService(session, policy=POLICY)
    plan = service.sync_from_drafts(root, now=NOW)
    assert plan["created"] == [draft.id] and not plan["executed"]
    assert session.scalar(select(func.count()).select_from(NotePiece)) == 0
    service.sync_from_drafts(root, execute=True, now=NOW)
    row = session.scalars(select(NotePiece)).one()
    assert (row.status, row.published_url, row.approved_hash) == (
        "published", "https://note.com/u/n/p1", draft.content_hash)
    assert service.sync_from_drafts(root, execute=True, now=NOW)["unchanged"] == [draft.id]
    data = json.loads(path.read_text("utf-8"))
    data["status"], data["publication"] = "approved", None
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    result = service.sync_from_drafts(root, execute=True, now=NOW)
    assert result["conflicts"] and "not synced" in result["conflicts"][0]
    assert session.scalars(select(NotePiece)).one().status == "published"
    assert service.published_bodies() == {f"note:{draft.id}": row.body_text}


def test_loop_status_and_cadence_are_guidelines_only(session, tmp_path) -> None:
    root, _path, draft = _drafts(tmp_path, publish=True)
    service = NoteLedgerService(session, policy=POLICY)
    service.sync_from_drafts(root, execute=True, now=NOW)
    status = service.loop_status()
    assert status["by_status"] == {"published": 1}
    assert "by hand" in status["pieces"][0]["next_step"]
    plan = service.cadence_plan(now=NOW + timedelta(days=1))
    assert plan["weeks"][-1]["published"] == 1 and plan["guideline_per_week"] == 2
    assert plan["this_week_remaining_vs_guideline"] == 1 and "not a quota" in plan["note"]


def test_related_links_skip_tracking_urls_and_tag_only_on_request(session, tmp_path) -> None:
    session.add_all([
        Article(title="Threadsの投稿を承認と自動公開に分けた", slug="a1", status="published",
                published_url="https://example.com/threads-approval/"),
        Article(title="Threadsの投稿を承認と自動公開に分けた", slug="a2", status="published",
                published_url="https://example.com/go/threads"),
        Article(title="まったく別の話題", slug="a3", status="published",
                published_url="https://example.com/other/"),
    ])
    session.commit()
    _root, _path, draft = _drafts(tmp_path)
    draft.working_title = "Threadsの投稿を「人の承認」と「自動公開」に分けた仕組み"
    found = NoteLedgerService(session, policy=POLICY).related_links(draft)
    assert [f["url"] for f in found] == ["https://example.com/threads-approval/"]
    assert found[0]["suggested_url"] == found[0]["url"]  # 既定は印なし
    tagged = links.tagged_url("https://example.com/x/?a=1", convention="utm", campaign="d1")
    assert "utm_source=note" in tagged and "a=1" in tagged and "utm_campaign=d1" in tagged
    with pytest.raises(ValueError, match="tracking"):
        links.tagged_url("https://example.com/go/x", convention="utm", campaign="d1")


def _entry(**over):
    base = {"subject_kind": "channel", "subject_ref": "note", "metric": "followers",
            "value": 12, "observed_at": NOW, "source_description": "note dashboard",
            "entered_by": "human"}  # fmt: skip
    return mm.MetricInput(**{**base, **over})


@pytest.mark.parametrize("over, match", [
    ({"subject_kind": "channel", "metric": "made_up"}, "unknown metric"),
    ({"subject_kind": "pilot", "subject_ref": "someone@example.com",
      "metric": "activated", "value": 1}, "no personal data"),
    ({"value": -1}, ">= 0"),
    ({"value": 1.5}, "whole"),
    ({"subject_kind": "pilot", "subject_ref": "pilot-01", "metric": "activated", "value": 2},
     "0 or 1"),
    ({"subject_kind": "pilot", "subject_ref": "pilot-01", "metric": "onboarding_difficulty",
      "value": 6}, "1-5"),
    ({"observed_at": datetime(2026, 9, 26, 9, 0)}, "timezone"),
    ({"period_start": date(2026, 9, 20), "period_end": date(2026, 9, 30)}, "no estimates"),
    ({"period_start": date(2026, 9, 20)}, "both"),
    ({"source_description": ""}, "copied from"),
])
def test_manual_entries_reject_unsupported_values(over, match) -> None:
    with pytest.raises(mm.MetricError, match=match):
        mm.validate(_entry(**over))


def test_manual_entries_are_idempotent_and_corrected_by_superseding(session) -> None:
    service = ManualMetricsService(session)
    assert service.record(_entry(), now=NOW)["recorded"] is False  # PLAN
    assert session.scalar(select(func.count()).select_from(ManualMetricEntry)) == 0
    first = service.record(_entry(), execute=True, now=NOW)
    assert service.record(_entry(), execute=True, now=NOW)["reason"] == "already recorded"
    with pytest.raises(mm.MetricError, match="supersedes"):
        service.record(_entry(value=13), execute=True, now=NOW)
    fixed = service.record(_entry(value=13, observed_at=NOW + timedelta(minutes=5)),
                           execute=True, supersedes=first["id"], now=NOW)
    rows = service.active_rows()
    assert [r["id"] for r in rows] == [fixed["id"]] and rows[0]["value"] == 13
    zero = service.record(_entry(subject_ref="threads", value=0), execute=True, now=NOW)
    assert zero["recorded"]  # 0 は結果として記録できる
    with pytest.raises(mm.MetricError, match="not in the ledger"):
        service.record(_entry(subject_kind="note_piece", subject_ref="draft-abcdef12",
                              metric="views"), now=NOW)


def test_summaries_state_the_sample_and_refuse_to_compare(session) -> None:
    service = ManualMetricsService(session)
    for i, ref in enumerate(("note", "wordpress")):
        service.record(_entry(subject_ref=ref, value=10 + i), execute=True, now=NOW)
    summary = service.summary("channel", "followers")
    assert summary["subjects"] == 2 and summary["small_sample"]
    assert "no conclusion" in summary["statement"] and summary["provenance"] == "manual_entry"


def test_missing_tables_refuse(tmp_path, capsys) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    factory = sessionmaker(bind=engine)
    with factory() as empty, pytest.raises(NoteLedgerError, match="a4a74a5bcb8b"):
        NoteLedgerService(empty)
    from scripts.manage_note_ledger import main as ledger_main
    from scripts.record_manual_metric import main as metric_main

    assert ledger_main(["status"], session_factory=factory) == 2
    assert metric_main(["list"], session_factory=factory) == 2
    assert "migration a4a74a5bcb8b" in capsys.readouterr().out


def test_record_cli_plans_by_default(session, capsys) -> None:
    from scripts.record_manual_metric import main

    def factory():
        return sessionmaker(bind=session.get_bind(), expire_on_commit=False)()

    base = ["record", "--kind", "channel", "--ref", "note", "--metric", "followers",
            "--value", "5", "--observed-at", "2026-09-26T09:00:00+09:00",
            "--source", "note dashboard", "--by", "human"]  # fmt: skip
    assert main(base, session_factory=factory, now=NOW) == 0
    assert "PLAN" in capsys.readouterr().out
    assert main([*base, "--execute"], session_factory=factory, now=NOW) == 0
    assert session.scalar(select(func.count()).select_from(ManualMetricEntry)) == 1
    assert datetime.now(UTC) > NOW
