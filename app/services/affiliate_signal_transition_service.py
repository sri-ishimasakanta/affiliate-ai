"""AffiliateSignalTransitionService — affiliate_opportunity の再導出と再スコア。

PLAN / EXECUTE の 2 モード。

catalog hygiene (``AffiliateCatalogHygieneService``) を適用したあと、live catalog と食い違って
いる affiliate_opportunity Signal を導出し直し、**その値が変わった既存 score だけ** を付け直す
ための transition。PLAN / EXECUTE の規約は catalog hygiene と揃える。

- :meth:`plan` は **SELECT だけ** (write 0 / commit 0)。新しい値は
  :meth:`KeywordSignalService.preview_affiliate_opportunity` で **計算するだけ** で保存しない
  (EXECUTE が保存する値と同じ計算を共有するので、PLAN と結果が食い違わない)。
- :meth:`execute` は preflight (期待値の検証) を **書き込み前に全て済ませ**、そのあと
  A: 30 件の導出 → B: 再スコア、の順で通常の service path を通す。A が完走しなければ B は
  1 件も走らない (部分的な scoring を作らない)。各 phase のあとに行数を検証し、想定と 1 行でも
  違えば :class:`TransitionRefusedError` で止める (黙って続けない)。
- 再スコアの対象は **DB から決める** (ID を直書きしない): 既に score があり、新しい
  affiliate_opportunity が **その score に入っている値** と違い、7 component が揃っているもの。
- 冪等: PLAN が「変わるものが無い」と判定したら EXECUTE は何も書かずに ``already_current`` で
  返る (2 回目の実行が同じ history を重ねて作らない)。
- scoring の式 / 重み / fit policy / catalog は変更しない。外部 API / LLM / network は使わない。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.keyword.scoring import (
    COMPONENT_NAMES,
    OpportunityScoreInput,
    calculate_opportunity_score,
)
from app.models import KeywordScore, KeywordScoreSignal, KeywordSignal
from app.repositories.keyword_repository import KeywordRepository
from app.repositories.keyword_score_repository import KeywordScoreRepository
from app.repositories.keyword_signal_repository import KeywordSignalRepository
from app.services.content_queue_service import read_only_session
from app.services.keyword_scoring_service import KeywordScoringService
from app.services.keyword_signal_service import KeywordSignalService

#: pool は DB の全 keyword。実運用の規模より十分大きい上限を置くだけ
_POOL_LIMIT = 100_000

#: affiliate_opportunity の値が「変わった」とみなす差 (正規化値は小数第 2 位まで)
VALUE_EPSILON = 1e-9

AFFILIATE_COMPONENT = "affiliate_opportunity"
#: 再スコアに必要な affiliate 以外の component
OTHER_COMPONENTS: tuple[str, ...] = tuple(
    name for name in COMPONENT_NAMES if name != AFFILIATE_COMPONENT
)

DECISION_RESCORE = "rescore"
DECISION_UNCHANGED = "unchanged_scored"
DECISION_NEVER_SCORED = "never_scored"
DECISION_BLOCKED_INCOMPLETE = "blocked_incomplete_signals"


class TransitionRefusedError(RuntimeError):
    """preflight / 事後検証に失敗したため止めた。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class KeywordOutcome:
    """1 keyword 分の transition 判定 (PLAN)。"""

    keyword_id: int
    keyword: str
    #: 保存済みの最新 affiliate_opportunity Signal の値 (未導出なら None)
    current_signal_value: float | None
    #: 最新 score に入っている affiliate_opportunity (未 score なら None)
    current_score_value: float | None
    #: これから導出される値 (計算しただけ、保存していない)
    proposed_value: float
    current_total: float | None
    proposed_total: float | None
    missing_components: tuple[str, ...]
    decision: str

    @property
    def has_score(self) -> bool:
        return self.current_score_value is not None

    @property
    def value_changed(self) -> bool:
        """最新 score に入っている値と、これから導出する値が違うか。"""

        if self.current_score_value is None:
            return False
        return abs(self.proposed_value - self.current_score_value) > VALUE_EPSILON

    @property
    def signal_changed(self) -> bool:
        """保存済みの最新 Signal と、これから導出する値が違うか (報告用)。"""

        if self.current_signal_value is None:
            return True
        return abs(self.proposed_value - self.current_signal_value) > VALUE_EPSILON


