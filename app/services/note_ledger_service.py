"""note の記録 (N2)。下書きのファイルから ``note_pieces`` へ同期し、流れ・頻度・リンクを示す。

- ``sync_from_drafts(root, execute)``: ``reports/note/drafts/*.json`` を ``draft_id`` で同期する。
  公開済みの記録は戻さない (ファイルが別の状態でも)。公開の URL が食い違えば同期しない
  (``conflicts``)。``execute=False`` は何も書かない。
- ``loop_status()``: 状態ごとの 1 本と、人の次の一手。
- ``cadence_plan(now)``: 週ごとの公開の数と目安 (週 2 本程度。**目安であって決まりではない**)。
- ``related_links(draft)``: 関係する公開済みの記事 (読むだけ。本文には入れない)。
- ``published_bodies()``: 重複の検査に使う、公開済みの本文。

表が無ければ (migration ``a4a74a5bcb8b`` の前) ``NoteLedgerError`` で止まる。外には問い合わせない。
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware
from app.models import Article
from app.models.n_track import NotePiece
from app.social.note import links as note_links
from app.social.note import review

DRAFTS = Path("reports/note/drafts")
NEXT_STEP = {
    "draft": "a human reviews the packet, edits if needed, then submits",
    "review_ready": "a human approves the exact content hash (and links)",
    "approved": "the human publishes in note, then records the URL",
    "published": "record reader numbers by hand (N3)",
    "rejected": "—",
}


class NoteLedgerError(RuntimeError):
    pass


def tables_ready(session: Session) -> bool:
    names = set(inspect(session.get_bind()).get_table_names())
    return {"note_pieces", "manual_metric_entries"} <= names


def _dt(value: str | None) -> datetime | None:
    return ensure_aware(datetime.fromisoformat(value)) if value else None


def _same(stored, value) -> bool:
    """SQLite は時刻をタイムゾーン無しで返すので、そろえてから比べる。"""

    if isinstance(stored, datetime) and isinstance(value, datetime):
        return ensure_aware(stored) == ensure_aware(value)
    return stored == value


class NoteLedgerService:
    def __init__(self, session: Session, *, policy: dict | None = None) -> None:
        self._session = session
        self._policy = policy or review.load_policy()
        if not tables_ready(session):
            raise NoteLedgerError("note ledger tables are missing (migration a4a74a5bcb8b "
                                  "is not applied)")  # fmt: skip

    # -- 同期 ------------------------------------------------------------------------------------
    def sync_from_drafts(self, root: Path, *, execute: bool = False,
                         now: datetime | None = None) -> dict:  # fmt: skip
        now = ensure_aware(now or datetime.now(UTC))
        rows = {r.draft_id: r for r in self._session.scalars(select(NotePiece))}
        result = {"created": [], "updated": [], "unchanged": [], "conflicts": []}
        for path in sorted((root / DRAFTS).glob("*.json")):
            draft = review.draft_from_dict(json.loads(path.read_text(encoding="utf-8")))
            values = self._values(draft, now)
            row = rows.get(draft.id)
            if row is None:
                result["created"].append(draft.id)
                if execute:
                    self._session.add(NotePiece(draft_id=draft.id, **values))
                continue
            if row.status == "published" and (draft.status != "published"
                                              or values["published_url"] != row.published_url):
                result["conflicts"].append(
                    f"{draft.id}: the ledger says published ({row.published_url}); the draft "
                    f"file says {draft.status} ({values['published_url']}) — not synced")
                continue
            changed = {k: v for k, v in values.items()
                       if k != "synced_at" and not _same(getattr(row, k), v)}  # fmt: skip
            if not changed:
                result["unchanged"].append(draft.id)
                continue
            result["updated"].append(draft.id)
            if execute:
                for key, value in {**changed, "synced_at": now}.items():
                    setattr(row, key, value)
        if execute:
            self._session.commit()
        else:
            self._session.rollback()
        result["executed"] = execute
        return result

    @staticmethod
    def _values(draft, now: datetime) -> dict:
        approval, publication = draft.approval or {}, draft.publication or {}
        return {
            "candidate_id": draft.candidate_id, "content_type": draft.content_type,
            "title": draft.working_title[:300], "status": draft.status,
            "access_mode": draft.access_mode, "content_hash": draft.content_hash,
            "body_text": draft.body, "source_event_ids_json": list(draft.source_event_ids),
            "edited_by_human": bool(draft.edited_by_human),
            "approved_by": approval.get("approved_by"),
            "approved_at": _dt(approval.get("approved_at")),
            "approved_hash": approval.get("content_hash"),
            "approval_json": approval or None,
            "published_url": publication.get("url"),
            "published_observed_at": _dt(publication.get("observed_at")),
            "published_recorded_at": _dt(publication.get("recorded_at")),
            "draft_created_at": _dt(draft.created_at), "synced_at": now,
        }  # fmt: skip

    # -- 見る ------------------------------------------------------------------------------------
    def loop_status(self) -> dict:
        rows = list(self._session.scalars(select(NotePiece).order_by(NotePiece.id)))
        self._session.rollback()
        return {"by_status": dict(Counter(r.status for r in rows)),
                "pieces": [{"draft_id": r.draft_id, "title": r.title, "status": r.status,
                            "access_mode": r.access_mode, "published_url": r.published_url,
                            "next_step": NEXT_STEP[r.status]} for r in rows]}  # fmt: skip

    def cadence_plan(self, *, now: datetime | None = None, weeks: int = 4) -> dict:
        now = ensure_aware(now or datetime.now(UTC))
        guideline = int(self._policy.get("weekly_guideline", 2))
        published = [ensure_aware(r.published_observed_at) for r in self._session.scalars(
            select(NotePiece).where(NotePiece.status == "published"))
            if r.published_observed_at]  # fmt: skip
        self._session.rollback()
        this_monday = (now - timedelta(days=now.weekday())).date()
        rows = []
        for i in range(weeks - 1, -1, -1):
            start = this_monday - timedelta(weeks=i)
            count = sum(1 for p in published if start <= p.date() < start + timedelta(weeks=1))
            rows.append({"week_of": start.isoformat(), "published": count})
        waiting = self.loop_status()["by_status"]
        return {"guideline_per_week": guideline, "weeks": rows,
                "this_week_remaining_vs_guideline": max(0, guideline - rows[-1]["published"]),
                "ready_to_publish": waiting.get("approved", 0),
                "waiting_for_review": waiting.get("review_ready", 0),
                "note": "a guideline, not a quota: nothing is padded or published automatically"}

    def related_links(self, draft, *, limit: int = 3) -> list[dict]:
        articles = [{"id": a.id, "title": a.title, "url": a.published_url,
                     "keyword": a.keyword.keyword if a.keyword else None}
                    for a in self._session.scalars(select(Article).where(
                        Article.status == "published"))]  # fmt: skip
        self._session.rollback()
        convention = self._policy.get("link_convention", "none")
        found = note_links.related_articles(draft.working_title + draft.body, articles,
                                            limit=limit)  # fmt: skip
        for item in found:
            item["suggested_url"] = note_links.tagged_url(item["url"], convention=convention,
                                                          campaign=draft.id)  # fmt: skip
            item["convention"] = convention
        return found

    def published_bodies(self) -> dict[str, str]:
        rows = self._session.scalars(select(NotePiece).where(NotePiece.status == "published"))
        out = {f"note:{r.draft_id}": r.body_text for r in rows}
        self._session.rollback()
        return out


__all__ = ["NEXT_STEP", "NoteLedgerError", "NoteLedgerService", "tables_ready"]
