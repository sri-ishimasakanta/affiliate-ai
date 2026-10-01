"""有料の note 記事の承認 (``note-approval/4``、2026-10-01)。手元だけ (note に書かない)。

pin する契約:

- 無料の記事は今までどおり ``note-approval/3`` (価格は要らない。販売の条件を渡すと拒む)。
- 有料の記事は ``note-approval/4``: 本文の ``content_hash`` と、販売の条件 (有料の境界・価格・
  売る物の完全な hash) の ``commercial_hash`` の両方に承認が結びつく。どれかが変われば承認は
  効かない (取り消し → 出し直し → 承認し直し、前の承認は履歴に残る)。
- 価格は正の整数と JPY だけ。境界は無料の節が 1 つ以上・有料の節が 1 つ以上。売る物は手元の
  release candidate から読み、zip のバイトを確かめる。有料の部分は package の資産の行をすべて含む。
- 最後の有料の承認は、売る物が H5 で承認された版であることを要る (H4 / H5 の記録はテストの中だけ)。
- 公開の記録: 価格の観測 (承認と違えば記録しない)・無料の部分の確認・有料の部分は人の確認
  (無ければ ``human_verification_required``)。後から人の確認を足せる (追記だけ)。
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app.products import build as pb
from app.products import records as rec
from app.products.spec import check, load_policy, load_product
from app.social.note import paid, review
from app.social.note.models import NoteStatusError
from app.social.note.sources import load_sources
from tests.unit.test_note_channel import NOW
from tests.unit.test_products import PID
from tests.unit.test_products import _repo as _product_repo

POLICY = {"publication_url_hosts": ["note.com"]}
PNG = b"\x89PNG\r\n\x1a\n" + b"paid-thumbnail-v1"
JPEG = b"\xff\xd8\xff" + b"paid-thumbnail-v2"
FREE = """## どんな人向けか

AIで作業を自動化し始めた人に向けた記事です。

## 何が問題か

