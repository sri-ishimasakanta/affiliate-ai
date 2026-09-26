"""N0: note チャネルの土台 (ローカルだけ。note・WordPress・Threads・スケジューラ・DB に触れない)。

pin する契約:

- 候補の発見は決定的。同じ出来事から 2 つの候補を作らない。採点は透明で、順は決まっている。
- W1 / W2 / T7 の記録から候補が出る。根拠が欠ければ警告、半分に満たなければ候補にしない。
- 秘密・内部の値は伏せる。根拠の無い観測 / 決定は誤り。手数料 0 件で収益の主張は誤り。
- WordPress の記事・Threads の公開文の写しは重複として誤り。内部の言葉は本文に出さない。
- 状態: 承認の記録なしに approved、公開の証拠なしに published にできない。
- ネットワークを使わない。WordPress / Threads / スケジューラ / 投稿案の在庫に触れるコードを
  持たない。プロジェクトの状態に note の状態が出る (警告にはしない)。
"""

from __future__ import annotations

import json
import shutil
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.project_state.note_state import collect_note_channel
from app.social.note import catalog, safety
from app.social.note.candidates import discover, doc_paths, topic_for
from app.social.note.models import Claim, EvidenceRef, NoteDraft, NoteStatusError, transition
from app.social.note.renderer import build_draft, render_markdown
from app.social.note.sources import load_sources, parse_decision_log

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)
NOTE_SRC = REPO / "app/social/note"
REF = EvidenceRef("decision:x", "operations_doc", "docs/decision-log/x.md")


def _fact(value, authority="live_observed"):
    return {"value": value, "authority": authority, "source": "s", "observed_at": "t",
            "confidence": "high", "conflicts": []}  # fmt: skip


def _repo(tmp_path: Path, *, commissions=0, drop_decisions=()) -> Path:
    root = tmp_path / "repo"
    (root / "docs/operations").mkdir(parents=True)
    shutil.copy2(REPO / "docs/project-roadmap.json", root / "docs/project-roadmap.json")
    shutil.copytree(REPO / "docs/decision-log", root / "docs/decision-log")
    for name in doc_paths():
        shutil.copy2(REPO / name, root / name)
    if drop_decisions:
        for path in (root / "docs/decision-log").glob("*.md"):
            text = path.read_text(encoding="utf-8")
            for did in drop_decisions:
                text = text.replace(f"<!-- decision:{did} -->", "<!-- removed -->")
            path.write_text(text, encoding="utf-8")
    (root / "reports").mkdir()
    report = {
        "generated_at": "2026-09-26T08:00:00+00:00",
        "facts": {
            "wordpress_featured_images": _fact("25/25"),
            "wordpress_taxonomy_matches_plan": _fact(True),
            "threads_automatic_publication": _fact(True, "runtime_config"),
            "stock_maintenance_enabled": _fact(False, "runtime_config"),
            "make_tracked_articles": _fact([1, 10, 11]),
            "last_completed_phase": _fact("N0", "committed_manifest"),
            "db_revision": _fact(["afc2f36bb3ca"]),
            "db_at_code_head": _fact(True),
        },
        "monetization": {"commission_facts": {"count": commissions}},
    }
    (root / "reports/project_state_latest.json").write_text(json.dumps(report), encoding="utf-8")
    (root / "reports/threads_performance_diagnostic_latest.json").write_text(
        json.dumps({"generated_at": "2026-09-25T12:00:00+00:00", "publication_count": 5}),
        encoding="utf-8",
    )
    return root


def _sources(root: Path):
    return load_sources(root, doc_names=doc_paths())


# == discovery / scoring ===========================================================
def test_discovery_is_deterministic_and_ordered(tmp_path) -> None:
    sources = _sources(_repo(tmp_path))
    first, second = discover(sources), discover(sources)
    assert [c.as_dict() for c in first] == [c.as_dict() for c in second]
    keys = [(-c.score["total"], c.id) for c in first]
    assert keys == sorted(keys)
    for c in first:
        parts = {k: v for k, v in c.score.items() if k not in ("total", "duplication_penalty")}
        assert c.score["total"] == sum(parts.values()) - c.score["duplication_penalty"]
        assert all(0 <= v <= 3 for v in parts.values())
    assert 3 <= len(first) <= 8


