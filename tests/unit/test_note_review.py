"""N1: note の 1 本を人と仕上げ、承認し、公開の証拠を記録する (ローカルだけ)。

pin する契約:

- 人が直した本文を取り込むと、承認は消え、検査をやり直す (秘密は誤り)。
- 承認は review_ready の本文の hash に結びつく。外部リンクは明示の承認が要る。
- 公開の記録は、承認した hash と同じ本文を人が公開したときだけ。URL は https で方針の host。
- 有料は記録するだけ。承認の後に公開の形は変えられない。
- CLI は reports/note/ だけに書き、note・WordPress・Threads・DB に触れない。
- C10 の話題が候補に出る。
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app.social.note import review
from app.social.note.models import NoteStatusError
from tests.unit.test_note_channel import NOW, _repo

POLICY = {"publication_url_hosts": ["note.com"]}


def _draft(tmp_path):
    from scripts.propose_note_content import main

    root = _repo(tmp_path)
    assert main(["--draft", "top", "--no-db"], root=root, now=NOW) == 0
    path = next((root / "reports/note/drafts").glob("*.json"))
    return root, path, review.draft_from_dict(json.loads(path.read_text("utf-8")))


def test_round_trip_keeps_the_hash(tmp_path) -> None:
    _root, path, draft = _draft(tmp_path)
    assert draft.content_hash == json.loads(path.read_text("utf-8"))["content_hash"]
    assert draft.access_mode == "free" and draft.claims


EDITED = """<!-- local -->
# 人が直した題名

## 前提

人が書き直した最初の段落。

## 学び

小さく始めて、人が確かめる。

---

## 根拠 (レビュー用。公開の前に消す)

- ignored
"""


def test_a_human_edit_resets_approval_and_rechecks(tmp_path) -> None:
    _root, _path, draft = _draft(tmp_path)
    before = draft.content_hash
    review.apply_edit(draft, EDITED, commissions_known=False)
    assert draft.working_title == "人が直した題名" and len(draft.sections) == 2
    assert draft.content_hash != before and draft.status == "draft" and draft.approval is None
    assert any("edited by human" in w for w in draft.warnings)
    assert draft.edited_by == "human" and draft.edited_by_human
    leaky = EDITED.replace("小さく始めて", r"C:\Users\me\secret.txt を見て")
    review.apply_edit(draft, leaky, commissions_known=False)
    assert draft.errors  # ローカルのパスは誤り
    with pytest.raises(NoteStatusError):
        review.submit(draft)
    with pytest.raises(ValueError, match="title"):
        review.parse_markdown("## only a heading\n\ntext\n")


def test_approval_is_hash_bound_and_links_need_approval(tmp_path) -> None:
    _root, _path, draft = _draft(tmp_path)
    review.apply_edit(draft, EDITED.replace("小さく始めて", "https://example.com/x を参考に"),
                      commissions_known=False)
    review.submit(draft)
    with pytest.raises(NoteStatusError, match="different content hash"):
        review.approve(draft, content_hash="0" * 64, approved_by="human", now=NOW,
                       links_approved=True)
    with pytest.raises(NoteStatusError, match="link"):
        review.approve(draft, content_hash=draft.content_hash, approved_by="human", now=NOW)
    review.approve(draft, content_hash=draft.content_hash, approved_by="human", now=NOW,
                   links_approved=True)
    assert draft.status == "approved" and draft.approval["links"] == ["https://example.com/x"]
    with pytest.raises(NoteStatusError):
        review.set_access_mode(draft, "paid")  # 承認の後に形を変えない


def _approved(tmp_path, mode="free"):
    root, path, draft = _draft(tmp_path)
    review.apply_edit(draft, EDITED, commissions_known=False)
    review.set_access_mode(draft, mode)
    review.submit(draft)
    review.approve(draft, content_hash=draft.content_hash, approved_by="human", now=NOW)
    return root, draft


def test_publication_needs_the_approved_hash_and_an_allowed_https_url(tmp_path) -> None:
    _root, draft = _approved(tmp_path, mode="paid")
    seen = NOW + timedelta(hours=1)
    for url, match in (("http://note.com/u/n/abc", "https"),
                       ("https://evil.example/n/abc", "not in publication_url_hosts"),
                       ("https://user:pw@note.com/u/n/abc", "credentials"),
                       ("https://note.com/", "point to a piece")):
        with pytest.raises(NoteStatusError, match=match):
            review.record_publication(draft, url=url, observed_at=seen,
                                      published_hash=draft.content_hash, now=seen, policy=POLICY)
    with pytest.raises(NoteStatusError, match="hash mismatch"):
        review.record_publication(draft, url="https://note.com/u/n/abc", observed_at=seen,
                                  published_hash="f" * 64, now=seen, policy=POLICY)
    review.record_publication(draft, url="https://note.com/u/n/abc", observed_at=seen,
                              published_hash=draft.content_hash, now=seen, policy=POLICY)
    assert draft.status == "published"
    assert draft.publication["access_mode"] == "paid"  # 記録するだけ (値段・販売は人が note で)
    assert draft.publication["content_hash"] == draft.approval["content_hash"]


def test_the_cli_runs_the_whole_local_flow(tmp_path, capsys) -> None:
    from scripts.manage_note_piece import main

    root, path, draft = _draft(tmp_path)
    did = draft.id
    edited = tmp_path / "edited.md"
    edited.write_text(EDITED, encoding="utf-8")
    assert main(["packet", did], root=root, now=NOW) == 0
    assert path.with_suffix(".packet.md").exists() and path.with_suffix(".txt").exists()
    assert main(["edit", did, "--from", str(edited), "--no-db"], root=root, now=NOW) == 0
    assert main(["approve", did, "--content-hash", "x", "--by", "h"], root=root, now=NOW) == 2
    assert main(["submit", did], root=root, now=NOW) == 0
    current = json.loads(path.read_text("utf-8"))["content_hash"]
    assert main(["approve", did, "--content-hash", current, "--by", "human"],
                root=root, now=NOW) == 0
    assert main(["record-publication", did, "--url", "https://note.com/u/n/n1",
                 "--observed-at", "2026-09-26T10:00:00", "--content-hash", current],
                root=root, now=NOW) == 2  # タイムゾーンが要る
    assert main(["record-publication", did, "--url", "https://note.com/u/n/n1",
                 "--observed-at", "2026-09-26T19:00:00+09:00", "--content-hash", current],
                root=root, now=NOW) == 0
    data = json.loads(path.read_text("utf-8"))
    assert data["status"] == "published" and data["publication"]["url"].endswith("/n1")
    from app.project_state.note_state import collect_note_channel

    phases = json.loads((root / "docs/project-roadmap.json").read_text("utf-8"))["phases"]
    assert collect_note_channel(root, phases)["published_with_evidence"] == 1
    assert "nothing was sent to note" in capsys.readouterr().out


def test_c10_topics_become_candidates(tmp_path) -> None:
    from app.social.note.candidates import discover, doc_paths
    from app.social.note.sources import load_sources

    sources = load_sources(_repo(tmp_path), doc_names=doc_paths())
    titles = [c.working_title for c in discover(sources)]
    assert any("承認だけでは記事を書き換えない" in t for t in titles)
    assert any("行動が要るときだけ" in t for t in titles)


def test_the_cli_has_no_publishing_paths() -> None:
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "scripts/manage_note_piece.py").read_text(
        encoding="utf-8")
    for word in ("requests", "httpx", "urllib.request", "playwright", "selenium",
                 "app.wordpress", "app.social.threads", "subprocess", ".commit()"):
        assert word not in text, word
