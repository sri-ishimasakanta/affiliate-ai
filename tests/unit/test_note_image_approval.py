"""N1: サムネイルの画像を人の承認に拘束する (``note-approval/2``)。

**このテストの画像は、先頭の印 (PNG / JPEG) だけが本物の fixture。** 本物の画像ではない。

pin する契約:

- 画像つきの承認は、名前・大きさ・sha256・中身から分かる形式を記録する (パスは記録しない)。
- サムネイルの指示がある下書きは、画像のファイル無しでは承認できない (真偽値だけの承認は無い)。
- 名前が同じでも中身が変われば sha256 が変わり、前の承認の画像とは別物になる。公開の記録は
  承認の画像と同じファイルでないと拒む。取り消し (reopen) で前の承認は履歴に残る。
- 無い・空・形式が分からないファイルは拒む。承認は JSON で往復できる。
- 古い承認 (画像の情報が無い) はそのまま動き、画像を補わない。
- 公開の記録は承認した画像の指紋を引き継ぎ、note の画像との一致は人の責任と書く。
- 本文の content_hash の意味は変わらない (画像は hash に入らない)。
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta

import pytest

from app.social.note import review
from app.social.note.models import NoteStatusError
from tests.unit.test_note_channel import NOW
from tests.unit.test_note_review import EDITED, _draft

POLICY = {"publication_url_hosts": ["note.com"]}
PNG = b"\x89PNG\r\n\x1a\n" + b"fixture-image-v1"
JPEG = b"\xff\xd8\xff" + b"fixture-image-v2"


def _ready(tmp_path, *, brief="3つの箱の図解"):
    root, path, draft = _draft(tmp_path)
    review.apply_edit(draft, EDITED, commissions_known=False, editor="claude", now=NOW)
    if brief:
        review.set_meta(draft, thumbnail_brief=brief)
    review.submit(draft)
    return root, path, draft


def _image(tmp_path, data=PNG, name="thumb.png"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_approval_binds_the_image_file(tmp_path) -> None:
    _root, _path, draft = _ready(tmp_path)
    image = _image(tmp_path)
    before = draft.content_hash
    review.approve(draft, content_hash=draft.content_hash, approved_by="human", now=NOW,
                   image=image)
    record = draft.approval["images"][0]
    assert record == {"filename": "thumb.png", "bytes": len(PNG),
                      "sha256": hashlib.sha256(PNG).hexdigest(), "mime_type": "image/png"}
    assert draft.approval["approval_schema"] == review.APPROVAL_SCHEMA
    assert draft.approval["images_approved"] is True and draft.approval["image_required"]
    assert str(tmp_path) not in json.dumps(draft.approval)  # パスは記録しない
    assert draft.content_hash == before  # 画像は本文の hash に入らない


def test_a_thumbnail_draft_cannot_be_approved_without_the_image(tmp_path) -> None:
    _root, _path, draft = _ready(tmp_path)
    with pytest.raises(NoteStatusError, match="--image"):
        review.approve(draft, content_hash=draft.content_hash, approved_by="human", now=NOW)
    assert draft.status == "review_ready" and draft.approval is None


def test_a_draft_without_thumbnail_can_still_be_approved_without_an_image(tmp_path) -> None:
    _root, _path, draft = _ready(tmp_path, brief=None)
    review.approve(draft, content_hash=draft.content_hash, approved_by="human", now=NOW)
    assert draft.approval["images"] == [] and draft.approval["images_approved"] is False


@pytest.mark.parametrize("data, name, match", [
    (None, "missing.png", "missing"),
    (b"", "empty.png", "empty"),
    (b"not an image at all", "fake.png", "unsupported image type"),
])
def test_bad_image_files_fail_closed(tmp_path, data, name, match) -> None:
    _root, _path, draft = _ready(tmp_path)
    path = tmp_path / name if data is None else _image(tmp_path, data, name)
    with pytest.raises(NoteStatusError, match=match):
        review.approve(draft, content_hash=draft.content_hash, approved_by="human", now=NOW,
                       image=path)
    assert draft.approval is None


def test_same_filename_new_content_is_a_different_image(tmp_path) -> None:
    _root, _path, draft = _ready(tmp_path)
    image = _image(tmp_path)
    review.approve(draft, content_hash=draft.content_hash, approved_by="human", now=NOW,
                   image=image)
    assert review.check_image(draft, image)["state"] == "matches_approval"
    image.write_bytes(JPEG)  # 同じ名前で中身を差し替える
    state = review.check_image(draft, image)
    assert state["state"] == "changed_since_approval"
    assert state["current"]["sha256"] != draft.approval["images"][0]["sha256"]
    with pytest.raises(NoteStatusError, match="not the approved one"):
        review.record_publication(draft, url="https://note.com/u/n/p2", observed_at=NOW,
                                  published_hash=draft.content_hash, now=NOW, policy=POLICY,
                                  image=image)
    assert draft.status == "approved"  # 記録しない
    # 取り消して新しい画像で承認し直す。前の承認は履歴に残る
    review.reopen(draft, reason="the thumbnail was replaced", now=NOW + timedelta(hours=1))
    old = draft.approval_history[-1]
    assert old["images"][0]["mime_type"] == "image/png"
    assert old["superseded_reason"] == "the thumbnail was replaced"
    review.submit(draft)
    review.approve(draft, content_hash=draft.content_hash, approved_by="human", now=NOW,
                   image=image)
    assert draft.approval["images"][0]["mime_type"] == "image/jpeg"


def test_publication_carries_the_approved_image_provenance(tmp_path) -> None:
    _root, _path, draft = _ready(tmp_path)
    image = _image(tmp_path)
    review.approve(draft, content_hash=draft.content_hash, approved_by="human", now=NOW,
                   image=image)
    with pytest.raises(NoteStatusError, match="--image"):
        review.record_publication(draft, url="https://note.com/u/n/p2", observed_at=NOW,
                                  published_hash=draft.content_hash, now=NOW, policy=POLICY)
    renamed = _image(tmp_path, PNG, "renamed.png")  # 名前が違っても中身が同じなら同じ画像
    review.record_publication(draft, url="https://note.com/u/n/p2", observed_at=NOW,
                              published_hash=draft.content_hash, now=NOW, policy=POLICY,
                              image=renamed)
    pub = draft.publication
    assert pub["approved_images"][0]["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert "human responsibility" in pub["note_image_match"]


def test_the_approval_round_trips_through_json(tmp_path) -> None:
    _root, path, draft = _ready(tmp_path)
    review.approve(draft, content_hash=draft.content_hash, approved_by="human", now=NOW,
                   image=_image(tmp_path))
    again = review.draft_from_dict(json.loads(json.dumps(draft.as_dict(), ensure_ascii=False)))
    assert again.approval == draft.approval and again.approval_history == []
    assert review.approved_images(again) == draft.approval["images"]


def test_old_approvals_without_image_data_still_work_and_are_not_backfilled(tmp_path) -> None:
    _root, _path, draft = _ready(tmp_path, brief=None)
    data = draft.as_dict()
    data["status"] = "approved"
    data["approval"] = {"approved_by": "human", "approved_at": NOW.isoformat(),
                        "content_hash": draft.content_hash, "access_mode": "free",
                        "links": [], "images_approved": False, "images": []}  # 古い形 (v1)
    data.pop("approval_history", None)
    old = review.draft_from_dict(data)
    assert old.approval_history == [] and review.approved_images(old) == []
    review.record_publication(old, url="https://note.com/u/n/p3", observed_at=NOW,
                              published_hash=old.content_hash, now=NOW, policy=POLICY)
    assert old.publication["approved_images"] == []
    assert "approval_schema" not in old.approval  # 過去の承認を書き換えない


def test_editing_after_approval_keeps_the_old_approval_in_history(tmp_path) -> None:
    _root, _path, draft = _ready(tmp_path)
    review.approve(draft, content_hash=draft.content_hash, approved_by="human", now=NOW,
                   image=_image(tmp_path))
    approved_hash = draft.approval["content_hash"]
    review.apply_edit(draft, EDITED.replace("小さく始めて", "少しずつ"),
                      commissions_known=False, editor="human", now=NOW)
    assert draft.approval is None and draft.status == "draft"
    assert draft.approval_history[-1]["content_hash"] == approved_hash
    assert draft.approval_history[-1]["superseded_reason"] == "the body was edited"


def test_the_packet_shows_basename_bytes_and_hash_only(tmp_path) -> None:
    _root, _path, draft = _ready(tmp_path)
    text = review.render_packet(draft)
    assert "image: **required**" in text and "human checks" in text
    review.approve(draft, content_hash=draft.content_hash, approved_by="human", now=NOW,
                   image=_image(tmp_path))
    text = review.render_packet(draft)
    assert "approved image: thumb.png" in text and hashlib.sha256(PNG).hexdigest() in text
    assert str(tmp_path) not in text


def test_the_cli_binds_the_image(tmp_path, capsys) -> None:
    from scripts.manage_note_piece import main

    root, path, draft = _ready(tmp_path)
    path.write_text(json.dumps(draft.as_dict(), ensure_ascii=False), encoding="utf-8")
    image = _image(tmp_path)
    base = ["approve", draft.id, "--content-hash", draft.content_hash, "--by", "human"]
    assert main(base, root=root, now=NOW) == 2  # 画像なし
    assert "--image" in capsys.readouterr().out
    assert main([*base, "--image", str(image)], root=root, now=NOW) == 0
    capsys.readouterr()
    assert main(["check-image", draft.id, "--image", str(image)], root=root, now=NOW) == 0
    assert "matches_approval" in capsys.readouterr().out
    saved = json.loads(path.read_text("utf-8"))
    assert saved["approval"]["images"][0]["filename"] == "thumb.png"


def test_the_image_record_round_trips_through_the_ledger(session, tmp_path) -> None:
    from sqlalchemy import select

    from app.models import NotePiece
    from app.services.note_ledger_service import NoteLedgerService

    root, path, draft = _ready(tmp_path)
    review.approve(draft, content_hash=draft.content_hash, approved_by="human", now=NOW,
                   image=_image(tmp_path))
    path.write_text(json.dumps(draft.as_dict(), ensure_ascii=False), encoding="utf-8")
    NoteLedgerService(session, policy=POLICY).sync_from_drafts(root, execute=True, now=NOW)
    row = session.scalars(select(NotePiece).where(NotePiece.draft_id == draft.id)).one()
    assert row.approval_json["images"] == draft.approval["images"]
    assert row.approved_hash == draft.content_hash