def test_candidates_come_from_w1_w2_t7_records(tmp_path) -> None:
    candidates = {c.working_title: c for c in discover(_sources(_repo(tmp_path)))}
    titles = " ".join(candidates)
    for expected in ("アイキャッチ", "カテゴリ", "今の状態"):
        assert expected in titles
    recap = next(c for c in candidates.values() if "全体像" in c.working_title)
    assert (
        recap.evidence_found == recap.evidence_required and recap.content_type == "milestone_recap"
    )
    assert {e.source for e in recap.evidence} >= {
        "phase:W1",
        "phase:T7",
        "fact:wordpress_featured_images",
    }


def test_missing_evidence_warns_and_thin_topics_are_dropped(tmp_path) -> None:
    w2 = ("w2-parent-plus-child", "w2-article-1-parent-only", "w2-article-25-ai",
          "wordpress-api-user-least-privilege")  # fmt: skip
    root = _repo(tmp_path, drop_decisions=w2)
    candidates = discover(_sources(root))
    assert not any("カテゴリ" in c.working_title for c in candidates)  # 根拠 2/6 は候補にしない
    root2 = _repo(tmp_path / "b", drop_decisions=("w2-article-25-ai",))
    taxonomy = next(c for c in discover(_sources(root2)) if "カテゴリ" in c.working_title)
    assert any("w2-article-25-ai" in w for w in taxonomy.warnings)
    assert taxonomy.evidence_found == taxonomy.evidence_required - 1


def test_one_candidate_per_source_event(tmp_path) -> None:
    topics = (*catalog.TOPICS, dict(catalog.TOPICS[0]))  # 同じ出来事を 2 度並べても
    candidates = discover(_sources(_repo(tmp_path)), topics=topics)
    events = [e for c in candidates for e in c.source_event_ids]
    assert len(events) == len(set(events))


def test_used_source_events_lower_novelty(tmp_path) -> None:
    sources = _sources(_repo(tmp_path))
    base = {c.id: c.score["novelty"] for c in discover(sources)}
    used = discover(sources, used_source_events={"topic:overview-automation-so-far"})
    overview = next(c for c in used if c.source_event_ids == ["topic:overview-automation-so-far"])
    assert base[overview.id] == 3 and overview.score["novelty"] == 0


def test_the_decision_log_parser_reads_fields() -> None:
    text = (REPO / "docs/decision-log/2026-W39.md").read_text(encoding="utf-8")
    entries = parse_decision_log(text)
    entry = entries["w1-featured-images-complete"]
    assert entry["area"] == "wordpress/featured-images" and "25/25" in entry["resulting_state"]


# == safety ==========================================================================
def test_secrets_and_internal_values_are_redacted() -> None:
    raw = (
        "log at D:\\Logs\\affiliate-ai\\threads-worker.log by admin@example.test "
        "on DESKTOP-U4S0FG9 "
        "token THAA" + "x" * 40 + " Bearer abcdefghijklmnopqrstu "
        "https://bizfluxlab.com/wp-json/relay/review?session=abc&sig=zzz "
        "digest 7baa2fcf054ea3fe855138f4c45c4b0bec212d0820dc97b2f02d6f36cee10e2d "
        "postgresql://u:pw@db/x /Users/ishim/secret.txt"
    )
    out, found = safety.sanitize(raw)
    leaks = (
        "D:\\Logs",
        "admin@example.test",
        "DESKTOP-U4S0FG9",
        "THAAxxxx",
        "abcdefghijklmnopqrstu",
        "sig=zzz",
        "7baa2fcf054e",
        "u:pw@",
        "/Users/ishim",
    )
    for leaked in leaks:
        assert leaked not in out, leaked
    assert {"local path", "email", "host", "digest"} <= set(found)


