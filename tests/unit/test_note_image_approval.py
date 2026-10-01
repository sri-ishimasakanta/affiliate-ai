"""N1: サムネイルの画像を人の承認に拘束する (``note-approval/3``、2026-10-01 に写しを追加)。

**このテストの画像は、先頭の印 (PNG / JPEG) だけが本物の fixture。** 本物の画像ではない。

pin する契約:

- 画像つきの承認は、名前・大きさ・sha256・中身から分かる形式を記録する (絶対パスは記録しない)。
- -3: 承認の時に画像を中身の sha256 の名前で写し (``reports/note/approved-images/<下書き>/``)、
  写しを読み直して確かめてから承認する。写しが作れなければ承認しない。写しは上書きしない。
- 承認の後の正本は写し。元のファイルが後で上書きされても承認は効いたまま、公開の記録もできる。
  公開に別の画像を使うなら、取り消し (reopen) → submit → 承認し直す。前の写しは消さない。
- サムネイルの指示がある下書きは、画像のファイル無しでは承認できない (真偽値だけの承認は無い)。
- 無い・空・形式が分からないファイルは拒む。承認は JSON で往復できる。
- 古い承認 (画像の情報が無い・写しが無い) はそのまま動き、画像も写しも補わない (legacy)。
- 公開の記録は承認した画像の指紋と写しを引き継ぎ、note の画像との一致は人の責任と書く。
- 本文の content_hash の意味は変わらない (画像は hash に入らない)。
"""

from __future__ import annotations

import hashlib
import json
import stat
from datetime import timedelta
from pathlib import Path

import pytest

from app.social.note import review
from app.social.note.models import NoteStatusError
from tests.unit.test_note_channel import NOW
from tests.unit.test_note_review import EDITED, _draft

POLICY = {"publication_url_hosts": ["note.com"]}
PNG = b"\x89PNG\r\n\x1a\n" + b"fixture-image-v1"
JPEG = b"\xff\xd8\xff" + b"fixture-image-v2"
ROOT = Path(__file__).resolve().parents[2]


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


def _approve(draft, root, image, **kw):
    return review.approve(draft, content_hash=draft.content_hash, approved_by="human",
                          now=kw.pop("now", NOW), image=image, snapshot_root=root, **kw)


def _publish(draft, root, image=None, url="https://note.com/u/n/p2"):
    return review.record_publication(draft, url=url, observed_at=NOW,
                                     published_hash=draft.content_hash, now=NOW, policy=POLICY,
                                     image=image, snapshot_root=root)  # fmt: skip


def _snapshot_file(root, draft) -> Path:
    return root / draft.approval["images"][0]["snapshot"]["path"]


# -- 承認と写し ------------------------------------------------------------------------------------
def test_approval_binds_the_image_file_and_makes_a_snapshot(tmp_path) -> None:
    root, _path, draft = _ready(tmp_path)
    image = _image(tmp_path)
    before = draft.content_hash
    _approve(draft, root, image)
    record = draft.approval["images"][0]
    sha = hashlib.sha256(PNG).hexdigest()
    assert {k: record[k] for k in ("filename", "bytes", "sha256", "mime_type")} == {
        "filename": "thumb.png", "bytes": len(PNG), "sha256": sha, "mime_type": "image/png"}
    snap = record["snapshot"]
    assert snap["path"] == f"reports/note/approved-images/{draft.id}/{sha}.png"
    assert (snap["sha256"], snap["bytes"], snap["mime_type"]) == (sha, len(PNG), "image/png")
    assert snap["reused"] is False and record["canonical"] == "approval_snapshot"
    copy = _snapshot_file(root, draft)
    assert copy.read_bytes() == PNG  # 写しの中身 = 承認した画像
    assert not copy.stat().st_mode & stat.S_IWRITE  # 読むだけ
    assert record["source"]["filename"] == "thumb.png"  # 出どころは名前だけ (root の外)
    assert draft.approval["approval_schema"] == review.APPROVAL_SCHEMA == "note-approval/3"
    assert draft.approval["images_approved"] is True and draft.approval["image_required"]
    assert str(tmp_path) not in json.dumps(draft.approval)  # 絶対パスは記録しない
    assert draft.content_hash == before  # 画像は本文の hash に入らない


