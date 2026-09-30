"""管理用 CLI: note の候補を探し、ローカルの下書きを作る (N0。**公開しない**)。

    uv run python scripts/propose_note_content.py              # 候補を表示するだけ
    uv run python scripts/propose_note_content.py --write      # 候補を reports/note/ に書く
    uv run python scripts/propose_note_content.py --draft top  # 一番の候補の下書き

読むもの: プロジェクトの状態の報告・roadmap・決定の記録・運用のドキュメント・(任意) アプリの DB
(重複の検査のための記事本文と Threads の公開文。SQLite は mode=ro)。
書くもの: ``reports/note/`` (git 管理外) だけ。note・WordPress・Threads・スケジューラ・DB には
書かない。ネットワークを使わない (LLM も呼ばない)。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.social.note.candidates import discover, doc_paths, topic_for  # noqa: E402
from app.social.note.renderer import build_draft, render_markdown  # noqa: E402
from app.social.note.sources import duplication_corpus, load_sources  # noqa: E402

OUT = Path("reports/note")


def used_source_events(root: Path) -> set[str]:
    used = set()
    for path in sorted((root / OUT / "drafts").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("status") in ("approved", "published"):
            used |= set(data.get("source_event_ids") or [])
    return used


def internal_corpus(root: Path) -> dict[str, str]:
    corpus = {}
    for path in [
        root / "reports/project_state_latest.md",
        *sorted((root / "docs/operations").glob("*.md")),
    ]:
        if path.exists():
            corpus[f"internal:{path.relative_to(root).as_posix()}"] = path.read_text(
                encoding="utf-8"
            )
    # 人がすでに note で公開した本文 (仕組みの外で公開したものを含む。手元の写しだけ)
    for path in sorted((root / "reports/note/external").glob("*.txt")):
        corpus[f"note-external:{path.stem}"] = path.read_text(encoding="utf-8")
    return corpus


def db_corpus(database_url: str) -> dict[str, str]:
    from app.project_state.db_state import readonly_engine

    with readonly_engine(database_url).connect() as conn:
        return duplication_corpus(conn)


def main(argv=None, *, root: Path = ROOT, now: datetime | None = None, database_url=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="候補を reports/note/ に書く")
    parser.add_argument("--draft", help="下書きを作る候補 (id か top)")
    parser.add_argument("--no-db", action="store_true", help="DB の重複の検査を使わない")
    args = parser.parse_args(argv)
    now = now or datetime.now(UTC)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # Windows のコンソール (cp932) で落ちない

    sources = load_sources(root, doc_names=doc_paths())
    candidates = discover(sources, used_source_events=used_source_events(root))
    print(f"note candidates: {len(candidates)} (read-only discovery; nothing is published)")
    for i, c in enumerate(candidates, start=1):
        s = c.score
        print(f"{i}. [{s['total']}] {c.working_title}")
        print(f"   type={c.content_type} evidence={c.evidence_found}/{c.evidence_required} "
              f"duplication_risk={c.duplication_risk} id={c.id}")  # fmt: skip
        print("   score: " + ", ".join(f"{k}={v}" for k, v in s.items() if k != "total"))
        for w in c.warnings:
            print(f"   warning: {w}")
    if args.write or args.draft:
        out = root / OUT
        out.mkdir(parents=True, exist_ok=True)
        (out / "candidates_latest.json").write_text(
            json.dumps({"generated_at": now.isoformat(timespec="seconds"),
                        "candidates": [c.as_dict() for c in candidates]},
                       ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )  # fmt: skip
        print(f"wrote {out / 'candidates_latest.json'}")
    if args.draft:
        chosen = (
            candidates[0]
            if args.draft == "top"
            else next((c for c in candidates if c.id == args.draft), None)
        )
        if chosen is None:
            print(f"no candidate {args.draft}")
            return 2
        corpus = internal_corpus(root)
        if not args.no_db:
            if database_url is None:
                from app.config.settings import get_settings

                database_url = get_settings().database_url
            corpus |= db_corpus(database_url)
        draft = build_draft(chosen, topic_for(chosen), sources, now=now, corpus=corpus)
        drafts = root / OUT / "drafts"
        drafts.mkdir(parents=True, exist_ok=True)
        (drafts / f"{draft.id}.json").write_text(
            json.dumps(draft.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (drafts / f"{draft.id}.md").write_text(render_markdown(draft), encoding="utf-8")
        print(f"wrote local draft {drafts / (draft.id + '.md')} (status {draft.status}; "
              f"errors {len(draft.errors)}, warnings {len(draft.warnings)})")  # fmt: skip
    print("local only: nothing was sent to note, WordPress, Threads, the scheduler or the database")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