def test_unsupported_revenue_claims_are_errors() -> None:
    claims = [Claim("この施策で収益が伸びた。", "observed_fact", (REF,))]
    errors, _ = safety.check_claims(claims, commissions_known=False)
    assert any("unsupported revenue claim" in e for e in errors)
    errors, _ = safety.check_claims(claims, commissions_known=True)
    assert not errors
    _, warnings = safety.check_claims(
        [Claim("画像のおかげでアクセスが増えた。", "observed_fact", (REF,))],
        commissions_known=False,
    )
    assert any("causal" in w for w in warnings)


def test_observed_facts_and_decisions_need_provenance() -> None:
    errors, _ = safety.check_claims(
        [Claim("25記事に画像を付けた。", "observed_fact"), Claim("こう決めた。", "decision"),
         Claim("たぶん効く。", "hypothesis")], commissions_known=False,
    )  # fmt: skip
    assert len(errors) == 2 and all(e.startswith("unsourced") for e in errors)
    with pytest.raises(ValueError):
        Claim("x", "rumour")


def test_wordpress_and_threads_copies_are_duplicates(tmp_path) -> None:
    article = (
        "CRMは導入後の乗り換えコストが大きいため、最初の比較で自社の使い方に合うかを見極めておきたいツールです。"
        * 3
    )
    post = (
        "手元の録音ファイル、会議中じゃなくても文字起こしAIに任せられる？ "
        "Krisp と Fireflies.ai は公式に記載。"
    )
    corpus = {"wordpress:article:4": article, "threads:publication:4": post}
    assert safety.duplication(article[:80], corpus)["closest_source"] == "wordpress:article:4"
    assert safety.duplication(article[:80], corpus)["verdict"] == "duplicate"
    assert safety.duplication(post, corpus)["verdict"] == "duplicate"
    fresh = "作業の段階を機械で読める形にして、次に何をするかを毎回同じ手順で確かめるようにした。"
    assert safety.duplication(fresh, corpus)["verdict"] == "distinct"
    sources = _sources(_repo(tmp_path))
    top = discover(sources)[0]
    copied = build_draft(top, topic_for(top), sources, now=NOW, corpus={"wordpress:article:1": ""})
    body_copy = {"wordpress:article:9": copied.body}
    again = build_draft(top, topic_for(top), sources, now=NOW, corpus=body_copy)
    assert any("duplicates wordpress:article:9" in e for e in again.errors)


# == drafts / statuses ===============================================================
def test_the_local_draft_is_evidence_backed_and_reader_facing(tmp_path) -> None:
    sources = _sources(_repo(tmp_path))
    top = discover(sources)[0]
    draft = build_draft(top, topic_for(top), sources, now=NOW)
    assert draft.status == "draft" and draft.human_review_required and draft.errors == []
    assert "0 件" in draft.body  # 手数料 0 件を正直に書く
    assert not safety.INTERNAL_WORDING.search(draft.body)  # 内部の言葉を本文に出さない
    assert any("kept out of the body" in w for w in draft.warnings)
    for claim in draft.claims:
        if claim.kind in ("observed_fact", "decision"):
            assert claim.evidence
    md = render_markdown(draft)
    assert "LOCAL DRAFT — NOT PUBLISHED" in md and "## 根拠" in md
    assert build_draft(top, topic_for(top), sources, now=NOW).content_hash == draft.content_hash


def test_zero_commissions_can_never_become_a_revenue_success(tmp_path) -> None:
    sources = _sources(_repo(tmp_path, commissions=0))
    top = discover(sources)[0]
    draft = build_draft(top, topic_for(top), sources, now=NOW)
    draft.sections.append({"heading": "結果", "role": "x", "paragraphs": ["収益が伸びた。"]})
    errors, _ = safety.check_body(draft.body, commissions_known=False)
    assert errors == ["unsupported revenue claim in body"]


