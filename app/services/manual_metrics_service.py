"""手で写した数の記録 (N3。N4〜N7 も使う)。**provider には問い合わせない。推定しない。**

- ``record(entry, execute)``: 検査して 1 行足す。同じ対象・指標・期間・観測の時刻がすでにあり、
  値も同じなら何もしない (やり直してよい)。値が違えば止まる (直すときは ``supersedes`` で古い
  行を指す新しい行を足す。行は消さない)。
- ``active_rows()``: 上書きされていない行。
- ``summary(kind, metric)``: 標本の大きさつきの要約 (``app/n_track/metrics.summarize``)。

表が無ければ (migration ``a4a74a5bcb8b`` の前) 止まる。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.article.fact_freshness import ensure_aware
from app.models.n_track import MM_PROVENANCE_MANUAL, ManualMetricEntry, NotePiece
from app.n_track import metrics as mm
from app.services.note_ledger_service import NoteLedgerError, tables_ready


class ManualMetricsService:
    def __init__(self, session: Session) -> None:
        self._session = session
        if not tables_ready(session):
            raise NoteLedgerError("manual metric tables are missing (migration a4a74a5bcb8b "
                                  "is not applied)")  # fmt: skip

    def record(self, entry: mm.MetricInput, *, execute: bool = False,
               supersedes: int | None = None, now: datetime | None = None) -> dict:  # fmt: skip
        now = ensure_aware(now or datetime.now(UTC))
        unit = mm.validate(entry)
        if entry.subject_kind == "note_piece" and self._session.scalars(select(NotePiece).where(
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
            observed_at=entry.observed_at, provenance=MM_PROVENANCE_MANUAL,
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


__all__ = ["ManualMetricsService"]
