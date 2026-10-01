"""note の 1 本を人と仕上げる (N1)。**公開は人が note の編集画面で行う。ここは記録だけ。**

    uv run python scripts/manage_note_piece.py list
    uv run python scripts/manage_note_piece.py packet <draft_id>   # 確認用 (.packet.md / .txt /
        .paste.html: 見出しは h2・箇条書きは ul の貼り付け用。文字と hash は .txt と同じ)
    uv run python scripts/manage_note_piece.py edit <draft_id> --from edited.md [--editor claude]
    uv run python scripts/manage_note_piece.py meta <draft_id> --tags AI,自動化
        --thumbnail-brief "..."
    uv run python scripts/manage_note_piece.py evidence <draft_id> --text "..." --decision <id>
        [--doc <docs/...md> "<phrase in it>"]
    uv run python scripts/manage_note_piece.py access-mode <draft_id> free|paid
    uv run python scripts/manage_note_piece.py submit <draft_id>
    uv run python scripts/manage_note_piece.py check-image <draft_id> --image <file>
    uv run python scripts/manage_note_piece.py approve <draft_id> --content-hash <sha> --by <name>
        [--links-approved] [--image <file>]     # サムネイルがある下書きは --image が必須
    uv run python scripts/manage_note_piece.py reopen <draft_id> --reason "..."   # 承認を取り消す
    uv run python scripts/manage_note_piece.py record-publication <draft_id> --url <https://note.com/...>
        --observed-at <ISO> --content-hash <sha> [--image <file>]   # 画像は承認の写しで確かめる
    uv run python scripts/manage_note_piece.py reject <draft_id> --reason "..."

    # 有料の記事 (note-approval/4): 無料の部分と有料の部分から下書きを作る。売る物は今の
    # release candidate から完全な hash を読む。承認は H4 と H5 の後だけ (販売の条件も承認に入る)
    uv run python scripts/manage_note_piece.py import-paid --title "..." --free free.md
        --paid paid.md --product <product_id> --price 500 --currency JPY [--replace]
    uv run python scripts/manage_note_piece.py paid-terms <draft_id> [--price N --currency JPY]
        [--product <product_id>] [--paid-from <section index>]
    uv run python scripts/manage_note_piece.py approve <draft_id> --content-hash <sha>
        --commercial-hash <sha> --by <name> [--links-approved] --image <file>
    uv run python scripts/manage_note_piece.py record-publication ... [--observed-price 500
        --observed-currency JPY] [--free-section-check "..."]
        [--paid-verified-by <name> --paid-verification "..."]
    uv run python scripts/manage_note_piece.py record-paid-verification <draft_id> --by <name>
        --method "..."     # 公開の後に、人が有料の部分を確かめた (追記だけ)

書くのは ``reports/note/drafts/`` と、承認した画像の写し ``reports/note/approved-images/`` (どちらも
git 管理外。写しは中身の sha256 の名前で、上書きしない・消さない) だけ。note・WordPress・
Threads・DB には書かない。
ネットワークも LLM も使わない。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.social.note import review  # noqa: E402
from app.social.note.models import NoteStatusError  # noqa: E402
from app.social.note.renderer import render_markdown  # noqa: E402
from app.social.note.sources import load_sources  # noqa: E402

DRAFTS = Path("reports/note/drafts")


def _path(root: Path, draft_id: str) -> Path:
    return root / DRAFTS / f"{draft_id}.json"


def _load(root: Path, draft_id: str):
    path = _path(root, draft_id)
    if not path.exists():
        raise NoteStatusError(f"no local draft {draft_id} (create one with "
                              "propose_note_content.py --draft)")  # fmt: skip
    return review.draft_from_dict(json.loads(path.read_text(encoding="utf-8")))


def _save(root: Path, draft) -> None:
    path = _path(root, draft.id)
    path.write_text(json.dumps(draft.as_dict(), ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")  # fmt: skip
    path.with_suffix(".md").write_text(render_markdown(draft), encoding="utf-8")


def _commissions_known(root: Path) -> bool:
    state = load_sources(root).project_state or {}
    return bool(((state.get("monetization") or {}).get("commission_facts") or {}).get("count"))


def _corpus(root: Path, *, use_db: bool) -> dict[str, str]:
    """重複の検査の相手: 内部のドキュメント・note で公開済みの本文・(任意) 記事と Threads。"""

    from scripts.propose_note_content import db_corpus, internal_corpus

    corpus = internal_corpus(root)
    if use_db:
        from app.config.settings import get_settings

        corpus |= db_corpus(get_settings().database_url)
    return corpus


def _import_paid(root: Path, args, now: datetime):
    """``import-paid``: 無料の部分と有料の部分から有料の下書きを作り、検査し直す。"""

    existing = None
    binding = review.paid.bind_product(root, args.product)
    path = _path(root, review.paid_draft_id(args.product, binding["version"]))
    if path.exists():
        if not args.replace:
            raise NoteStatusError(f"{path.stem} already exists (use --replace to re-import)")
        existing = _load(root, path.stem)
    draft = review.import_paid_draft(
        title=args.title, free_markdown=Path(args.free).read_text(encoding="utf-8"),
        paid_markdown=Path(args.paid).read_text(encoding="utf-8"), root=root,
        product_id=args.product, amount=args.price, currency=args.currency, now=now,
        existing=existing)  # fmt: skip
    review.recheck(draft, commissions_known=_commissions_known(root),
                   corpus=_corpus(root, use_db=not args.no_db))  # fmt: skip
    return draft


def main(argv=None, *, root: Path = ROOT, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    for name in ("packet", "submit"):
        sub.add_parser(name).add_argument("draft_id")
    edit = sub.add_parser("edit")
    edit.add_argument("draft_id")
    edit.add_argument("--from", dest="source", required=True)
    edit.add_argument("--editor", choices=("human", "claude"), default="human",
                      help="who wrote this version (recorded as is)")
    edit.add_argument("--no-db", action="store_true", help="skip the WordPress / Threads check")
    ev = sub.add_parser("evidence")
    ev.add_argument("draft_id")
    ev.add_argument("--text", required=True)
    ev.add_argument("--kind", choices=("observed_fact", "decision"), default="observed_fact")
    ev.add_argument("--decision", action="append", default=[], dest="decisions")
    ev.add_argument("--doc", action="append", default=[], dest="docs", nargs=2,
                    metavar=("PATH", "PHRASE"), help="a doc in the repository and a phrase in it")
    meta = sub.add_parser("meta")
    meta.add_argument("draft_id")
    meta.add_argument("--tags")
    meta.add_argument("--thumbnail-brief")
    mode = sub.add_parser("access-mode")
    mode.add_argument("draft_id")
    mode.add_argument("mode", choices=("free", "paid"))
    appr = sub.add_parser("approve")
    appr.add_argument("draft_id")
    appr.add_argument("--content-hash", required=True)
    appr.add_argument("--by", required=True)
    appr.add_argument("--links-approved", action="store_true")
    appr.add_argument("--image", help="the final thumbnail image file (bound by sha256)")
    appr.add_argument("--note", help="a short note stored with the approval")
    appr.add_argument("--after-publication-at",
                      help="approving after the note was already published: its time (ISO+09:00)")
    chk = sub.add_parser("check-image")
    chk.add_argument("draft_id")
    chk.add_argument("--image", required=True)
    reo = sub.add_parser("reopen")
    reo.add_argument("draft_id")
    reo.add_argument("--reason", required=True)
    pub = sub.add_parser("record-publication")
    pub.add_argument("draft_id")
    pub.add_argument("--url", required=True)
    pub.add_argument("--observed-at", required=True)
    pub.add_argument("--content-hash", required=True)
    pub.add_argument("--image", help="optional check: the image file used in the note (must match "
                     "the approved one; the record itself uses the approval snapshot)")
    pub.add_argument("--note", help="a short note from checking the published page "
                     "(stored with the publication; the approval is not changed)")
    pub.add_argument("--observed-price", type=int, help="paid: the price shown in note")
    pub.add_argument("--observed-currency", default=None)
    pub.add_argument("--free-section-check", help="paid: the result of checking the public part")
    pub.add_argument("--paid-verified-by", help="paid: who checked the paid part (short name)")
    pub.add_argument("--paid-verification", help="paid: how the paid part was checked")
    rej = sub.add_parser("reject")
    rej.add_argument("draft_id")
    rej.add_argument("--reason", required=True)
    imp = sub.add_parser("import-paid")
    for name in ("--title", "--free", "--paid", "--product", "--currency"):
        imp.add_argument(name, required=True)
    imp.add_argument("--price", type=int, required=True)
    imp.add_argument("--replace", action="store_true", help="re-import an existing paid draft")
    imp.add_argument("--no-db", action="store_true", help="skip the WordPress / Threads check")
    terms = sub.add_parser("paid-terms")
    terms.add_argument("draft_id")
    terms.add_argument("--price", type=int)
    terms.add_argument("--currency")
    terms.add_argument("--product")
    terms.add_argument("--paid-from", type=int)
    ver = sub.add_parser("record-paid-verification")
    ver.add_argument("draft_id")
    ver.add_argument("--by", required=True)
    ver.add_argument("--method", required=True)
    appr.add_argument("--commercial-hash", help="paid: the reviewed commercial terms hash")
    args = parser.parse_args(argv)
    now = now or datetime.now(UTC)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    if args.command == "list":
        for path in sorted((root / DRAFTS).glob("*.json")):
            d = review.draft_from_dict(json.loads(path.read_text(encoding="utf-8")))
            pub_url = (d.publication or {}).get("url") or "—"
            print(f"- {d.id} [{d.status}/{d.access_mode}] {d.working_title} "
                  f"(errors {len(d.errors)}, hash {d.content_hash[:12]}, "
                  f"url {pub_url})")  # fmt: skip
        return 0
    try:
        if args.command == "import-paid":
            draft = _import_paid(root, args, now)
            _save(root, draft)
            print(json.dumps({"draft_id": draft.id, "status": draft.status,
                              "content_hash": draft.content_hash,
                              "paid": review.paid_packet(draft, root)},
                             ensure_ascii=False, indent=2))  # fmt: skip
            print("local only: nothing was sent to note, WordPress, Threads or the database")
            return 0
        draft = _load(root, args.draft_id)
        if args.command == "packet":
            path = _path(root, draft.id)
            path.with_suffix(".packet.md").write_text(review.render_packet(draft, root=root),
                                                      encoding="utf-8")  # fmt: skip
            if draft.access_mode == "paid":
                free_text, paid_text = review.plain_text_parts(draft)
                path.with_suffix(".free.txt").write_text(free_text, encoding="utf-8")
                path.with_suffix(".paid.txt").write_text(paid_text, encoding="utf-8")
            path.with_suffix(".txt").write_text(review.plain_text(draft), encoding="utf-8")
            # 表示の形 (見出しは h2、箇条書きは ul)。文字と hash は .txt と同じ
            path.with_suffix(".paste.html").write_text(review.paste_html(draft), encoding="utf-8")
            packet = review.review_packet(draft, root=root)
            print(json.dumps({k: packet[k] for k in ("draft_id", "status", "access_mode", "paid",
                                                     "content_hash", "links", "image_required",
                                                     "images", "errors", "can_submit",
                                                     "can_approve")},
                             ensure_ascii=False, indent=2))  # fmt: skip
            print(f"wrote {path.with_suffix('.packet.md')}, {path.with_suffix('.txt')} and "
                  f"{path.with_suffix('.paste.html')}")
            return 0
        if args.command == "check-image":
            state = review.check_image(draft, args.image, root=root)
            print(json.dumps({"image_required": review.image_required(draft), **state},
                             ensure_ascii=False, indent=2))  # fmt: skip
            return 0 if state["state"] != "changed_since_approval" else 1
        if args.command == "edit":
            text = Path(args.source).read_text(encoding="utf-8")
            review.apply_edit(draft, text, commissions_known=_commissions_known(root),
                              corpus=_corpus(root, use_db=not args.no_db),
                              editor=args.editor, now=now)  # fmt: skip
        elif args.command == "evidence":
            review.add_evidence(draft, text=args.text, kind=args.kind,
                                decision_ids=args.decisions,
                                decisions=load_sources(root).decisions,
                                docs=[tuple(d) for d in args.docs], root=root)  # fmt: skip
        elif args.command == "meta":
            review.set_meta(draft, tags=args.tags.split(",") if args.tags else None,
                            thumbnail_brief=args.thumbnail_brief)  # fmt: skip
        elif args.command == "access-mode":
            review.set_access_mode(draft, args.mode)
        elif args.command == "submit":
            review.submit(draft)
        elif args.command == "approve":
            published_at = (datetime.fromisoformat(args.after_publication_at)
                            if args.after_publication_at else None)  # fmt: skip
            review.approve(draft, content_hash=args.content_hash, approved_by=args.by, now=now,
                           links_approved=args.links_approved, image=args.image,
                           note=args.note, after_publication_at=published_at,
                           snapshot_root=root, commercial_hash=args.commercial_hash,
                           product_root=root)  # fmt: skip
        elif args.command == "record-publication":
            observed = datetime.fromisoformat(args.observed_at)
            if observed.tzinfo is None:
                raise NoteStatusError("--observed-at needs a timezone (e.g. +09:00)")
            review.record_publication(draft, url=args.url, observed_at=observed,
                                      published_hash=args.content_hash, now=now,
                                      policy=review.load_policy(),
                                      image=args.image, snapshot_root=root,
                                      note=args.note, product_root=root,
                                      observed_price=(
                                          {"amount": args.observed_price,
                                           "currency": args.observed_currency,
                                           "source": "note page"}
                                          if args.observed_price is not None else None),
                                      free_section_check=args.free_section_check,
                                      paid_verified_by=args.paid_verified_by,
                                      paid_verification=args.paid_verification)  # fmt: skip
        elif args.command == "paid-terms":
            review.set_paid_terms(draft, root=root, product_id=args.product,
                                  amount=args.price, currency=args.currency,
                                  paid_from_section=args.paid_from)  # fmt: skip
        elif args.command == "record-paid-verification":
            review.record_paid_verification(draft, by=args.by, method=args.method, now=now)
        elif args.command == "reopen":
            review.reopen(draft, reason=args.reason, now=now)
        elif args.command == "reject":
            review.reject(draft, reason=args.reason, now=now)
    except (NoteStatusError, ValueError, OSError) as exc:
        print(f"refused: {exc}")
        return 2
    _save(root, draft)
    print(f"{draft.id}: status {draft.status}, access mode {draft.access_mode}, "
          f"hash {draft.content_hash[:12]}, errors {len(draft.errors)}")  # fmt: skip
    print("local only: nothing was sent to note, WordPress, Threads or the database")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