@dataclass(frozen=True)
class TransitionPlan:
    outcomes: tuple[KeywordOutcome, ...]

    def _with(self, decision: str) -> list[KeywordOutcome]:
        return [o for o in self.outcomes if o.decision == decision]

    @property
    def keyword_count(self) -> int:
        return len(self.outcomes)

    @property
    def rescore(self) -> list[KeywordOutcome]:
        return self._with(DECISION_RESCORE)

    @property
    def rescore_ids(self) -> tuple[int, ...]:
        return tuple(o.keyword_id for o in self.rescore)

    @property
    def unchanged_scored(self) -> list[KeywordOutcome]:
        return self._with(DECISION_UNCHANGED)

    @property
    def never_scored(self) -> list[KeywordOutcome]:
        return self._with(DECISION_NEVER_SCORED)

    @property
    def blocked(self) -> list[KeywordOutcome]:
        return self._with(DECISION_BLOCKED_INCOMPLETE)

    @property
    def signal_value_changes(self) -> int:
        """保存済み Signal と違う値になる keyword 数 (score の有無に関わらず)。"""

        return sum(1 for o in self.outcomes if o.signal_changed)

    @property
    def expected_signal_inserts(self) -> int:
        """EXECUTE で追記される affiliate_opportunity Signal 行数 (pool 全件)。"""

        return self.keyword_count

    @property
    def expected_score_inserts(self) -> int:
        return len(self.rescore)

    @property
    def expected_score_signal_inserts(self) -> int:
        return len(self.rescore) * len(COMPONENT_NAMES)

    @property
    def has_work(self) -> bool:
        """再導出も再スコアも要らない状態か (冪等 guard)。"""

        return bool(self.rescore) or self.signal_value_changes > 0

    def ranking(self) -> list[tuple[int, str, int | None, int | None]]:
        """``(keyword_id, keyword, current_rank, proposed_rank)`` を現在順位で返す。

        score を持つ keyword だけを対象にする (順位は total_score 降順、同点は id 昇順)。
        """

        scored = [o for o in self.outcomes if o.current_total is not None]
        current = sorted(scored, key=lambda o: (-o.current_total, o.keyword_id))
        proposed = sorted(
            scored,
            key=lambda o: (-(o.proposed_total if o.proposed_total is not None
                             else o.current_total), o.keyword_id),
        )
        cur_rank = {o.keyword_id: n for n, o in enumerate(current, 1)}
        new_rank = {o.keyword_id: n for n, o in enumerate(proposed, 1)}
        return [
            (o.keyword_id, o.keyword, cur_rank[o.keyword_id], new_rank[o.keyword_id])
            for o in current
        ]

    @property
    def rank_changes(self) -> list[tuple[int, str, int | None, int | None]]:
        return [row for row in self.ranking() if row[2] != row[3]]


@dataclass(frozen=True)
class TransitionResult:
    plan: TransitionPlan
    already_current: bool
    derived_keyword_ids: tuple[int, ...]
    rescored_keyword_ids: tuple[int, ...]
    inserted_signals: int
    inserted_scores: int
    inserted_score_signals: int