def test_status_transitions_need_approval_and_publication_evidence() -> None:
    draft = NoteDraft(id="d", candidate_id="c", content_type="build_log", working_title="t",
                      premise="p", audience="a", summary="s",
                      sections=[{"heading": "h", "role": "r", "paragraphs": ["x"]}], claims=[],
                      phases=[], source_event_ids=[], wordpress_refs=[], threads_refs=[],
                      created_at="now")  # fmt: skip
    with pytest.raises(NoteStatusError):
        transition(
            draft, "published", publication={"url": "https://note.com/x", "observed_at": "t"}
        )
    transition(draft, "review_ready")
    with pytest.raises(NoteStatusError):
        transition(draft, "approved", approval={"approved_by": "h", "approved_at": "t"})
    with pytest.raises(NoteStatusError):
        transition(draft, "approved", approval={"approved_by": "h", "approved_at": "t",
                                                "content_hash": "other"})  # fmt: skip
    transition(draft, "approved", approval={"approved_by": "h", "approved_at": "t",
                                            "content_hash": draft.content_hash})  # fmt: skip
    with pytest.raises(NoteStatusError):
        transition(draft, "published")  # 公開の証拠が無い
    with pytest.raises(NoteStatusError):
        transition(draft, "published", publication={"url": "", "observed_at": "t"})
    errored = NoteDraft(**{**draft.__dict__, "status": "draft", "errors": ["x"]})
    with pytest.raises(NoteStatusError):
        transition(errored, "review_ready")


# == no external effects ===========================================================
def test_discovery_and_drafting_never_use_the_network(tmp_path, monkeypatch) -> None:
    def refuse(*args, **kwargs):
        raise AssertionError("note N0 must not open network connections")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    from scripts.propose_note_content import main

    root = _repo(tmp_path)
    assert main(["--draft", "top", "--no-db"], root=root, now=NOW) == 0
    drafts = list((root / "reports/note/drafts").glob("*.json"))
    assert len(drafts) == 1 and json.loads(drafts[0].read_text("utf-8"))["status"] == "draft"
    assert (root / "reports/note/candidates_latest.json").exists()


def test_the_plan_mode_writes_nothing(tmp_path) -> None:
    from scripts.propose_note_content import main

    root = _repo(tmp_path)
    assert main([], root=root, now=NOW) == 0
    assert not (root / "reports/note").exists()


def test_the_note_package_has_no_publishing_or_production_paths() -> None:
    forbidden = ("requests", "httpx", "urllib.request", "playwright", "selenium", "webbrowser",
                 "app.wordpress", "app.social.threads", "schtasks", "Register-ScheduledTask",
                 "subprocess", "threads-generation", ".commit()", "insert into",
                 "update ")  # fmt: skip
    for path in [*NOTE_SRC.glob("*.py"), REPO / "scripts/propose_note_content.py"]:
        text = path.read_text(encoding="utf-8")
        for word in forbidden:
            assert word not in text, f"{path.name} contains {word!r}"


# == project state =================================================================
def test_project_state_reports_the_note_channel(tmp_path) -> None:
    root = _repo(tmp_path)
    phases = json.loads((root / "docs/project-roadmap.json").read_text("utf-8"))["phases"]
    empty = collect_note_channel(root, phases)
    assert (empty["candidates"], empty["drafts"], empty["published_with_evidence"]) == (0, {}, 0)
    assert empty["phases"]["N0"] == "complete" and empty["phases"]["N1"] == "planned"
    from scripts.propose_note_content import main

    main(["--draft", "top", "--no-db"], root=root, now=NOW)
    state = collect_note_channel(root, phases)
    assert state["candidates"] >= 3 and state["drafts"] == {"draft": 1}
    assert state["published_with_evidence"] == 0 and state["pending_human_review"] == 0
    draft_path = next((root / "reports/note/drafts").glob("*.json"))
    data = json.loads(draft_path.read_text("utf-8"))
    data["status"] = "published"  # 証拠の無い「公開済み」は数えない
    draft_path.write_text(json.dumps(data), encoding="utf-8")
    assert collect_note_channel(root, phases)["published_with_evidence"] == 0


def test_the_generator_includes_the_note_channel_without_warnings(tmp_path) -> None:
    from app.project_state.generator import build_report, render_markdown
    from tests.unit.test_project_state import _context

    ctx, _, _ = _context(tmp_path)
    report = build_report(ctx)
    note = report["project"]["note_channel"]
    assert note["phases"]["N0"] == "complete" and note["published_with_evidence"] == 0
    assert not any("note" in w["id"] for w in report["warnings"])
    assert "note channel (local)" in render_markdown(report)
    assert report["project"]["next_phase"] == "N1"
