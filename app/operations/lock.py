"""実行ロック (C8、T4.2 で原子的な回収に強化)。

スケジューラが前回の実行中に次の実行を開始してしまうと、同じ取り込みが二重に
走る。DB 上の 1 行で排他し、取得できなければ **成功ではなく skip/conflict として
記録** する。

クラッシュ耐性: プロセスが死ぬとロック行は解放されないまま残る。恒久デッドロック
を避けるため、``stale_after_minutes`` を過ぎた heartbeat は「所有者が死んだ」と
みなして回収する。回収したことは戻り値で分かるので、呼び出し側が事実として記録
できる (黙って奪わない)。

T4.2: 回収は **DB 上で原子的** に行う。

旧実装は「読んで、古ければ書く」だったため、古いロックの直後に 2 つの worker が
ほぼ同時に起動すると、どちらも「自分が回収した」と信じられた。いまは回収を
条件付き UPDATE (compare-and-swap) 1 文で行う::

    UPDATE operations_locks SET owner_token = <mine>, heartbeat_at = <now>, ...
    WHERE lock_name = :name
      AND (released_at IS NOT NULL OR heartbeat_at < :stale_cutoff)

最初の UPDATE が heartbeat を新しくするので、2 つ目の UPDATE の条件はもう成り立たず
0 行になる。**行数が 1 のときだけ取得できた** とみなす。プロセス内のロックには頼らない。

所有者の証明: 取得ごとに乱数の ``owner_token`` を発行する。heartbeat と解放は、
token (または C8 の run id) が一致したときだけ効く。他人のロックを延長したり
解放したりすることはできない。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
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
    #: 取得できた場合の所有者証明。heartbeat / 解放に使う。**ログに出さない。**
    owner_token: str | None = None
    #: 取得できなかった場合、現在の所有者の情報。
    blocking_owner_run_id: int | None = None
    blocking_owner_label: str | None = None
    blocking_acquired_at: datetime | None = None
    #: 古いロックを回収して取得した場合 True (黙って奪ったことにしない)。
    reclaimed_stale: bool = False
    #: 同時に取りに来た別の取得者に負けた場合 True。
    lost_race: bool = False


class OperationsLockService:
    """transaction owner。取得/解放のたびに commit する。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- acquire ---------------------------------------------------------------
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
        token = secrets.token_hex(16)

        row = self._observe(lock_name)
        if row is None:
            self._session.add(
                OperationsLock(
                    lock_name=lock_name,
                    owner_run_id=owner_run_id,
                    owner_label=owner_label,
                    owner_token=token,
                    acquired_at=stored_now,
                    heartbeat_at=stored_now,
                    released_at=None,
                )
            )
            try:
                self._session.commit()
            except IntegrityError:
                # 同時に別の取得者が先に行を作った。負けを事実として返す。
                self._session.rollback()
                return self._blocked(lock_name, lost_race=True)
            return LockOutcome(
                acquired=True,
                lock_name=lock_name,
                owner_run_id=owner_run_id,
                owner_label=owner_label,
                owner_token=token,
            )

        held = row.released_at is None
        stale = held and now - _as_utc(row.heartbeat_at) > timedelta(minutes=stale_after_minutes)
        if held and not stale:
            return self._blocked(lock_name, row=row)

        # compare-and-swap: 解放済みか、今この瞬間も古いときだけ書ける。
        cutoff = to_storage_utc(now - timedelta(minutes=stale_after_minutes))
        result = self._session.execute(
            update(OperationsLock)
            .where(
                OperationsLock.lock_name == lock_name,
                or_(
                    OperationsLock.released_at.is_not(None),
                    OperationsLock.heartbeat_at < cutoff,
                ),
            )
            .values(
                owner_run_id=owner_run_id,
                owner_label=owner_label,
                owner_token=token,
                acquired_at=stored_now,
                heartbeat_at=stored_now,
                released_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        self._session.commit()
        if result.rowcount != 1:
            return self._blocked(lock_name, lost_race=True)

        # 書けた行が本当に自分のものかを確かめる (二重の確認)。
        confirmed = self._observe(lock_name)
        if confirmed is None or confirmed.owner_token != token:
            return self._blocked(lock_name, lost_race=True)
        return LockOutcome(
            acquired=True,
            lock_name=lock_name,
            owner_run_id=owner_run_id,
            owner_label=owner_label,
            owner_token=token,
            reclaimed_stale=bool(stale),
        )

    # -- heartbeat / release ---------------------------------------------------
    def heartbeat(
        self,
        *,
        lock_name: str = DEFAULT_LOCK_NAME,
        owner_token: str | None = None,
        owner_run_id: int | None = None,
        now: datetime | None = None,
    ) -> bool:
        """自分が持っているロックだけを延長する。**持っていなければ False。**

        False が返ったら、所有権はもう無い (古いと判定されて誰かに回収された)。
        呼び出し側は仕事を止めなければならない。
        """

        result = self._session.execute(
            update(OperationsLock)
            .where(
                OperationsLock.lock_name == lock_name,
                OperationsLock.released_at.is_(None),
                self._owner_clause(owner_token, owner_run_id),
            )
            .values(heartbeat_at=to_storage_utc(now or datetime.now(UTC)))
            .execution_options(synchronize_session=False)
        )
        self._session.commit()
        return result.rowcount == 1

    def release(
        self,
        *,
        lock_name: str = DEFAULT_LOCK_NAME,
        owner_token: str | None = None,
        owner_run_id: int | None = None,
        now: datetime | None = None,
    ) -> bool:
        """自分が持っているロックだけを解放する (他人のロックは解放しない)。"""

        result = self._session.execute(
            update(OperationsLock)
            .where(
                OperationsLock.lock_name == lock_name,
                OperationsLock.released_at.is_(None),
                self._owner_clause(owner_token, owner_run_id),
            )
            .values(released_at=to_storage_utc(now or datetime.now(UTC)))
            .execution_options(synchronize_session=False)
        )
        self._session.commit()
        return result.rowcount == 1

    # -- internals -------------------------------------------------------------
    @staticmethod
    def _owner_clause(owner_token: str | None, owner_run_id: int | None):
        """所有者の証明なしには延長も解放もさせない。"""

        if owner_token is not None:
            return OperationsLock.owner_token == owner_token
        if owner_run_id is not None:
            return OperationsLock.owner_run_id == owner_run_id
        raise ValueError("an owner_token or owner_run_id is required to touch a held lock")

    def _observe(self, lock_name: str) -> OperationsLock | None:
        # 直前の UPDATE (Core) の結果を必ず DB から読み直す。
        self._session.expire_all()
        return self._session.scalars(
            select(OperationsLock).where(OperationsLock.lock_name == lock_name)
        ).first()

    def _blocked(
        self, lock_name: str, *, row: OperationsLock | None = None, lost_race: bool = False
    ) -> LockOutcome:
        row = row if row is not None else self._observe(lock_name)
        return LockOutcome(
            acquired=False,
            lock_name=lock_name,
            blocking_owner_run_id=row.owner_run_id if row else None,
            blocking_owner_label=row.owner_label if row else None,
            blocking_acquired_at=_as_utc(row.acquired_at) if row else None,
            lost_race=lost_race,
        )


def _as_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