def test_the_source_path_inside_the_repository_is_kept_as_provenance(tmp_path) -> None:
    root, _path, draft = _ready(tmp_path)
    (root / "artifacts" / "note").mkdir(parents=True)
    image = _image(root / "artifacts" / "note")
    _approve(draft, root, image)
    source = draft.approval["images"][0]["source"]
    assert source["path"] == "artifacts/note/thumb.png" and "not canonical" in source["role"]


def test_a_thumbnail_draft_cannot_be_approved_without_the_image(tmp_path) -> None:
    _root, _path, draft = _ready(tmp_path)
    with pytest.raises(NoteStatusError, match="--image"):
        review.approve(draft, content_hash=draft.content_hash, approved_by="human", now=NOW)
    assert draft.status == "review_ready" and draft.approval is None


def test_an_image_approval_needs_the_snapshot_store(tmp_path) -> None:
    _root, _path, draft = _ready(tmp_path)
    with pytest.raises(NoteStatusError, match="snapshot store"):
        review.approve(draft, content_hash=draft.content_hash, approved_by="human", now=NOW,
                       image=_image(tmp_path))
    assert draft.status == "review_ready" and draft.approval is None


def test_a_draft_without_thumbnail_can_still_be_approved_without_an_image(tmp_path) -> None:
    root, _path, draft = _ready(tmp_path, brief=None)
    review.approve(draft, content_hash=draft.content_hash, approved_by="human", now=NOW)
    assert draft.approval["images"] == [] and draft.approval["images_approved"] is False
    assert not (root / review.SNAPSHOT_DIR).exists()


@pytest.mark.parametrize("data, name, match", [
    (None, "missing.png", "missing"),
    (b"", "empty.png", "empty"),
    (b"not an image at all", "fake.png", "unsupported image type"),
])
def test_bad_image_files_fail_closed(tmp_path, data, name, match) -> None:
    root, _path, draft = _ready(tmp_path)
    path = tmp_path / name if data is None else _image(tmp_path, data, name)
    with pytest.raises(NoteStatusError, match=match):
        _approve(draft, root, path)
    assert draft.approval is None and not (root / review.SNAPSHOT_DIR).exists()


def test_a_failed_snapshot_means_no_approval(tmp_path) -> None:
    root, _path, draft = _ready(tmp_path)
    store = root / review.SNAPSHOT_DIR
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("not a directory", encoding="utf-8")  # 写しの置き場が作れない
    with pytest.raises(NoteStatusError, match="nothing was approved"):
        _approve(draft, root, _image(tmp_path))
    assert draft.status == "review_ready" and draft.approval is None


def test_an_existing_snapshot_is_never_overwritten(tmp_path) -> None:
    root, _path, draft = _ready(tmp_path)
    sha = hashlib.sha256(PNG).hexdigest()
    target = root / review.SNAPSHOT_DIR / draft.id / f"{sha}.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(JPEG)  # 名前の hash と中身が違う (壊れた写し)
    with pytest.raises(NoteStatusError, match="not overwritten"):
        _approve(draft, root, _image(tmp_path))
    assert target.read_bytes() == JPEG and draft.approval is None


def test_an_unsafe_draft_id_is_refused_for_the_snapshot(tmp_path) -> None:
    with pytest.raises(NoteStatusError, match="unsafe draft id"):
        review.snapshot_image(_image(tmp_path), root=tmp_path, draft_id="../escape")


