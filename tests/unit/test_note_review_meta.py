"""N1: 誰が直したか・推奨のタグ・サムネイルの指示・公開済みの note との重複の検査。

pin する契約:

- Claude の手直しは ``claude`` と記録する (人が直したことにしない)。
- タグとサムネイルの指示は承認に入り、承認の後は変えられない。内部の値は拒む。
- 人がすでに公開した note の本文 (仕組みの外で公開したものを含む) は重複の検査の相手になる。
"""

from __future__ import annotations

import pytest

from app.social.note import review
from app.social.note.models import NoteStatusError
from tests.unit.test_note_channel import NOW, _repo
from tests.unit.test_note_review import EDITED, _draft


def test_a_claude_edit_is_recorded_as_claude_not_human(tmp_path) -> None:
    _root, _path, draft = _draft(tmp_path)
    review.apply_edit(draft, EDITED, commissions_known=False, editor="claude")
    assert draft.edited_by == "claude" and draft.edited_by_human is False
    assert any("edited by claude" in w for w in draft.warnings)
    with pytest.raises(ValueError, match="editor"):
        review.apply_edit(draft, EDITED, commissions_known=False, editor="someone")


def test_tags_and_thumbnail_brief_are_part_of_the_approval(tmp_path) -> None:
    _root, _path, draft = _draft(tmp_path)
    review.apply_edit(draft, EDITED, commissions_known=False, editor="claude")
    review.set_meta(draft, tags=["#AI", "自動化", ""], thumbnail_brief="白地に図が1つ")
    assert draft.tags == ["AI", "自動化"]
    with pytest.raises(ValueError, match="internal values"):
        review.set_meta(draft, thumbnail_brief=r"C:\Users\me\x.png を使う")
    with pytest.raises(ValueError, match="10 tags"):
        review.set_meta(draft, tags=["a b"])
    packet = review.review_packet(draft)
    assert packet["tags"] == ["AI", "自動化"] and packet["headings"] == ["前提", "学び"]
    assert "#AI #自動化" in review.render_packet(draft)
    review.submit(draft)
    image = tmp_path / "thumb.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fixture")  # テストの画像 (先頭の印だけ本物)
    review.approve(draft, content_hash=draft.content_hash, approved_by="human", now=NOW,
                   image=image, snapshot_root=tmp_path)
    assert draft.approval["tags"] == ["AI", "自動化"] and draft.approval["edited_by"] == "claude"
    with pytest.raises(NoteStatusError):
        review.set_meta(draft, tags=["x"])  # 承認の後は変えない


def test_published_note_text_is_in_the_duplication_corpus(tmp_path) -> None:
    from scripts.propose_note_content import internal_corpus

    root = _repo(tmp_path)
    ext = root / "reports/note/external"
    ext.mkdir(parents=True)
    (ext / "n1.txt").write_text("公開済みの本文", encoding="utf-8")
    assert internal_corpus(root)["note-external:n1"] == "公開済みの本文"


def test_decision_log_evidence_keeps_the_content_hash(tmp_path) -> None:
    from app.social.note.sources import load_sources

    root, _path, draft = _draft(tmp_path)
    decisions = load_sources(root).decisions
    before = draft.content_hash
    review.add_evidence(draft, text="実機で確かめた。", kind="observed_fact",
                        decision_ids=["t6-1-mobile-render-observed"], decisions=decisions)
    assert draft.content_hash == before
    assert draft.claims[-1].evidence[0].source == "decision:t6-1-mobile-render-observed"
    with pytest.raises(ValueError, match="not in the decision log"):
        review.add_evidence(draft, text="x", kind="observed_fact", decision_ids=["nope"],
                            decisions=decisions)
    review.apply_edit(draft, EDITED, commissions_known=False, editor="claude")
    review.submit(draft)
    review.approve(draft, content_hash=draft.content_hash, approved_by="human", now=NOW)
    with pytest.raises(NoteStatusError):
        review.add_evidence(draft, text="x", kind="observed_fact",
                            decision_ids=["t6-1-mobile-render-observed"], decisions=decisions)


def test_doc_evidence_claims_come_only_from_the_topics_own_sentence(tmp_path) -> None:
    from datetime import UTC, datetime

    from app.social.note.candidates import discover, doc_paths, topic_for
    from app.social.note.renderer import build_draft
    from app.social.note.sources import load_sources

    sources = load_sources(_repo(tmp_path), doc_names=doc_paths())
    by_title = {c.working_title: c for c in discover(sources)}
    alerts = next(c for t, c in by_title.items() if "行動が要るときだけ" in t)
    draft = build_draft(alerts, topic_for(alerts), sources, now=datetime.now(UTC))
    assert not any("子のプロセス" in c.text for c in draft.claims)  # 別の話題の文を付けない
    worker = next(c for t, c in by_title.items() if "止まっていなかった" in t)
    draft = build_draft(worker, topic_for(worker), sources, now=datetime.now(UTC))
    assert any("子のプロセス" in c.text for c in draft.claims)
