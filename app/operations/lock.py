"""実行ロック (C8)。

スケジューラが前回の実行中に次の実行を開始してしまうと、同じ取り込みが二重に
走る。DB 上の 1 行で排他し、取得できなければ **成功ではなく skip/conflict として
記録** する。

クラッシュ耐性: プロセスが死ぬとロック行は解放されないまま残る。恒久デッドロック
を避けるため、``stale_after_minutes`` を過ぎた heartbeat は「所有者が死んだ」と
みなして回収する。回収したことは戻り値で分かるので、呼び出し側が事実として記録
できる (黙って奪わない)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.article.fact_freshness import to_storage_utc
from app.models import OperationsLock

DEFAULT_LOCK_NAME = "operations_pipeline"


@dataclass(frozen=True)
class LockOutcome:
    acquired: bool
    lock_name: str
    owner_run_id: int | None = None
    owner_label: str | None = None
    #: 取得できなかった場合、現在の所有者の情報。
    blocking_owner_run_id: int | None = None
    blocking_owner_label: str | None = None
    blocking_acquired_at: datetime | None = None
    #: 古いロックを回収して取得した場合 True (黙って奪ったことにしない)。
    reclaimed_stale: bool = False


class OperationsLockService:
    """transaction owner。取得/解放のたびに commit する。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def acquire(
        self,
        *,
        lock_name: str = DEFAULT_LOCK_NAME,
        owner_run_id: int | None = None,
        owner_label: str | None = None,
        stale_after_minutes: int = 120,
        now: datetime | None = None,
    ) -> LockOutcome:
        now = now or datetime.now(UTC)
        stored_now = to_storage_utc(now)
        row = self._session.scalars(
            select(OperationsLock).where(OperationsLock.lock_name == lock_name)
        ).first()

        if row is None:
            self._session.add(
                OperationsLock(
                    lock_name=lock_name,
                    owner_run_id=owner_run_id,
                    owner_label=owner_label,
                    acquired_at=stored_now,
                    heartbeat_at=stored_now,
                    released_at=None,
                )
            )
            self._session.commit()
            return LockOutcome(
                acquired=True,
                lock_name=lock_name,
                owner_run_id=owner_run_id,
                owner_label=owner_label,
            )

        held = row.released_at is None
        stale = False
        if held:
            heartbeat = _as_utc(row.heartbeat_at)
            stale = now - heartbeat > timedelta(minutes=stale_after_minutes)

        if held and not stale:
            return LockOutcome(
                acquired=False,
                lock_name=lock_name,
                blocking_owner_run_id=row.owner_run_id,
                blocking_owner_label=row.owner_label,
                blocking_acquired_at=_as_utc(row.acquired_at),
            )

        reclaimed = held and stale
        row.owner_run_id = owner_run_id
        row.owner_label = owner_label
        row.acquired_at = stored_now
        row.heartbeat_at = stored_now
        row.released_at = None
        self._session.commit()
        return LockOutcome(
            acquired=True,
            lock_name=lock_name,
            owner_run_id=owner_run_id,
            owner_label=owner_label,
            reclaimed_stale=reclaimed,
        )

    def heartbeat(self, *, lock_name: str = DEFAULT_LOCK_NAME, now: datetime | None = None) -> None:
        row = self._session.scalars(
            select(OperationsLock).where(OperationsLock.lock_name == lock_name)
        ).first()
        if row is None or row.released_at is not None:
            return
        row.heartbeat_at = to_storage_utc(now or datetime.now(UTC))
        self._session.commit()

    def release(
        self,
        *,
        lock_name: str = DEFAULT_LOCK_NAME,
        owner_run_id: int | None = None,
        now: datetime | None = None,
    ) -> bool:
        """自分が持っているロックだけを解放する (他人のロックは解放しない)。"""

        row = self._session.scalars(
            select(OperationsLock).where(OperationsLock.lock_name == lock_name)
        ).first()
        if row is None or row.released_at is not None:
            return False
        if owner_run_id is not None and row.owner_run_id != owner_run_id:
            return False
        row.released_at = to_storage_utc(now or datetime.now(UTC))
        self._session.commit()
        return True


def _as_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