# -- 元のファイルが後で変わる ------------------------------------------------------------------
def test_overwriting_the_source_after_approval_keeps_the_approval(tmp_path) -> None:
    root, _path, draft = _ready(tmp_path)
    image = _image(tmp_path)
    _approve(draft, root, image)
    image.write_bytes(JPEG)  # 同じ名前で中身を差し替える (3 本目の事故の形)
    state = review.check_image(draft, image, root=root)
    assert state["state"] == "changed_since_approval" and state["approval_valid"] is True
    assert state["snapshots"][0]["state"] == "ok" and "still valid" in state["note"]
    assert draft.status == "approved" and draft.approval["images"][0]["mime_type"] == "image/png"
    # 差し替えた画像を note に使ったと人が示すなら記録しない (取り消して承認し直す)
    with pytest.raises(NoteStatusError, match="not the approved one"):
        _publish(draft, root, image)
    assert draft.status == "approved"
    # 承認した画像 (写し) を使ったなら、元のファイルが変わっていても記録できる
    _publish(draft, root)
    pub = draft.publication
    sha = hashlib.sha256(PNG).hexdigest()
    assert pub["approved_images"][0]["sha256"] == sha
    assert pub["approved_images"][0]["canonical"] == "approval_snapshot"
    assert pub["approved_images"][0]["snapshot"].endswith(f"{sha}.png")
    assert pub["image_confirmation"]["checked"] is False
    assert "human responsibility" in pub["note_image_match"]


def test_publication_with_the_used_file_confirms_the_hash(tmp_path) -> None:
    root, _path, draft = _ready(tmp_path)
    _approve(draft, root, _image(tmp_path))
    renamed = _image(tmp_path, PNG, "renamed.png")  # 名前が違っても中身が同じなら同じ画像
    _publish(draft, root, renamed)
    confirmation = draft.publication["image_confirmation"]
    assert confirmation["checked"] is True and confirmation["matches_approval"] is True


@pytest.mark.parametrize("damage", ["missing", "changed", "outside"])
def test_a_damaged_snapshot_blocks_the_publication_record(tmp_path, damage) -> None:
    root, _path, draft = _ready(tmp_path)
    _approve(draft, root, _image(tmp_path))
    copy = _snapshot_file(root, draft)
    copy.chmod(stat.S_IREAD | stat.S_IWRITE)
    if damage == "missing":
        copy.unlink()
    elif damage == "changed":
        copy.write_bytes(JPEG)
    else:
        draft.approval["images"][0]["snapshot"]["path"] = "../elsewhere.png"
    expected = {"missing": "missing", "changed": "changed", "outside": "invalid"}[damage]
    with pytest.raises(NoteStatusError, match=expected):
        _publish(draft, root)
    assert draft.status == "approved"


# -- 取り消しと承認し直し ----------------------------------------------------------------------
def test_a_different_image_needs_reopen_and_reapproval(tmp_path) -> None:
    root, _path, draft = _ready(tmp_path)
    image = _image(tmp_path)
    _approve(draft, root, image)
    old_copy = _snapshot_file(root, draft)
    image.write_bytes(JPEG)
    with pytest.raises(NoteStatusError, match="not allowed"):
        _approve(draft, root, image)  # 承認済みのまま画像だけ替えることはできない
    review.reopen(draft, reason="the thumbnail was replaced", now=NOW + timedelta(hours=1))
    old = draft.approval_history[-1]
    assert old["images"][0]["mime_type"] == "image/png"
    assert old["superseded_reason"] == "the thumbnail was replaced"
    review.submit(draft)
    _approve(draft, root, image, now=NOW + timedelta(hours=1))
    new = draft.approval["images"][0]
    assert new["mime_type"] == "image/jpeg" and new["snapshot"]["path"].endswith(".jpg")
    # 前の写しは消さず、履歴から追える
    assert root / old["images"][0]["snapshot"]["path"] == old_copy
    assert old_copy.read_bytes() == PNG
    assert _snapshot_file(root, draft).read_bytes() == JPEG


def test_reapproving_the_same_image_reuses_the_snapshot(tmp_path) -> None:
    root, _path, draft = _ready(tmp_path)
    image = _image(tmp_path)
    _approve(draft, root, image)
    first = draft.approval["images"][0]["snapshot"]
    review.reopen(draft, reason="checking again", now=NOW + timedelta(minutes=5))
    review.submit(draft)
    _approve(draft, root, image, now=NOW + timedelta(minutes=6))
    again = draft.approval["images"][0]["snapshot"]
    assert again["path"] == first["path"] and again["reused"] is True
    assert _snapshot_file(root, draft).read_bytes() == PNG
    assert len(list((root / review.SNAPSHOT_DIR / draft.id).iterdir())) == 1


