"""承認済み投稿案の queue 評価 (T4.1、pure、**公開はしない**)。

**承認は公開ではない。** 人が承認したのは「この文面を出してよい」ことであって、
「今すぐ出せ」ではない。承認済みの提案は queue に入り、**いつ・どれを** 出すかは
ここで別に判定する。

この判定は:

- 不透明な重み付きスコアを使わない。名前の付いた事実と理由だけで並べる。
- 何も変わっていなければ、同じ順序を返す (純関数・安定ソート)。
- 若い投稿の指標を順位の根拠にしない (T4.1 では指標を順位に一切使わない)。
- 弱い信号 (直前と同じ切り口・同じ記事) は **並び順を少し後ろにするだけ** で、
  候補から外さない。しかも承認から ``starvation_guard_hours`` を過ぎた候補には
  効かない。常緑の提案が弱い信号だけで永遠に後回しにならないようにするため。
- 強いブロッカー (間隔・公開窓・不確定な公開・中身の不整合・stale) だけが
  公開を止める。
- **1 回の評価で選ぶのは最大 1 件。** 資格のある候補をまとめて出す API は無い。
  出したら状態が変わるので、次の 1 件は改めて評価し直す。

T4.1 では自動公開そのものが無効 (``AUTOMATIC_PUBLICATION_ENABLED = False``)。
評価結果の ``would_publish_now`` は常に False になる。

T4.2 で人の queue 操作と時刻の制約が加わった:

- **保留 (hold)** は強いブロッカー。解除 (release) するまで選ばれない。
- **not_before** より前は資格が無い。**expires_at** を過ぎたら資格が無い (消さない)。
- **次に優先 (prefer_next)** は並び順の先頭に来るだけ。**安全の条件を 1 つも飛ばさない。**
  優先した提案がブロックされていれば、その理由を示し、他の候補を通常どおり評価する。
- 常緑の提案には期限が無い。古いというだけで期限切れにはしない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.operations.local_time import to_local
from app.social.threads.policy import ThreadsOperationsPolicy
from app.social.threads.schedule import PublicationTiming, publication_timing

#: T4.1 の境界。自動公開は **コードで** 無効にしてある (設定では有効にできない)。
AUTOMATIC_PUBLICATION_ENABLED = False
#: 1 回の評価サイクルで公開してよい最大件数。**決して増やさない。**
MAX_PUBLICATIONS_PER_CYCLE = 1

# -- candidate reasons (1 件の提案について) -----------------------------------
REASON_ELIGIBLE = "eligible"
REASON_NOT_APPROVED = "not_approved"
REASON_ALREADY_PUBLISHED = "already_published"
REASON_STALE = "stale"
REASON_CONTENT_INTEGRITY = "content_integrity"
REASON_UNCERTAIN_PUBLICATION = "uncertain_publication"
#: T4.2: 人が保留にしている。
REASON_HELD = "held"
#: T4.2: not_before より前。
REASON_NOT_BEFORE = "not_before"
#: T4.2: expires_at を過ぎた。**消さない。** 理由を残して資格だけを失う。
REASON_EXPIRED = "expired"
CANDIDATE_REASONS = (
    REASON_ELIGIBLE,
    REASON_NOT_APPROVED,
    REASON_ALREADY_PUBLISHED,
    REASON_STALE,
    REASON_CONTENT_INTEGRITY,
    REASON_UNCERTAIN_PUBLICATION,
    REASON_HELD,
    REASON_NOT_BEFORE,
    REASON_EXPIRED,
)

# -- global blockers (queue 全体を止める) ---------------------------------------
BLOCKER_THREADS_DISABLED = "threads_disabled"
BLOCKER_THREADS_MISCONFIGURED = "threads_misconfigured"
BLOCKER_UNCERTAIN_PUBLICATION = "uncertain_publication"
BLOCKER_GAP_NOT_ELAPSED = "gap_not_elapsed"
BLOCKER_OUTSIDE_PUBLICATION_WINDOW = "outside_publication_window"
BLOCKER_NO_ELIGIBLE_CANDIDATE = "no_eligible_candidate"
BLOCKER_AUTOMATIC_PUBLICATION_DISABLED = "automatic_publication_disabled"
GLOBAL_BLOCKERS = (
    BLOCKER_THREADS_DISABLED,
    BLOCKER_THREADS_MISCONFIGURED,
    BLOCKER_UNCERTAIN_PUBLICATION,
    BLOCKER_GAP_NOT_ELAPSED,
    BLOCKER_OUTSIDE_PUBLICATION_WINDOW,
    BLOCKER_NO_ELIGIBLE_CANDIDATE,
    BLOCKER_AUTOMATIC_PUBLICATION_DISABLED,
)
#: 設定が壊れている・公開が不確定、など **人が直す必要がある** ブロッカー。
#: 「無効にしてある」「間隔待ち」「夜」は健全な待ちであって問題ではない。
PROBLEM_BLOCKERS = frozenset({BLOCKER_THREADS_MISCONFIGURED, BLOCKER_UNCERTAIN_PUBLICATION})

# -- evidence ------------------------------------------------------------------
EVIDENCE_INSUFFICIENT = "insufficient_evidence"
EVIDENCE_USABLE = "usable"

_THREADS_STATE_BLOCKERS = {
    "disabled": BLOCKER_THREADS_DISABLED,
    "misconfigured": BLOCKER_THREADS_MISCONFIGURED,
}


@dataclass(frozen=True)
class CandidateFacts:
    """1 件の提案について、DB から集めた事実だけ (判断は含めない)。"""

    proposal_id: int
    status: str
    angle: str
    source_article_id: int
    link_mode: str
    approved_at: datetime | None
    stale_reasons: tuple[str, ...] = ()
    integrity_reasons: tuple[str, ...] = ()
    already_published: bool = False
    in_flight: bool = False
    held: bool = False
    not_before: datetime | None = None
    expires_at: datetime | None = None
    preferred_at: datetime | None = None


@dataclass(frozen=True)
class QueueFacts:
    now: datetime
    threads_state: str
    candidates: tuple[CandidateFacts, ...]
    last_published_at: datetime | None = None
    last_published_angle: str | None = None
    last_published_article_id: int | None = None
    published_today: int = 0
    uncertain_publication_ids: tuple[int, ...] = ()
    mature_post_count: int = 0
    minimum_mature_posts: int = 3


@dataclass(frozen=True)
class CandidateVerdict:
    proposal_id: int
    reason: str
    angle: str
    source_article_id: int
    approved_at: datetime | None
    details: tuple[str, ...] = ()
    #: 弱い信号。**候補から外す理由にはならない。** 並び順にだけ効く。
    repeats_last_angle: bool = False
    repeats_last_article: bool = False
    starvation_guard_active: bool = False
    #: 人が「次に優先」を指定している (安全の条件は飛ばさない)。
    preferred: bool = False
    preferred_at: datetime | None = None
    not_before: datetime | None = None
    expires_at: datetime | None = None

    @property
    def eligible(self) -> bool:
        return self.reason == REASON_ELIGIBLE

    def as_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "reason": self.reason,
            "angle": self.angle,
            "source_article_id": self.source_article_id,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "details": list(self.details),
            "repeats_last_angle": self.repeats_last_angle,
            "repeats_last_article": self.repeats_last_article,
            "starvation_guard_active": self.starvation_guard_active,
            "preferred": self.preferred,
            "not_before": self.not_before.isoformat() if self.not_before else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


@dataclass(frozen=True)
class QueueEvaluation:
    now: datetime
    blockers: tuple[str, ...]
    candidates: tuple[CandidateVerdict, ...]
    #: 次に出すならこの 1 件。**複数形の API は意図的に存在しない。**
    next_candidate: CandidateVerdict | None
    timing: PublicationTiming
    next_evaluation_at: datetime
    evidence_state: str
    ordering_basis: str
    publication_enabled: bool = AUTOMATIC_PUBLICATION_ENABLED
    max_publications_per_cycle: int = MAX_PUBLICATIONS_PER_CYCLE
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def would_publish_now(self) -> bool:
        return self.publication_enabled and not self.blockers and self.next_candidate is not None

    @property
    def eligible_count(self) -> int:
        return sum(1 for c in self.candidates if c.eligible)

    @property
    def problems(self) -> tuple[str, ...]:
        return tuple(b for b in self.blockers if b in PROBLEM_BLOCKERS)

    def as_dict(self, tz: ZoneInfo) -> dict:
        return {
            "now": self.now.isoformat(),
            "now_local": to_local(self.now, tz).isoformat(),
            "blockers": list(self.blockers),
            "problems": list(self.problems),
            "eligible_count": self.eligible_count,
            "next_candidate": self.next_candidate.as_dict() if self.next_candidate else None,
            "candidates": [c.as_dict() for c in self.candidates],
            "timing": self.timing.as_dict(tz),
            "next_evaluation_at": self.next_evaluation_at.isoformat(),
            "next_evaluation_local": to_local(self.next_evaluation_at, tz).isoformat(),
            "evidence_state": self.evidence_state,
            "ordering_basis": self.ordering_basis,
            "publication_enabled": self.publication_enabled,
            "would_publish_now": self.would_publish_now,
            "max_publications_per_cycle": self.max_publications_per_cycle,
            "notes": list(self.notes),
        }


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _verdict(
    facts: CandidateFacts, queue: QueueFacts, policy: ThreadsOperationsPolicy
) -> CandidateVerdict:
    base = {
        "proposal_id": facts.proposal_id,
        "angle": facts.angle,
        "source_article_id": facts.source_article_id,
        "approved_at": _aware(facts.approved_at),
        "preferred": facts.preferred_at is not None,
        "preferred_at": _aware(facts.preferred_at),
        "not_before": _aware(facts.not_before),
        "expires_at": _aware(facts.expires_at),
    }
    now = _aware(queue.now)
    # 強い理由から順に 1 つだけ採る (理由の優先順位は固定)。
    if facts.already_published:
        return CandidateVerdict(reason=REASON_ALREADY_PUBLISHED, **base)
    if facts.in_flight:
        return CandidateVerdict(reason=REASON_UNCERTAIN_PUBLICATION, **base)
    if facts.status != "approved":
        return CandidateVerdict(reason=REASON_NOT_APPROVED, details=(facts.status,), **base)
    if facts.integrity_reasons:
        return CandidateVerdict(
            reason=REASON_CONTENT_INTEGRITY, details=facts.integrity_reasons, **base
        )
    if facts.stale_reasons:
        return CandidateVerdict(reason=REASON_STALE, details=facts.stale_reasons, **base)
    expires_at = _aware(facts.expires_at)
    if expires_at is not None and expires_at <= now:
        return CandidateVerdict(
            reason=REASON_EXPIRED, details=(f"expired at {expires_at.isoformat()}",), **base
        )
    if facts.held:
        return CandidateVerdict(reason=REASON_HELD, **base)
    not_before = _aware(facts.not_before)
    if not_before is not None and now < not_before:
        return CandidateVerdict(
            reason=REASON_NOT_BEFORE, details=(f"not before {not_before.isoformat()}",), **base
        )

    approved_at = _aware(facts.approved_at)
    guard = approved_at is not None and queue.now - approved_at >= timedelta(
        hours=policy.starvation_guard_hours
    )
    repeats_angle = (
        queue.last_published_angle is not None and facts.angle == queue.last_published_angle
    )
    repeats_article = (
        queue.last_published_article_id is not None
        and facts.source_article_id == queue.last_published_article_id
    )
    return CandidateVerdict(
        reason=REASON_ELIGIBLE,
        repeats_last_angle=repeats_angle,
        repeats_last_article=repeats_article,
        starvation_guard_active=guard,
        **base,
    )


def _order_key(verdict: CandidateVerdict) -> tuple:
    """並び順。**スコアではなく、名前の付いた事実の辞書式比較。**

    この関数に来るのは **資格のある候補だけ**。だから「優先」が安全の条件を
    飛ばすことは構造上ありえない。

    0. 人が「次に優先」を指定したか (指定した順)
    1. 直前と同じ記事か (弱い信号。starvation guard 中は無視)
    2. 直前と同じ切り口か (弱い信号。starvation guard 中は無視)
    3. 承認が古い順 (編集上の順序。何も変わらなければ順序も変わらない)
    4. proposal id (完全に決定的にするため)
    """

    soft = not verdict.starvation_guard_active
    far = datetime.max.replace(tzinfo=UTC)
    approved = verdict.approved_at or far
    return (
        not verdict.preferred,
        verdict.preferred_at or far,
        soft and verdict.repeats_last_article,
        soft and verdict.repeats_last_angle,
        approved,
        verdict.proposal_id,
    )


def evaluate_queue(
    queue: QueueFacts, policy: ThreadsOperationsPolicy, tz: ZoneInfo
) -> QueueEvaluation:
    now = _aware(queue.now) or datetime.now(UTC)
    verdicts = [_verdict(c, queue, policy) for c in queue.candidates]
    eligible = sorted((v for v in verdicts if v.eligible), key=_order_key)
    others = sorted((v for v in verdicts if not v.eligible), key=lambda v: v.proposal_id)

    timing = publication_timing(
        now=now, last_published_at=queue.last_published_at, policy=policy, tz=tz
    )

    blockers: list[str] = []
    state_blocker = _THREADS_STATE_BLOCKERS.get(queue.threads_state)
    if state_blocker:
        blockers.append(state_blocker)
    if queue.uncertain_publication_ids:
        # T3 の不確定状態は絶対。照合が済むまで、次の候補は 1 件も進めない。
        blockers.append(BLOCKER_UNCERTAIN_PUBLICATION)
    blockers.extend(r for r in timing.reasons)  # gap_not_elapsed / outside_publication_window
    if not eligible:
        blockers.append(BLOCKER_NO_ELIGIBLE_CANDIDATE)
    if not AUTOMATIC_PUBLICATION_ENABLED:
        blockers.append(BLOCKER_AUTOMATIC_PUBLICATION_DISABLED)

    evidence = (
        EVIDENCE_USABLE
        if queue.mature_post_count >= queue.minimum_mature_posts
        else EVIDENCE_INSUFFICIENT
    )
    notes: list[str] = []
    if evidence == EVIDENCE_INSUFFICIENT:
        notes.append(
            f"{queue.mature_post_count} mature post(s); {queue.minimum_mature_posts} needed "
            "before performance can inform ordering"
        )
    notes.append("ordering never uses insight metrics; it uses explicit editorial facts")
    for verdict in others:
        if verdict.preferred:
            # 優先した提案が止まっているなら、なぜかを説明し、他の候補で評価を続ける。
            notes.append(
                f"preferred proposal {verdict.proposal_id} is blocked ({verdict.reason}); "
                "the normal order is used for the remaining candidates"
            )

    return QueueEvaluation(
        now=now,
        blockers=tuple(blockers),
        candidates=tuple(eligible + others),
        next_candidate=eligible[0] if eligible else None,
        timing=timing,
        next_evaluation_at=_next_evaluation_at(
            now, blockers, timing, policy, _earliest_not_before(others, now)
        ),
        evidence_state=evidence,
        ordering_basis="diversity_then_approval_order",
        notes=tuple(notes),
    )


def _earliest_not_before(verdicts, now: datetime) -> datetime | None:
    """not_before だけで止まっている候補のうち、最も早く資格が生まれる時刻。"""

    moments = [
        v.not_before
        for v in verdicts
        if v.reason == REASON_NOT_BEFORE and v.not_before is not None and v.not_before > now
    ]
    return min(moments) if moments else None


def _next_evaluation_at(
    now: datetime,
    blockers: list[str],
    timing: PublicationTiming,
    policy: ThreadsOperationsPolicy,
    earliest_not_before: datetime | None = None,
) -> datetime:
    """次に公開を評価し直す時刻。**固定の枠 (5 分刻み等) には合わせない。**

    - 待つ理由が時刻だけ (間隔・公開窓) なら、その資格時刻ちょうど。
    - 候補が無い・設定待ち・照合待ちなら、控えめな間隔で見直す
      (承認が増えれば queue 観測が評価を前倒しする)。
    """

    idle = now + timedelta(minutes=policy.idle_publication_reevaluation_minutes)
    if earliest_not_before is not None and BLOCKER_NO_ELIGIBLE_CANDIDATE in blockers:
        # 候補が not_before を待っているだけなら、その時刻ちょうどに見直す。
        idle = min(idle, earliest_not_before)
    waiting_on_humans = {
        BLOCKER_THREADS_DISABLED,
        BLOCKER_THREADS_MISCONFIGURED,
        BLOCKER_UNCERTAIN_PUBLICATION,
        BLOCKER_NO_ELIGIBLE_CANDIDATE,
    }
    if waiting_on_humans.intersection(blockers):
        return idle
    if not timing.eligible_now:
        return timing.earliest_at
    return idle


__all__ = [
    "AUTOMATIC_PUBLICATION_ENABLED",
    "CANDIDATE_REASONS",
    "EVIDENCE_INSUFFICIENT",
    "EVIDENCE_USABLE",
    "GLOBAL_BLOCKERS",
    "MAX_PUBLICATIONS_PER_CYCLE",
    "PROBLEM_BLOCKERS",
    "CandidateFacts",
    "CandidateVerdict",
    "QueueEvaluation",
    "QueueFacts",
    "evaluate_queue",
]
