"""note の 1 本を人と仕上げる (N1)。**公開は人が note の編集画面で行う。ここは記録だけ。**

    uv run python scripts/manage_note_piece.py list
    uv run python scripts/manage_note_piece.py packet <draft_id>   # 確認用 (.packet.md / .txt)
    uv run python scripts/manage_note_piece.py edit <draft_id> --from edited.md [--editor claude]
    uv run python scripts/manage_note_piece.py meta <draft_id> --tags AI,自動化
        --thumbnail-brief "..."
    uv run python scripts/manage_note_piece.py access-mode <draft_id> free|paid
    uv run python scripts/manage_note_piece.py submit <draft_id>
    uv run python scripts/manage_note_piece.py approve <draft_id> --content-hash <sha> --by <name>
    uv run python scripts/manage_note_piece.py record-publication <draft_id> --url <https://note.com/...>
        --observed-at <ISO> --content-hash <sha>
    uv run python scripts/manage_note_piece.py reject <draft_id> --reason "..."

書くのは ``reports/note/drafts/`` (git 管理外) だけ。note・WordPress・Threads・DB には書かない。
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
    appr.add_argument("--images-approved", action="store_true")
    pub = sub.add_parser("record-publication")
    pub.add_argument("draft_id")
    pub.add_argument("--url", required=True)
    pub.add_argument("--observed-at", required=True)
    pub.add_argument("--content-hash", required=True)
    rej = sub.add_parser("reject")
    rej.add_argument("draft_id")
    rej.add_argument("--reason", required=True)
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
        draft = _load(root, args.draft_id)
        if args.command == "packet":
            path = _path(root, draft.id)
            path.with_suffix(".packet.md").write_text(review.render_packet(draft),
                                                      encoding="utf-8")  # fmt: skip
            path.with_suffix(".txt").write_text(review.plain_text(draft), encoding="utf-8")
            packet = review.review_packet(draft)
            print(json.dumps({k: packet[k] for k in ("draft_id", "status", "access_mode",
                                                     "content_hash", "links", "errors",
                                                     "can_submit", "can_approve")},
                             ensure_ascii=False, indent=2))  # fmt: skip
            print(f"wrote {path.with_suffix('.packet.md')} and {path.with_suffix('.txt')}")
            return 0
        if args.command == "edit":
            text = Path(args.source).read_text(encoding="utf-8")
            review.apply_edit(draft, text, commissions_known=_commissions_known(root),
                              corpus=_corpus(root, use_db=not args.no_db),
                              editor=args.editor)  # fmt: skip
        elif args.command == "meta":
            review.set_meta(draft, tags=args.tags.split(",") if args.tags else None,
                            thumbnail_brief=args.thumbnail_brief)  # fmt: skip
        elif args.command == "access-mode":
            review.set_access_mode(draft, args.mode)
        elif args.command == "submit":
            review.submit(draft)
        elif args.command == "approve":
            review.approve(draft, content_hash=args.content_hash, approved_by=args.by, now=now,
                           links_approved=args.links_approved,
                           images_approved=args.images_approved)  # fmt: skip
        elif args.command == "record-publication":
            observed = datetime.fromisoformat(args.observed_at)
            if observed.tzinfo is None:
                raise NoteStatusError("--observed-at needs a timezone (e.g. +09:00)")
            review.record_publication(draft, url=args.url, observed_at=observed,
                                      published_hash=args.content_hash, now=now,
                                      policy=review.load_policy())  # fmt: skip
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
