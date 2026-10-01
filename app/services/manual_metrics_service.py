"""手で写した数の記録 (N3。N4〜N7 も使う)。**provider には問い合わせない。推定しない。**

- ``record(entry, execute)``: 検査して 1 行足す。同じ対象・指標・期間・観測の時刻がすでにあり、
  値も同じなら何もしない (やり直してよい)。値が違えば止まる (直すときは ``supersedes`` で古い
  行を指す新しい行を足す。行は消さない)。
- ``active_rows()``: 上書きされていない行。
- ``summary(kind, metric)``: 標本の大きさつきの要約 (``app/n_track/metrics.summarize``)。
- ``record_many(entries, execute)``: 1 回の観測の数をまとめて記録する (全部を検査してから書く)。
- ``report(now)``: N3 の報告 (記事ごとの推移・公開からの日数・前の観測との差・観測の予定)。
  測る記事は ``app/config/note_measurement_targets.json`` (bootstrap と controlled を分ける)。
- 観測の時刻は UTC にそろえて保存する (``to_storage_utc``。台帳と同じ)。

表が無ければ (migration ``a4a74a5bcb8b`` の前) 止まる。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware, to_storage_utc
from app.models.n_track import MM_PROVENANCE_MANUAL, ManualMetricEntry, NotePiece
from app.n_track import metrics as mm
from app.services.note_ledger_service import NoteLedgerError, tables_ready

TARGETS_PATH = Path(__file__).resolve().parents[1] / "config" / "note_measurement_targets.json"


def load_targets(path: Path | None = None) -> dict:
    return json.loads((path or TARGETS_PATH).read_text(encoding="utf-8"))


class ManualMetricsService:
    def __init__(self, session: Session, *, targets: dict | None = None) -> None:
        self._session = session
        self._targets = targets if targets is not None else load_targets()
        if not tables_ready(session):
            raise NoteLedgerError("manual metric tables are missing (migration a4a74a5bcb8b "
                                  "is not applied)")  # fmt: skip

    def record(self, entry: mm.MetricInput, *, execute: bool = False,
               supersedes: int | None = None, now: datetime | None = None) -> dict:  # fmt: skip
        now = ensure_aware(now or datetime.now(UTC))
        unit = mm.validate(entry)
        if entry.subject_kind == "note_piece" and entry.subject_ref.startswith("external-"):
            # 台帳に無い (仕組みの外で公開した) 記事: 測る記事として登録したものだけ
            if entry.subject_ref not in {p["ref"] for p in self._targets.get("pieces", [])
                                         if not p.get("in_ledger")}:
                raise mm.MetricError(f"external piece {entry.subject_ref} is not a registered "
                                     "measurement target")  # fmt: skip
        elif entry.subject_kind == "note_piece" and self._session.scalars(select(NotePiece).where(
                NotePiece.draft_id == entry.subject_ref)).first() is None:  # fmt: skip
            raise mm.MetricError(f"note piece {entry.subject_ref} is not in the ledger "
                                 "(sync first)")  # fmt: skip
        key = mm.entry_key(entry)
        existing = self._session.scalars(select(ManualMetricEntry).where(
            ManualMetricEntry.entry_key == key)).first()  # fmt: skip
        if existing is not None:
            self._session.rollback()
            if existing.value == float(entry.value):
                return {"recorded": False, "reason": "already recorded", "id": existing.id}
            raise mm.MetricError(f"entry {existing.id} already has {existing.value} for this "
                                 "subject / metric / period / time; to correct it, record a new "
                                 "observation with --supersedes")  # fmt: skip
        if supersedes is not None:
            old = self._session.get(ManualMetricEntry, supersedes)
            if old is None or (old.subject_kind, old.subject_ref, old.metric) != (
                    entry.subject_kind, entry.subject_ref, entry.metric):  # fmt: skip
                raise mm.MetricError("--supersedes must point to an entry of the same "
                                     "subject and metric")  # fmt: skip
        row = ManualMetricEntry(
            entry_key=key, subject_kind=entry.subject_kind, subject_ref=entry.subject_ref,
            metric=entry.metric, value=float(entry.value), unit=unit,
            period_start=entry.period_start, period_end=entry.period_end,
            observed_at=to_storage_utc(entry.observed_at), provenance=MM_PROVENANCE_MANUAL,
            source_description=entry.source_description.strip()[:300],
            entered_by=entry.entered_by.strip()[:64], entered_at=now,
            note=(entry.note or None), supersedes_entry_id=supersedes)  # fmt: skip
        if not execute:
            self._session.rollback()
            return {"recorded": False, "reason": "PLAN (re-run with --execute)", "unit": unit}
        self._session.add(row)
        self._session.commit()
        return {"recorded": True, "id": row.id, "unit": unit}

    def active_rows(self) -> list[dict]:
        rows = list(self._session.scalars(select(ManualMetricEntry)))
        superseded = {r.supersedes_entry_id for r in rows if r.supersedes_entry_id}
        out = [{"id": r.id, "subject_kind": r.subject_kind, "subject_ref": r.subject_ref,
                "metric": r.metric, "value": r.value, "unit": r.unit,
                "period_start": r.period_start, "period_end": r.period_end,
                "observed_at": ensure_aware(r.observed_at), "source": r.source_description,
                "entered_by": r.entered_by} for r in rows if r.id not in superseded]  # fmt: skip
        self._session.rollback()
        return out

    def summary(self, kind: str, metric: str) -> dict:
        return mm.summarize(self.active_rows(), kind=kind, metric=metric)

    def record_many(self, entries: list[mm.MetricInput], *, execute: bool = False,
                    now: datetime | None = None) -> dict:  # fmt: skip
        """1 回の観測の数をまとめて記録する。先に全部を検査し、1 つでも拒まれれば何も書かない。"""

        checked = [self.record(e, execute=False, now=now) for e in entries]
        if not execute:
            return {"recorded": 0, "planned": len(entries), "results": checked,
                    "reason": "PLAN (re-run with --execute)"}  # fmt: skip
        results = [self.record(e, execute=True, now=now) for e in entries]
        return {"recorded": sum(1 for r in results if r.get("recorded")), "results": results}

    def report(self, *, now: datetime | None = None) -> dict:
        """N3 の報告。観測した点だけを並べる (推定・補間・勝ち負け・因果なし)。"""

        now = ensure_aware(now or datetime.now(UTC))
        rows = self.active_rows()
        ledger = {r.draft_id: r for r in self._session.scalars(select(NotePiece))}
        self._session.rollback()
        pieces, observed = [], []
        for target in self._targets.get("pieces", []):
            ref = target["ref"]
            published = datetime.fromisoformat(target["note_published_at"])
            entry = {"ref": ref, "role": target["role"], "url": target["url"],
                     "note_published_at": published.isoformat(), "metrics": {}}
            row = ledger.get(ref)
            if target.get("in_ledger") and (row is None or row.published_url != target["url"]):
                entry["warning"] = "the ledger does not have this piece at this URL"
            for metric in ("views", "likes", "comments"):
                series = mm.timeline(rows, kind="note_piece", ref=ref, metric=metric,
                                     published_at=published)  # fmt: skip
                entry["metrics"][metric] = series or "waiting for the first observation"
            observed += [r["observed_at"] for r in rows if r["subject_kind"] == "note_piece"
                         and r["subject_ref"] == ref]  # fmt: skip
            pieces.append(entry)
        channels = {}
        for name in self._targets.get("channels", []):
            series = mm.timeline(rows, kind="channel", ref=name, metric="followers")
            channels[name] = {"followers": series or "waiting for the first observation"}
            observed += [r["observed_at"] for r in rows if r["subject_kind"] == "channel"
                         and r["subject_ref"] == name]  # fmt: skip
        summaries = {m: mm.summarize(rows, kind="note_piece", metric=m)
                     for m in ("views", "likes", "comments")}  # fmt: skip
        return {
            "generated_at": now.isoformat(timespec="seconds"),
            "plan": mm.checkpoints(observed, now=now,
                                   cadence_days=self._targets.get("cadence_days", 7),
                                   count=self._targets.get("checkpoints", 4),
                                   window_days=self._targets.get("window_days", 3)),
            "pieces": pieces, "channels": channels,
            "summaries": {m: {k: s[k] for k in ("subjects", "entries", "span_days",
                                                "small_sample", "statement")}
                          for m, s in summaries.items()},
            "reading": ("observed values only (copied by a human); a running total is compared "
                        "with the previous observation; no estimate, ranking or causal claim"),
        }  # fmt: skip


__all__ = ["TARGETS_PATH", "ManualMetricsService", "load_targets"]