def test_the_approval_round_trips_through_json(tmp_path) -> None:
    root, _path, draft = _ready(tmp_path)
    _approve(draft, root, _image(tmp_path))
    again = review.draft_from_dict(json.loads(json.dumps(draft.as_dict(), ensure_ascii=False)))
    assert again.approval == draft.approval and again.approval_history == []
    assert review.approved_images(again) == draft.approval["images"]


def test_editing_after_approval_keeps_the_old_approval_in_history(tmp_path) -> None:
    root, _path, draft = _ready(tmp_path)
    _approve(draft, root, _image(tmp_path))
    approved_hash = draft.approval["content_hash"]
    review.apply_edit(draft, EDITED.replace("小さく始めて", "少しずつ"),
                      commissions_known=False, editor="human", now=NOW)
    assert draft.approval is None and draft.status == "draft"
    assert draft.approval_history[-1]["content_hash"] == approved_hash
    assert draft.approval_history[-1]["superseded_reason"] == "the body was edited"
    assert draft.approval_history[-1]["images"][0]["snapshot"]["path"]


def test_a_retrospective_reapproval_keeps_the_real_times(tmp_path) -> None:
    from datetime import datetime

    root, _path, draft = _ready(tmp_path)
    image = _image(tmp_path)
    _approve(draft, root, image)
    image.write_bytes(JPEG)  # 承認の後に差し替え、それを公開した
    later = NOW + timedelta(hours=2)
    review.reopen(draft, reason="replaced after approval", now=later)
    review.submit(draft)
    published = NOW + timedelta(hours=1)
    with pytest.raises(NoteStatusError, match="timezone"):
        _approve(draft, root, image, now=later, after_publication_at=datetime(2026, 9, 26, 10, 0))
    with pytest.raises(NoteStatusError, match="future"):
        _approve(draft, root, image, now=later, after_publication_at=later + timedelta(days=1))
    _approve(draft, root, image, now=later, note="checked the published image",
             after_publication_at=published)
    assert draft.approval["approved_at"] == later.isoformat(timespec="seconds")  # 実際の時刻
    assert draft.approval["retrospective"]["approved_after_publication"] is True
    assert draft.approval["approval_note"] == "checked the published image"
    assert draft.approval_history[0]["approved_at"] == NOW.isoformat(timespec="seconds")
    assert draft.approval_history[0]["images"][0]["mime_type"] == "image/png"
    _publish(draft, root, image, url="https://note.com/u/n/p4")
    assert draft.publication["approved_images"][0]["mime_type"] == "image/jpeg"


# -- 古い承認 (legacy) -----------------------------------------------------------------------------
def test_old_approvals_without_image_data_still_work_and_are_not_backfilled(tmp_path) -> None:
    root, _path, draft = _ready(tmp_path, brief=None)
    data = draft.as_dict()
    data["status"] = "approved"
    data["approval"] = {"approved_by": "human", "approved_at": NOW.isoformat(),
                        "content_hash": draft.content_hash, "access_mode": "free",
                        "links": [], "images_approved": False, "images": []}  # 古い形 (v1)
    data.pop("approval_history", None)
    old = review.draft_from_dict(data)
    assert old.approval_history == [] and review.approved_images(old) == []
    _publish(old, root, url="https://note.com/u/n/p3")
    assert old.publication["approved_images"] == []
    assert "approval_schema" not in old.approval  # 過去の承認を書き換えない
    assert not (root / review.SNAPSHOT_DIR).exists()


def _legacy_v2(draft) -> dict:
    """写しの無い ``note-approval/2`` の承認 (controlled #1・#2 と同じ形)。"""

    data = draft.as_dict()
    data["status"] = "approved"
    data["approval"] = {"approval_schema": "note-approval/2", "approved_by": "human",
                        "approved_at": NOW.isoformat(), "content_hash": draft.content_hash,
                        "access_mode": "free", "links": [], "images_approved": True,
                        "images": [{"filename": "thumb.png", "bytes": len(PNG),
                                    "sha256": hashlib.sha256(PNG).hexdigest(),
                                    "mime_type": "image/png"}]}  # fmt: skip
    return data


