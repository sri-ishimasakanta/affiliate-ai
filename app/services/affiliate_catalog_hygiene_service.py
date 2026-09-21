"""AffiliateCatalogHygieneService — 宣言した match_terms 削除を PLAN / EXECUTE で適用する。

- :meth:`plan` は **SELECT だけ** (write 0 / commit 0)。session が pending 変更を持つと flush 前に
  例外で止まる (:func:`app.services.content_queue_service.read_only_session`)。
- :meth:`execute` は **全件 preflight → 1 transaction で適用**。1 件でも ``drift`` /
  ``program_not_found`` なら何も書かない (all-or-nothing)。各 program の現在値が ``before`` と
  完全一致することを、書き込む直前にもう一度 (同じ transaction 内で) 確認する。冪等: 適用済み
  (現在値 == ``after``) は何もせず commit も呼ばない。
- 触るのは ``match_terms`` だけ。score / signal / article / link / status には一切触れない。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.affiliate.catalog_hygiene import (
    STATUS_APPLIED,
    STATUS_DRIFT,
    STATUS_NOT_FOUND,
    STATUS_PENDING,
    HygieneSpec,
    TermRemoval,
    evaluate_change,
)
from app.repositories.affiliate_program_repository import AffiliateProgramRepository
from app.services.content_queue_service import read_only_session


class HygieneRefusedError(RuntimeError):
    """drift / program 不在のため適用を拒否した (何も書いていない)。"""

    def __init__(self, blocked: list[HygieneOutcome]) -> None:
        self.blocked = tuple(blocked)
        detail = ", ".join(f"{o.change.program} ({o.status})" for o in blocked)
        super().__init__(f"refusing to apply: {detail}")


@dataclass(frozen=True)
class HygieneOutcome:
    change: TermRemoval
    status: str  # pending | already_applied | drift | program_not_found
    program_id: int | None
    current: tuple[str, ...] | None

    @property
    def will_remove(self) -> tuple[str, ...]:
        return self.change.remove if self.status == STATUS_PENDING else ()


@dataclass(frozen=True)
class HygienePlan:
    outcomes: tuple[HygieneOutcome, ...]

    def _count(self, status: str) -> int:
        return sum(1 for o in self.outcomes if o.status == status)

    @property
    def pending(self) -> int:
        return self._count(STATUS_PENDING)

    @property
    def already_applied(self) -> int:
        return self._count(STATUS_APPLIED)

    @property
    def blocked(self) -> list[HygieneOutcome]:
        return [o for o in self.outcomes if o.status in (STATUS_DRIFT, STATUS_NOT_FOUND)]

    @property
    def executable(self) -> bool:
        return not self.blocked

    @property
    def terms_to_remove(self) -> int:
        return sum(len(o.will_remove) for o in self.outcomes)


@dataclass(frozen=True)
class HygieneResult:
    plan: HygienePlan
    applied: tuple[str, ...]  # 実際に書き換えた change id
    skipped_already_applied: tuple[str, ...]


class AffiliateCatalogHygieneService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._repo = AffiliateProgramRepository(session)

    def plan(self, spec: HygieneSpec) -> HygienePlan:
        with read_only_session(self._session):
            outcomes = tuple(self._evaluate(change) for change in spec.changes)
        return HygienePlan(outcomes)

    def execute(self, spec: HygieneSpec) -> HygieneResult:
        plan = self.plan(spec)
        if plan.blocked:
            raise HygieneRefusedError(plan.blocked)  # 何も書かない
        pending = [o for o in plan.outcomes if o.status == STATUS_PENDING]
        already = tuple(o.change.id for o in plan.outcomes if o.status == STATUS_APPLIED)
        if not pending:
            return HygieneResult(plan, (), already)  # 冪等: write 0 / commit なし
        try:
            for outcome in pending:
                entity = self._repo.get_by_name_and_provider(
                    outcome.change.program, outcome.change.provider
                )
                # 書き込む直前 (同じ transaction 内) に、exact な before をもう一度確認する
                if entity is None or evaluate_change(
                    outcome.change, tuple(entity.match_terms or ())
                ) != (STATUS_PENDING):
                    raise HygieneRefusedError([outcome])
                self._repo.update(entity, {"match_terms": list(outcome.change.after)})
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        self._session.expire_all()  # in-memory ではなく DB に保存された値を読み直す
        for outcome in pending:  # commit 後の状態が after と一致することを確認する
            entity = self._repo.get_by_name_and_provider(
                outcome.change.program, outcome.change.provider
            )
            if entity is None or tuple(entity.match_terms or ()) != outcome.change.after:
                raise RuntimeError(f"post-check failed for {outcome.change.id!r}")
        return HygieneResult(plan, tuple(o.change.id for o in pending), already)

    def _evaluate(self, change: TermRemoval) -> HygieneOutcome:
        entity = self._repo.get_by_name_and_provider(change.program, change.provider)
        if entity is None:
            return HygieneOutcome(change, STATUS_NOT_FOUND, None, None)
        current = tuple(entity.match_terms or ())
        return HygieneOutcome(change, evaluate_change(change, current), entity.id, current)