class AffiliateSignalTransitionService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._keywords = KeywordRepository(session)
        self._signals = KeywordSignalRepository(session)
        self._scores = KeywordScoreRepository(session)
        self._signal_service = KeywordSignalService(session)
        self._scoring_service = KeywordScoringService(session)

    # -- PLAN (write 0) -------------------------------------------------
    def plan(self) -> TransitionPlan:
        with read_only_session(self._session):
            outcomes = tuple(self._evaluate(kid) for kid in self._pool_ids())
        return TransitionPlan(outcomes)

    # -- EXECUTE --------------------------------------------------------
    def execute(
        self,
        *,
        expect_keywords: int | None = None,
        expect_rescores: int | None = None,
        allow_blocked: bool = False,
    ) -> TransitionResult:
        plan = self.plan()
        self.assert_expectations(
            plan,
            expect_keywords=expect_keywords,
            expect_rescores=expect_rescores,
            allow_blocked=allow_blocked,
        )

        if not plan.has_work:
            # 冪等: 既に現在の catalog と一致している。history を重ねない
            return TransitionResult(plan, True, (), (), 0, 0, 0)

        pool = [o.keyword_id for o in plan.outcomes]
        before = self._counts()

        # --- phase A: 再導出 (pool 全件)。ここが完走しなければ scoring は 1 件も走らない
        derived: list[int] = []
        try:
            for keyword_id in pool:
                self._signal_service.derive_affiliate_opportunity(keyword_id)
                derived.append(keyword_id)
        except Exception:
            self._session.rollback()
            raise
        after_a = self._counts()
        self._assert_delta(
            before, after_a, "derivation",
            signals=len(pool), scores=0, score_signals=0,
        )
        self._assert_only_affiliate_signals_added(before["max_signal_id"], len(pool))

        # --- 再スコア対象を DB から決め直す (PLAN と一致しなければ書かずに止める)
        self._session.expire_all()
        recomputed = tuple(
            o.keyword_id
            for o in (self._evaluate(kid) for kid in pool)
            if o.decision == DECISION_RESCORE
        )
        if recomputed != plan.rescore_ids:
            raise TransitionRefusedError(
                "the re-score set changed between PLAN and the post-derivation state "
                f"(plan {list(plan.rescore_ids)}, now {list(recomputed)}); nothing was scored"
            )

        # --- phase B: 再スコア
        rescored: list[int] = []
        try:
            for keyword_id in plan.rescore_ids:
                self._scoring_service.score_keyword_from_latest_signals(keyword_id)
                rescored.append(keyword_id)
        except Exception:
            self._session.rollback()
            raise
        after_b = self._counts()
        self._assert_delta(
            after_a, after_b, "scoring",
            signals=0,
            scores=len(plan.rescore_ids),
            score_signals=len(plan.rescore_ids) * len(COMPONENT_NAMES),
        )

        return TransitionResult(
            plan=plan,
            already_current=False,
            derived_keyword_ids=tuple(derived),
            rescored_keyword_ids=tuple(rescored),
            inserted_signals=after_b["signals"] - before["signals"],
            inserted_scores=after_b["scores"] - before["scores"],
            inserted_score_signals=after_b["score_signals"] - before["score_signals"],
        )

    # -- internals ------------------------------------------------------
    def _pool_ids(self) -> list[int]:
        """対象 keyword pool = DB の全 keyword (id 昇順・決定論)。"""

        return [k.id for k in self._keywords.list(limit=_POOL_LIMIT, offset=0)]

    def _evaluate(self, keyword_id: int) -> KeywordOutcome:
        computed = self._signal_service.preview_affiliate_opportunity(keyword_id)
        latest_signal = self._signals.get_latest(keyword_id, AFFILIATE_COMPONENT)
        latest_score = self._scores.get_latest(keyword_id)
        missing = tuple(
            name
            for name in OTHER_COMPONENTS
            if self._signals.get_latest(keyword_id, name) is None
        )

        outcome = KeywordOutcome(
            keyword_id=keyword_id,
            keyword=computed.keyword,
            current_signal_value=(
                latest_signal.normalized_value if latest_signal is not None else None
            ),
            current_score_value=(
                latest_score.affiliate_opportunity if latest_score is not None else None
            ),
            proposed_value=computed.normalized_value,
            current_total=latest_score.total_score if latest_score is not None else None,
            proposed_total=None,
            missing_components=missing,
            decision=DECISION_NEVER_SCORED,
        )
        if latest_score is None:
            return outcome
        if not outcome.value_changed:
            return replace(outcome, decision=DECISION_UNCHANGED)
        if missing:
            # score はあるが 7 component が揃っていない: 付け直せない (黙って飛ばさず報告する)
            return replace(outcome, decision=DECISION_BLOCKED_INCOMPLETE)
        return replace(
            outcome,
            decision=DECISION_RESCORE,
            proposed_total=self._proposed_total(keyword_id, computed.normalized_value),
        )

    def _proposed_total(self, keyword_id: int, affiliate_value: float) -> float:
        values = {AFFILIATE_COMPONENT: affiliate_value}
        for name in OTHER_COMPONENTS:
            signal = self._signals.get_latest(keyword_id, name)
            values[name] = signal.normalized_value
        return calculate_opportunity_score(OpportunityScoreInput(**values)).total

    def _counts(self) -> dict[str, int]:
        scalar = self._session.scalar
        return {
            "signals": int(scalar(select(func.count()).select_from(KeywordSignal)) or 0),
            "scores": int(scalar(select(func.count()).select_from(KeywordScore)) or 0),
            "score_signals": int(
                scalar(select(func.count()).select_from(KeywordScoreSignal)) or 0
            ),
            "max_signal_id": int(scalar(select(func.max(KeywordSignal.id))) or 0),
        }

    def _assert_delta(
        self, before: dict, after: dict, phase: str, *, signals: int, scores: int,
        score_signals: int,
    ) -> None:
        actual = {
            "signals": after["signals"] - before["signals"],
            "scores": after["scores"] - before["scores"],
            "score_signals": after["score_signals"] - before["score_signals"],
        }
        expected = {"signals": signals, "scores": scores, "score_signals": score_signals}
        if actual != expected:
            raise TransitionRefusedError(
                f"unexpected row counts after {phase}: expected {expected}, got {actual}"
            )

    def _assert_only_affiliate_signals_added(self, max_id_before: int, count: int) -> None:
        rows = self._session.scalars(
            select(KeywordSignal).where(KeywordSignal.id > max_id_before)
        ).all()
        if len(rows) != count:
            raise TransitionRefusedError(
                f"expected {count} new signal rows, found {len(rows)}"
            )
        other = sorted({str(r.component) for r in rows} - {AFFILIATE_COMPONENT})
        if other:
            raise TransitionRefusedError(
                f"derivation inserted non-affiliate signal components: {other}"
            )

    @staticmethod
    def assert_expectations(
        plan: TransitionPlan, *, expect_keywords: int | None = None,
        expect_rescores: int | None = None, allow_blocked: bool = False,
    ) -> None:
        """transition 固有の guard。満たさなければ :class:`TransitionRefusedError`。

        PLAN からも呼べる (書き込み前の検証なので、PLAN では報告だけに使える)。
        """

        if expect_keywords is not None and plan.keyword_count != expect_keywords:
            raise TransitionRefusedError(
                f"keyword pool is {plan.keyword_count}, expected {expect_keywords}; "
                "nothing was written"
            )
        if expect_rescores is not None and len(plan.rescore) != expect_rescores:
            raise TransitionRefusedError(
                f"re-score set is {len(plan.rescore)} keyword(s) "
                f"{list(plan.rescore_ids)}, expected {expect_rescores}; nothing was written"
            )
        if plan.blocked and not allow_blocked:
            names = ", ".join(f"{o.keyword_id}:{o.keyword}" for o in plan.blocked)
            raise TransitionRefusedError(
                "these keywords have a stored score whose affiliate_opportunity would change "
                f"but are missing required signals, so the score cannot be refreshed: {names}. "
                "Collect the missing signals, or re-run with allow_blocked to derive anyway"
            )