def test_legacy_image_approvals_without_a_snapshot_stay_valid(tmp_path) -> None:
    root, _path, draft = _ready(tmp_path)
    old = review.draft_from_dict(_legacy_v2(draft))
    assert review.verify_snapshot(old.approval["images"][0], root)["state"] == "legacy"
    assert review.review_packet(old)["images"][0]["canonical"] == "legacy_no_snapshot"
    assert "legacy (no snapshot; not backfilled)" in review.render_packet(old)
    with pytest.raises(NoteStatusError, match="legacy"):
        _publish(old, root)  # 写しが無いので、今までどおり使ったファイルが要る
    _publish(old, root, _image(tmp_path))
    assert old.publication["approved_images"][0]["canonical"] == "legacy_no_snapshot"
    assert "snapshot" not in old.approval["images"][0]  # 後から写しを作らない
    assert not (root / review.SNAPSHOT_DIR).exists()


# -- 確認用のまとめ・CLI・台帳 ---------------------------------------------------------------------
def test_the_packet_shows_basename_bytes_hash_and_snapshot(tmp_path) -> None:
    root, _path, draft = _ready(tmp_path)
    text = review.render_packet(draft)
    assert "image: **required**" in text and "human checks" in text
    _approve(draft, root, _image(tmp_path))
    text = review.render_packet(draft)
    assert "approved image: thumb.png" in text and hashlib.sha256(PNG).hexdigest() in text
    assert "canonical: snapshot reports/note/approved-images/" in text
    assert str(tmp_path) not in text


def test_the_cli_binds_the_image_through_a_snapshot(tmp_path, capsys) -> None:
    from scripts.manage_note_piece import main

    root, path, draft = _ready(tmp_path)
    path.write_text(json.dumps(draft.as_dict(), ensure_ascii=False), encoding="utf-8")
    image = _image(tmp_path)
    base = ["approve", draft.id, "--content-hash", draft.content_hash, "--by", "human"]
    assert main(base, root=root, now=NOW) == 2  # 画像なし
    assert "--image" in capsys.readouterr().out
    assert main([*base, "--image", str(image)], root=root, now=NOW) == 0
    capsys.readouterr()
    saved = json.loads(path.read_text("utf-8"))
    assert saved["approval"]["images"][0]["filename"] == "thumb.png"
    assert (root / saved["approval"]["images"][0]["snapshot"]["path"]).read_bytes() == PNG
    image.write_bytes(JPEG)  # 承認の後に元のファイルが変わった
    assert main(["check-image", draft.id, "--image", str(image)], root=root, now=NOW) == 1
    assert '"approval_valid": true' in capsys.readouterr().out
    pub = ["record-publication", draft.id, "--url", "https://note.com/u/n/p9",
           "--observed-at", "2026-10-01T12:00+09:00", "--content-hash", draft.content_hash]
    assert main([*pub, "--image", str(image)], root=root, now=NOW) == 2  # 別の画像を使った
    assert main(pub, root=root, now=NOW) == 0  # 写しで記録する
    saved = json.loads(path.read_text("utf-8"))
    assert saved["status"] == "published"
    assert saved["publication"]["approved_images"][0]["canonical"] == "approval_snapshot"


def test_the_image_record_round_trips_through_the_ledger(session, tmp_path) -> None:
    from sqlalchemy import select

    from app.models import NotePiece
    from app.services.note_ledger_service import NoteLedgerService

    root, path, draft = _ready(tmp_path)
    _approve(draft, root, _image(tmp_path))
    path.write_text(json.dumps(draft.as_dict(), ensure_ascii=False), encoding="utf-8")
    NoteLedgerService(session, policy=POLICY).sync_from_drafts(root, execute=True, now=NOW)
    row = session.scalars(select(NotePiece).where(NotePiece.draft_id == draft.id)).one()
    assert row.approval_json["images"] == draft.approval["images"]
    assert row.approval_json["images"][0]["snapshot"]["path"].startswith("reports/note/")
    assert row.approved_hash == draft.content_hash


def test_snapshots_and_source_artifacts_stay_out_of_git() -> None:
    ignored = {line.strip() for line in (ROOT / ".gitignore").read_text("utf-8").splitlines()}
    assert "reports/" in ignored and "artifacts/" in ignored
    assert review.SNAPSHOT_DIR.parts[0] == "reports"