どこで人が止めるかが曖昧になります。
"""


def _setup(tmp_path):
    from scripts.manage_products import _write_candidate

    root = _product_repo(tmp_path)
    product = load_product(root, PID)
    result = check(product, load_sources(root), policy=load_policy())
    candidate = pb.build_candidate(product, result["sources"], policy=load_policy(), terms=[])
    _write_candidate(root, product, candidate)
    return root, product, result, candidate


def _paid_markdown(product) -> str:
    parts = []
    for path, text in sorted(product.assets.items()):
        body = "\n".join("#" + line if line.startswith("#") else line for line in text.splitlines())
        if path.endswith(".json"):
            body = "```json\n" + text.strip() + "\n```"
        parts.append(f"## {path}\n\n{body}")
    return "\n\n".join(parts) + "\n"


def _draft(root, product, *, amount=500, currency="JPY", free=FREE):
    draft = review.import_paid_draft(title="承認の門", free_markdown=free,
                                     paid_markdown=_paid_markdown(product), root=root,
                                     product_id=PID, amount=amount, currency=currency, now=NOW)
    review.recheck(draft, commissions_known=False)
    return draft


def _release(root, product, result, candidate):
    rec.record_quality_review(product, result, by="human", now=NOW,
                              checklist={k: True for k in rec.CHECKLIST})
    rec.record_content_approval(product, result, content_hash=product.content_hash, by="human",
                                now=NOW)
    rec.record_release_approval(product, candidate, manifest_hash=candidate["manifest_hash"],
                                by="human", now=NOW)


def _approve(draft, root, *, image=None, commercial=None, **kw):
    return review.approve(draft, content_hash=draft.content_hash, approved_by="human",
                          now=kw.pop("now", NOW), links_approved=True, image=image,
                          snapshot_root=root, product_root=root,
                          commercial_hash=commercial or paid.commercial_hash(draft), **kw)


def _ready(tmp_path, *, release=True):
    root, product, result, candidate = _setup(tmp_path)
    if release:
        _release(root, product, result, candidate)
    draft = _draft(root, product)
    review.submit(draft)
    return root, product, candidate, draft


# -- 無料の記事は変わらない ---------------------------------------------------------------------
def test_free_articles_keep_note_approval_3_without_a_price(tmp_path) -> None:
    from tests.unit.test_note_review import EDITED
    from tests.unit.test_note_review import _draft as _free_draft

    _root, _path, draft = _free_draft(tmp_path)
    review.apply_edit(draft, EDITED, commissions_known=False, editor="claude", now=NOW)
    review.submit(draft)
    with pytest.raises(NoteStatusError, match="free article has no commercial terms"):
        review.approve(draft, content_hash=draft.content_hash, approved_by="human", now=NOW,
                       commercial_hash="0" * 64)
    review.approve(draft, content_hash=draft.content_hash, approved_by="human", now=NOW)
    assert draft.approval["approval_schema"] == "note-approval/3"
    assert "commercial_hash" not in draft.approval and draft.price is None
    review.record_publication(draft, url="https://note.com/u/n/free1", observed_at=NOW,
                              published_hash=draft.content_hash, now=NOW, policy=POLICY)
    assert "paid" not in draft.publication
    assert review.review_packet(draft)["paid"] is None


def test_old_drafts_load_without_paid_fields() -> None:
    data = {"id": "draft-aaaaaaaaaa", "candidate_id": "c", "content_type": "build_log",
            "working_title": "t", "premise": "p", "audience": "a", "summary": "s",
            "sections": [{"heading": "h", "paragraphs": ["x"]}], "claims": [], "phases": [],
            "source_event_ids": [], "wordpress_refs": [], "threads_refs": [],
            "created_at": "2026-09-30T00:00:00+00:00", "status": "published",
            "approval": {"approval_schema": "note-approval/2", "approved_by": "human",
                         "approved_at": "2026-09-30T00:00:00+00:00", "content_hash": "x"}}
    draft = review.draft_from_dict(data)
    assert (draft.paid_from_section, draft.price, draft.product) == (None, None, None)
    assert paid.problems(draft) == [] and review.paid_section_state(draft.publication) is None


# -- 有料の記事の形 ----------------------------------------------------------------------------
def test_import_binds_the_full_product_identity_and_the_boundary(tmp_path) -> None:
    root, product, _result, candidate = _setup(tmp_path)
    draft = _draft(root, product)
    assert draft.id == review.paid_draft_id(PID, product.version) and draft.status == "draft"
    assert draft.access_mode == "paid" and draft.paid_from_section == 2
    assert draft.price == {"amount": 500, "currency": "JPY"}
    assert draft.product == {"product_id": PID, "version": product.version,
                             "content_hash": product.content_hash,
                             "manifest_hash": candidate["manifest_hash"],
                             "package_sha256": candidate["package_sha256"]}
    assert all(len(draft.product[k]) == 64 for k in ("content_hash", "manifest_hash",
                                                     "package_sha256"))
    assert paid.missing_asset_lines(draft, root) == {a: 0 for a in
                                                     paid.package_asset_lines(root, draft.product)}
    bound = paid.boundary(draft)
    assert bound["free_headings"] == ["どんな人向けか", "何が問題か"]
    assert draft.errors == [] and draft.content_hash == _draft(root, product).content_hash
    assert paid.commercial_hash(draft) == paid.commercial_hash(_draft(root, product))  # 決まる


@pytest.mark.parametrize("amount, currency, match", [
    (0, "JPY", "above 0"), (-500, "JPY", "above 0"), (500.5, "JPY", "whole number"),
    (500, "USD", "currency"), (500, "", "currency"), (True, "JPY", "whole number")])
def test_bad_prices_are_refused(tmp_path, amount, currency, match) -> None:
    root, product, _r, _c = _setup(tmp_path)
    with pytest.raises(NoteStatusError, match=match):
        _draft(root, product, amount=amount, currency=currency)


def test_the_free_and_paid_parts_are_both_required(tmp_path) -> None:
    root, product, _r, _c = _setup(tmp_path)
    with pytest.raises(NoteStatusError, match="free part and a paid part"):
        review.import_paid_draft(title="t", free_markdown="", paid_markdown=_paid_markdown(product),
                                 root=root, product_id=PID, amount=500, currency="JPY", now=NOW)
    with pytest.raises(NoteStatusError, match="free part and a paid part"):
        review.import_paid_draft(title="t", free_markdown=FREE, paid_markdown="", root=root,
                                 product_id=PID, amount=500, currency="JPY", now=NOW)
    draft = _draft(root, product)
    for index, match in ((0, "free part is empty"), (len(draft.sections), "paid part is empty")):
        draft.paid_from_section = index
        assert match in paid.problems(draft)[0]


@pytest.mark.parametrize("field, value, match", [
    ("price", None, "approved price"), ("product", None, "product it sells"),
    ("paid_from_section", None, "paid boundary")])
def test_missing_terms_block_submit_and_approval(tmp_path, field, value, match) -> None:
    root, product, _r, _c = _setup(tmp_path)
    draft = _draft(root, product)
    setattr(draft, field, value)
    review.recheck(draft, commissions_known=False)
    assert any(match in e for e in draft.errors)
    with pytest.raises(NoteStatusError):
        review.submit(draft)


def test_short_or_unsafe_product_identities_are_refused(tmp_path) -> None:
    root, product, _r, _c = _setup(tmp_path)
    with pytest.raises(NoteStatusError, match="unsafe product id"):
        paid.bind_product(root, "../products")
    draft = _draft(root, product)
    draft.product = {**draft.product, "manifest_hash": draft.product["manifest_hash"][:12]}
    assert "full sha256" in paid.problems(draft)[0]
    draft.product = {**draft.product, "extra": "x"}
    assert "exactly" in paid.problems(draft)[0]


def test_a_changed_package_is_refused_at_binding(tmp_path) -> None:
    root, product, _r, _c = _setup(tmp_path)
    zip_path = root / "reports/products" / PID / product.version / f"{PID}-{product.version}.zip"
    zip_path.write_bytes(zip_path.read_bytes() + b"x")
    with pytest.raises(NoteStatusError, match="package bytes"):
        paid.bind_product(root, PID)


# -- 承認 --------------------------------------------------------------------------------------
def test_the_paid_approval_needs_the_h5_release(tmp_path) -> None:
    root, _product, _candidate, draft = _ready(tmp_path, release=False)
    with pytest.raises(NoteStatusError, match="not H5-approved"):
        _approve(draft, root, image=_image(tmp_path))
    assert draft.status == "review_ready" and draft.approval is None
    assert not (root / review.SNAPSHOT_DIR).exists()  # 拒んだときは写しも作らない


def _image(tmp_path, data=PNG):
    path = tmp_path / "thumb.png"
    path.write_bytes(data)
    return path


def test_the_paid_approval_is_note_approval_4_with_commercial_terms(tmp_path) -> None:
    root, product, candidate, draft = _ready(tmp_path)
    with pytest.raises(NoteStatusError, match="--commercial-hash"):
        review.approve(draft, content_hash=draft.content_hash, approved_by="human", now=NOW,
                       links_approved=True, snapshot_root=root, product_root=root)
    with pytest.raises(NoteStatusError, match="commercial hash mismatch"):
        _approve(draft, root, commercial="0" * 64)
    _approve(draft, root, image=_image(tmp_path))
    approval = draft.approval
    assert approval["approval_schema"] == "note-approval/4"
    assert approval["content_hash"] == draft.content_hash
    assert approval["commercial_hash"] == paid.commercial_hash(draft)
    terms = approval["paid_terms"]
    assert terms["price"] == {"amount": 500, "currency": "JPY"}
    assert terms["product"]["manifest_hash"] == candidate["manifest_hash"]
    assert terms["boundary"]["paid_from_section"] == 2
    assert approval["product_release"]["manifest_hash"] == candidate["manifest_hash"]
    assert approval["images"][0]["canonical"] == "approval_snapshot"  # -3 の写しのまま
    assert str(tmp_path) not in json.dumps(approval)  # 絶対パスは記録しない


@pytest.mark.parametrize("change", ["price", "currency", "boundary", "version", "hash"])
def test_any_commercial_change_invalidates_the_approval(tmp_path, change) -> None:
    root, _product, _candidate, draft = _ready(tmp_path)
    _approve(draft, root, image=_image(tmp_path))
    before = draft.approval["commercial_hash"]
    with pytest.raises(NoteStatusError, match="reopen first"):
        review.set_paid_terms(draft, amount=980, currency="JPY")
    # 承認の後に JSON を直接書き換えても、公開の記録で止まる
    if change == "price":
        draft.price = {"amount": 980, "currency": "JPY"}
    elif change == "currency":
        draft.price = {"amount": 500, "currency": "USD"}
    elif change == "boundary":
        draft.paid_from_section = 1
    elif change == "version":
        draft.product = {**draft.product, "version": "0.2.1"}
    else:
        draft.product = {**draft.product, "package_sha256": "f" * 64}
    with pytest.raises(NoteStatusError):
        review.record_publication(draft, url="https://note.com/u/n/p1", observed_at=NOW,
                                  published_hash=draft.content_hash, now=NOW, policy=POLICY,
                                  snapshot_root=root, product_root=root)
    assert draft.status == "approved"
    if change in ("price", "boundary"):
        assert paid.commercial_hash(draft) != before


def test_reopen_change_the_price_and_approve_again_keeps_history(tmp_path) -> None:
    root, _product, _candidate, draft = _ready(tmp_path)
    image = _image(tmp_path)
    _approve(draft, root, image=image)
    old = draft.approval["commercial_hash"]
    review.reopen(draft, reason="price changed by the human", now=NOW + timedelta(hours=1))
    review.set_paid_terms(draft, amount=980, currency="JPY")
    assert draft.content_hash == draft.approval_history[-1]["content_hash"]  # 本文は同じ
    review.submit(draft)
    with pytest.raises(NoteStatusError, match="commercial hash mismatch"):
        _approve(draft, root, image=image, commercial=old)
    _approve(draft, root, image=image, now=NOW + timedelta(hours=2))
    assert draft.approval["paid_terms"]["price"]["amount"] == 980
    assert draft.approval_history[-1]["paid_terms"]["price"]["amount"] == 500
    assert draft.approval["images"][0]["snapshot"]["reused"] is True  # 同じ画像の写し


def test_content_and_image_changes_need_reapproval(tmp_path) -> None:
    root, product, _candidate, draft = _ready(tmp_path)
    image = _image(tmp_path)
    _approve(draft, root, image=image)
    with pytest.raises(NoteStatusError, match="not allowed"):
        _approve(draft, root, image=_image(tmp_path, JPEG))  # 承認のまま画像だけは替えない
    review.reopen(draft, reason="new thumbnail", now=NOW + timedelta(hours=1))
    review.submit(draft)
    _approve(draft, root, image=_image(tmp_path, JPEG), now=NOW + timedelta(hours=1))
    assert draft.approval["images"][0]["mime_type"] == "image/jpeg"
    # 本文を変えるのは取り込み直し (境界を明示したまま)。前の承認は履歴に残る
    with pytest.raises(NoteStatusError, match="re-importing"):
        review.apply_edit(draft, "# t\n\n## h\n\nx\n", commissions_known=False)
    changed = review.import_paid_draft(title="承認の門", free_markdown=FREE + "\n追記の段落。\n",
                                       paid_markdown=_paid_markdown(product), root=root,
                                       product_id=PID, amount=500, currency="JPY",
                                       now=NOW + timedelta(hours=2), existing=draft)
    assert changed.status == "draft" and changed.approval is None
    assert changed.approval_history[-1]["superseded_reason"].startswith("re-imported")


def test_the_paid_part_must_contain_the_product_assets(tmp_path) -> None:
    root, product, result, candidate = _setup(tmp_path)
    _release(root, product, result, candidate)
    draft = review.import_paid_draft(title="t", free_markdown=FREE,
                                     paid_markdown="## 一部だけ\n\nチェックリストの一部。\n",
                                     root=root, product_id=PID, amount=500, currency="JPY",
                                     now=NOW)
    review.recheck(draft, commissions_known=False)
    review.submit(draft)
    with pytest.raises(NoteStatusError, match="does not contain the product's assets"):
        _approve(draft, root, image=_image(tmp_path))


def test_a_retrospective_paid_approval_keeps_the_real_times(tmp_path) -> None:
    root, _product, _candidate, draft = _ready(tmp_path)
    later = NOW + timedelta(hours=3)
    _approve(draft, root, image=_image(tmp_path), now=later, note="checked after publishing",
             after_publication_at=NOW + timedelta(hours=1))
    assert draft.approval["retrospective"]["approved_after_publication"] is True
    assert draft.approval["approved_at"] == later.isoformat(timespec="seconds")
    assert draft.approval["approval_schema"] == "note-approval/4"


# -- 公開の記録 --------------------------------------------------------------------------------
def _publish(draft, root, **kw):
    return review.record_publication(draft, url="https://note.com/u/n/paid1", observed_at=NOW,
                                     published_hash=draft.content_hash, now=NOW, policy=POLICY,
                                     snapshot_root=root, product_root=root, **kw)


def test_publication_records_price_and_says_when_the_paid_part_is_unverified(tmp_path) -> None:
    root, _product, _candidate, draft = _ready(tmp_path)
    _approve(draft, root, image=_image(tmp_path))
    with pytest.raises(NoteStatusError, match="not the approved price"):
        _publish(draft, root, observed_price={"amount": 980, "currency": "JPY"})
    assert draft.status == "approved"
    _publish(draft, root, observed_price={"amount": 500, "currency": "JPY", "source": "note page"},
             free_section_check="free part: 4 lines identical")
    record = draft.publication["paid"]
    assert record["approved_price"] == {"amount": 500, "currency": "JPY"}
    assert record["observed_price"]["state"] == "observed"
    assert record["observed_price"]["amount"] == 500
    assert record["public_free_section"]["state"] == "checked"
    assert record["paid_section"]["state"] == review.HUMAN_VERIFICATION_REQUIRED
    assert review.paid_section_state(draft.publication) == review.HUMAN_VERIFICATION_REQUIRED
    review.record_paid_verification(draft, by="human", method="note editor preview",
                                    now=NOW + timedelta(hours=1))
    assert review.paid_section_state(draft.publication) == "human_verified"
    assert record["paid_section"]["state"] == review.HUMAN_VERIFICATION_REQUIRED  # 前は変えない


def test_publication_can_carry_the_human_paid_verification(tmp_path) -> None:
    root, _product, _candidate, draft = _ready(tmp_path)
    _approve(draft, root, image=_image(tmp_path))
    with pytest.raises(NoteStatusError, match="short name"):
        _publish(draft, root, paid_verified_by="human")
    _publish(draft, root, paid_verified_by="human", paid_verification="purchaser view")
    assert draft.publication["paid"]["paid_section"]["state"] == "human_verified"
    assert draft.publication["paid"]["observed_price"] == {"state": "not_observed"}


def test_a_paid_approval_without_note_approval_4_cannot_be_published(tmp_path) -> None:
    root, _product, _candidate, draft = _ready(tmp_path)
    _approve(draft, root, image=_image(tmp_path))
    draft.approval = {**draft.approval, "approval_schema": "note-approval/3"}
    with pytest.raises(NoteStatusError, match="note-approval/4"):
        _publish(draft, root)


def test_the_snapshot_path_validation_still_applies(tmp_path) -> None:
    root, _product, _candidate, draft = _ready(tmp_path)
    _approve(draft, root, image=_image(tmp_path))
    draft.approval["images"][0]["snapshot"]["path"] = "../outside.png"
    with pytest.raises(NoteStatusError, match="invalid"):
        _publish(draft, root)


# -- CLI ---------------------------------------------------------------------------------------
def test_the_cli_imports_a_paid_draft_and_writes_both_parts(tmp_path, capsys) -> None:
    from scripts.manage_note_piece import main

    root, product, _r, _c = _setup(tmp_path)
    (root / "reports/note/drafts").mkdir(parents=True, exist_ok=True)
    free, paid_md = tmp_path / "free.md", tmp_path / "paid.md"
    free.write_text(FREE, encoding="utf-8")
    paid_md.write_text(_paid_markdown(product), encoding="utf-8")
    args = ["import-paid", "--title", "承認の門", "--free", str(free), "--paid", str(paid_md),
            "--product", PID, "--price", "500", "--currency", "JPY", "--no-db"]
    assert main(args, root=root, now=NOW) == 0
    draft_id = review.paid_draft_id(PID, product.version)
    assert '"approval_schema": "note-approval/4"' in capsys.readouterr().out
    assert main(args, root=root, now=NOW) == 2  # もうある (--replace が要る)
    assert "--replace" in capsys.readouterr().out
    assert main([*args, "--replace"], root=root, now=NOW) == 0
    assert main(["paid-terms", draft_id, "--price", "0", "--currency", "JPY"], root=root,
                now=NOW) == 2
    capsys.readouterr()
    assert main(["submit", draft_id], root=root, now=NOW) == 0
    assert main(["packet", draft_id], root=root, now=NOW) == 0
    base = root / "reports/note/drafts" / draft_id
    packet = base.with_suffix(".packet.md").read_text("utf-8")
    assert "note-approval/4" in packet and "500 JPY" in packet and "**not yet**" in packet
    assert base.with_suffix(".free.txt").read_text("utf-8").startswith("どんな人向けか")
    assert "<pre><code>" in base.with_suffix(".paste.html").read_text("utf-8")
    assert "有料ライン" in base.with_suffix(".paste.html").read_text("utf-8")
    saved = json.loads(base.with_suffix(".json").read_text("utf-8"))
    assert saved["status"] == "review_ready" and saved["price"]["amount"] == 500
