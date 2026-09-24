"""承認依頼のまとめ送り (digest) の計画 (T4.2、pure)。

**提案づくりとメール送信は別物。** ここが扱うのは、既に DB にある提案のうち、
「いま人に承認を求める価値があるもの」をどれにし、いつ 1 通にまとめて送るか
だけである。提案の文面は読むだけで、書き換えない。似た提案を黙って直すことも
しない (見送るだけで、在庫には残す)。

時刻の決まり方 (固定の時刻表は持たない。乱数も使わない):

- 承認通知窓 ``[08:00, 21:00)`` JST の外では **送らない** (依頼も催促も)。
  夜に作られた提案は、窓が開いた後に改めて評価し直してから送る。
- ``cooldown``: 前の digest から最低これだけ空ける (1 時間に何通も送らない)。
- ``gather``: 最初に依頼の価値が生まれた提案は最大これだけ待つ。その間に増えた
  提案は同じ 1 通に入る (13:05 / 13:22 / 13:41 → 14:05 に 1 通)。
- 1 通に入る数が上限に達していれば gather を待たない (cooldown だけ守る)。

1 通の上限は通常 5 件。それを超える分は **次の機会に回す** (消さない)。

選び方 (不透明な順位付けはしない。名前の付いた規則だけ):

1. 期限 (expires_at) が近いものから
2. 同じ 1 通の中で、記事と切り口がなるべく重ならないように
3. 作られた順 (古いものから)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.operations.local_time import to_local
from app.social.threads.policy import ThreadsOperationsPolicy
from app.social.threads.schedule import next_window_open_at, window_is_open

# -- 見送り (この digest には入れないが、消さない) ---------------------------------
SUPPRESS_HELD = "held"
SUPPRESS_EXPIRED = "expired"
SUPPRESS_STALE = "stale"
SUPPRESS_CONTENT_INTEGRITY = "content_integrity"
SUPPRESS_ALREADY_REQUESTED = "already_requested"
SUPPRESS_DUPLICATE = "duplicate"
SUPPRESSION_REASONS = (
    SUPPRESS_HELD,
    SUPPRESS_EXPIRED,
    SUPPRESS_STALE,
    SUPPRESS_CONTENT_INTEGRITY,
    SUPPRESS_ALREADY_REQUESTED,
    SUPPRESS_DUPLICATE,
)

# -- 先送り (価値はあるが、今回の 1 通には入れない) ---------------------------------
DEFER_OVER_DIGEST_LIMIT = "over_digest_limit"
DEFER_APPROVED_STOCK_SUFFICIENT = "approved_stock_sufficient"
DEFERRAL_REASONS = (DEFER_OVER_DIGEST_LIMIT, DEFER_APPROVED_STOCK_SUFFICIENT)

# -- 送らない理由 (1 通そのもの) ----------------------------------------------------
WAIT_NOTHING_TO_REQUEST = "nothing_to_request"
WAIT_OUTSIDE_NOTIFICATION_WINDOW = "outside_notification_window"
WAIT_COOLDOWN = "cooldown"
WAIT_GATHERING = "gathering"
WAIT_REASONS = (
    WAIT_NOTHING_TO_REQUEST,
    WAIT_OUTSIDE_NOTIFICATION_WINDOW,
    WAIT_COOLDOWN,
    WAIT_GATHERING,
)


#: 手動の上書きを行える唯一の主体。常駐 worker や自動の送信には使わせない。
OVERRIDE_SOURCE_HUMAN_CLI = "human-cli"


class WindowOverrideError(ValueError):
    pass


@dataclass(frozen=True)
class ManualWindowOverride:
    """人が CLI で明示した **通知窓だけ** の上書き (本番パイロット・試験用)。

    飛ばすのは 08:00-21:00 の通知窓だけ。stale / 期限切れ / 保留 / 重複 /
    cooldown / gather / capability / セッションの期限 / CSRF / 1 件ずつの決定 —
    それ以外の規則はすべてそのまま効く。

    常駐 worker と自動の送信はこれを作らない (作る経路が無い)。理由は必須。
    """

    reason: str
    source: str = OVERRIDE_SOURCE_HUMAN_CLI

    def __post_init__(self) -> None:
        if self.source != OVERRIDE_SOURCE_HUMAN_CLI:
            raise WindowOverrideError("only an explicit human CLI run may override the window")
        if not (self.reason or "").strip():
            raise WindowOverrideError("a notification-window override needs a non-empty reason")
        object.__setattr__(self, "reason", self.reason.strip()[:500])


@dataclass(frozen=True)
class DigestCandidate:
    """在庫にある 1 件の提案について、DB から集めた事実だけ。"""

    proposal_id: int
    source_article_id: int
    article_title: str
    angle: str
    #: 依頼の価値が生まれた時刻 = 提案が在庫に入った時刻。
    ready_at: datetime
    #: 同一性の判定用 (NFKC まで畳んだ本文)。本文そのものではない。
    identity: str
    preview: str
    not_before: datetime | None = None
    expires_at: datetime | None = None
    held: bool = False
    stale_reasons: tuple[str, ...] = ()
    integrity_reasons: tuple[str, ...] = ()
    #: いま有効な承認依頼 (pending のセッション) があるか。
    has_active_request: bool = False


@dataclass(frozen=True)
class DigestItem:
    candidate: DigestCandidate
    reason: str
    details: tuple[str, ...] = ()

    def as_dict(self, tz: ZoneInfo) -> dict:
        c = self.candidate
        return {
            "proposal_id": c.proposal_id,
            "source_article_id": c.source_article_id,
            "article_title": c.article_title,
            "angle": c.angle,
            "ready_local": to_local(c.ready_at, tz).isoformat(),
            "not_before_local": to_local(c.not_before, tz).isoformat() if c.not_before else None,
            "expires_local": to_local(c.expires_at, tz).isoformat() if c.expires_at else None,
            "preview": c.preview,
            "reason": self.reason,
            "details": list(self.details),
        }


@dataclass(frozen=True)
class StockSummary:
    """在庫の目安 (助言のみ)。ノルマでも上限でもない。"""

    approved_unpublished: int
    pending_requests: int
    prepared: int
    target_low: int
    target_high: int

    @property
    def room(self) -> int:
        return max(0, self.target_high - self.approved_unpublished - self.pending_requests)

    @property
    def below_low(self) -> bool:
        return self.approved_unpublished + self.pending_requests + self.prepared < self.target_low

    def as_dict(self) -> dict:
        return {
            "approved_unpublished": self.approved_unpublished,
            "pending_requests": self.pending_requests,
            "prepared": self.prepared,
            "target_low": self.target_low,
            "target_high": self.target_high,
            "room_for_new_requests": self.room,
            "suggest_generating_more": self.below_low,
            "advisory": True,
        }


@dataclass(frozen=True)
class DigestPlan:
    now: datetime
    window_open: bool
    last_sent_at: datetime | None
    cooldown_until: datetime | None
    gather_until: datetime | None
    due_at: datetime | None
    would_send: bool
    waiting_for: tuple[str, ...]
    selected: tuple[DigestItem, ...]
    suppressed: tuple[DigestItem, ...]
    deferred: tuple[DigestItem, ...]
    stock: StockSummary
    ttl_hours: int
    next_run_at: datetime
    policy_version: str
    notes: tuple[str, ...] = field(default_factory=tuple)
    #: 人が明示した通知窓の上書き (あれば理由)。窓以外の規則には影響しない。
    override_reason: str | None = None

    @property
    def manual_override_requested(self) -> bool:
        return self.override_reason is not None

    @property
    def window_overridden(self) -> bool:
        """窓の外なのに、上書きによって送れる状態か。"""

        return self.manual_override_requested and not self.window_open and self.would_send

    @property
    def planned_ttl_start(self) -> datetime | None:
        """送るならこの時刻から期限を数える。**提案を作った時刻ではない。**"""

        if not self.selected:
            return None
        return self.now if self.would_send else self.due_at

    @property
    def planned_expires_at(self) -> datetime | None:
        start = self.planned_ttl_start
        return start + timedelta(hours=self.ttl_hours) if start else None

    def as_dict(self, tz: ZoneInfo) -> dict:
        def local(moment):
            return to_local(moment, tz).isoformat() if moment else None

        return {
            "now_local": local(self.now),
            "policy_version": self.policy_version,
            "notification_window_open": self.window_open,
            "manual_override_requested": self.manual_override_requested,
            "override_reason": self.override_reason,
            "window_overridden": self.window_overridden,
            "last_sent_local": local(self.last_sent_at),
            "cooldown_until_local": local(self.cooldown_until),
            "gather_until_local": local(self.gather_until),
            "due_local": local(self.due_at),
            "would_send": self.would_send,
            "waiting_for": list(self.waiting_for),
            "selected": [i.as_dict(tz) for i in self.selected],
            "suppressed": [i.as_dict(tz) for i in self.suppressed],
            "deferred": [i.as_dict(tz) for i in self.deferred],
            "stock": self.stock.as_dict(),
            "ttl_hours": self.ttl_hours,
            "planned_ttl_start_local": local(self.planned_ttl_start),
            "planned_expires_local": local(self.planned_expires_at),
            "next_run_local": local(self.next_run_at),
            "emails_if_sent": 1 if self.selected else 0,
            "notes": list(self.notes),
        }


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(UTC)


def _suppression(c: DigestCandidate, now: datetime, seen: set[str]) -> DigestItem | None:
    """この 1 通に入れない理由。強いものから 1 つだけ。"""

    expires_at = _aware(c.expires_at)
    if c.has_active_request:
        return DigestItem(c, SUPPRESS_ALREADY_REQUESTED)
    if expires_at is not None and expires_at <= now:
        return DigestItem(c, SUPPRESS_EXPIRED, (f"expired at {expires_at.isoformat()}",))
    if c.stale_reasons:
        return DigestItem(c, SUPPRESS_STALE, c.stale_reasons)
    if c.integrity_reasons:
        return DigestItem(c, SUPPRESS_CONTENT_INTEGRITY, c.integrity_reasons)
    if c.held:
        return DigestItem(c, SUPPRESS_HELD)
    if c.identity in seen:
        return DigestItem(c, SUPPRESS_DUPLICATE, ("same text as another queued proposal",))
    return None


def _select(ready: list[DigestCandidate], limit: int) -> tuple[list, list]:
    """期限の近い順 → 1 通の中で記事と切り口を散らす → 古い順。"""

    far = datetime.max.replace(tzinfo=UTC)
    ordered = sorted(
        ready,
        key=lambda c: (_aware(c.expires_at) or far, _aware(c.ready_at), c.proposal_id),
    )
    chosen: list[DigestCandidate] = []
    articles: set[int] = set()
    angles: set[str] = set()
    # 1 巡目: 記事も切り口もまだ入っていないものだけ。
    for c in ordered:
        if len(chosen) >= limit:
            break
        if c.source_article_id not in articles and c.angle not in angles:
            chosen.append(c)
            articles.add(c.source_article_id)
            angles.add(c.angle)
    # 2 巡目: 残りの枠を、元の順序のまま埋める。
    for c in ordered:
        if len(chosen) >= limit:
            break
        if c not in chosen:
            chosen.append(c)
    rest = [c for c in ordered if c not in chosen]
    # 1 通の中は、期限と作成順の元の順序で並べ直す (読む順が安定するように)。
    chosen.sort(key=ordered.index)
    return chosen, rest


def plan_digest(
    candidates: list[DigestCandidate],
    *,
    now: datetime,
    last_sent_at: datetime | None,
    queued_identities: set[str],
    approved_unpublished: int,
    policy: ThreadsOperationsPolicy,
    tz: ZoneInfo,
    window_override: ManualWindowOverride | None = None,
) -> DigestPlan:
    now = _aware(now)
    if window_override is not None and not isinstance(window_override, ManualWindowOverride):
        raise WindowOverrideError("the window override must be a ManualWindowOverride")
    last_sent_at = _aware(last_sent_at)
    window = policy.approval_notification_window
    window_open = window_is_open(window, now, tz)

    suppressed: list[DigestItem] = []
    ready: list[DigestCandidate] = []
    seen = set(queued_identities)
    for c in sorted(candidates, key=lambda x: (_aware(x.ready_at), x.proposal_id)):
        item = _suppression(c, now, seen)
        if item is not None:
            suppressed.append(item)
            continue
        ready.append(c)
        seen.add(c.identity)

    pending = sum(1 for c in candidates if c.has_active_request)
    stock = StockSummary(
        approved_unpublished=approved_unpublished,
        pending_requests=pending,
        prepared=len(ready),
        target_low=round(policy.daily_target_low * policy.stock_days_low),
        target_high=round(policy.daily_target_high * policy.stock_days_high),
    )

    deferred: list[DigestItem] = []
    limit = min(policy.digest_max_items, stock.room)
    if ready and limit <= 0:
        deferred = [DigestItem(c, DEFER_APPROVED_STOCK_SUFFICIENT) for c in ready]
        chosen: list[DigestCandidate] = []
    else:
        chosen, rest = _select(ready, limit)
        reason = (
            DEFER_OVER_DIGEST_LIMIT
            if limit == policy.digest_max_items
            else DEFER_APPROVED_STOCK_SUFFICIENT
        )
        deferred = [DigestItem(c, reason) for c in rest]
    selected = tuple(DigestItem(c, "selected") for c in chosen)

    # -- いつ送るか ---------------------------------------------------------------
    waiting: list[str] = []
    cooldown_until = (
        last_sent_at + timedelta(minutes=policy.digest_cooldown_minutes) if last_sent_at else None
    )
    gather_until = None
    due_at = None
    flush = policy.subsystem("approval_notification_flush")
    idle = now + timedelta(minutes=int(flush.get("idle_interval_minutes", 30)))
    if not selected:
        waiting.append(WAIT_NOTHING_TO_REQUEST)
        next_run_at = idle
    else:
        full = len(ready) >= policy.digest_max_items
        oldest = min(_aware(c.ready_at) for c in chosen)
        gather_until = None if full else oldest + timedelta(minutes=policy.digest_gather_minutes)
        earliest = max(m for m in (now, cooldown_until, gather_until) if m is not None)
        # 上書きが飛ばすのは通知窓だけ。cooldown と gather はそのまま効く。
        due_at = (
            earliest if window_override is not None else next_window_open_at(window, earliest, tz)
        )
        if cooldown_until is not None and cooldown_until > now:
            waiting.append(WAIT_COOLDOWN)
        if gather_until is not None and gather_until > now:
            waiting.append(WAIT_GATHERING)
        if not window_open and window_override is None:
            # 夜間 (21:00-08:00) は依頼も催促も送らない。窓が開いたら評価し直す。
            waiting.append(WAIT_OUTSIDE_NOTIFICATION_WINDOW)
        next_run_at = due_at if due_at > now else now

    window_ok = window_open or window_override is not None
    would_send = bool(selected) and window_ok and due_at is not None and due_at <= now

    notes: list[str] = []
    if stock.below_low:
        notes.append(
            f"approved + requested + prepared stock is below the advisory {stock.target_low}; "
            "consider generating more proposals"
        )
    if deferred:
        notes.append(f"{len(deferred)} proposal(s) stay in stock for a later digest")
    if window_override is not None and not window_open:
        notes.append(
            "the notification window is overridden by an explicit human CLI run "
            f"({window_override.reason}); every other rule still applies"
        )
    return DigestPlan(
        now=now,
        window_open=window_open,
        last_sent_at=last_sent_at,
        cooldown_until=cooldown_until,
        gather_until=gather_until,
        due_at=due_at,
        would_send=would_send,
        waiting_for=tuple(dict.fromkeys(waiting)),
        selected=selected,
        suppressed=tuple(suppressed),
        deferred=tuple(deferred),
        stock=stock,
        ttl_hours=policy.approval_ttl_hours,
        next_run_at=next_run_at,
        policy_version=policy.policy_version,
        notes=tuple(notes),
        override_reason=window_override.reason if window_override is not None else None,
    )


__all__ = [
    "DEFERRAL_REASONS",
    "DEFER_APPROVED_STOCK_SUFFICIENT",
    "DEFER_OVER_DIGEST_LIMIT",
    "SUPPRESSION_REASONS",
    "WAIT_COOLDOWN",
    "WAIT_GATHERING",
    "WAIT_NOTHING_TO_REQUEST",
    "WAIT_OUTSIDE_NOTIFICATION_WINDOW",
    "WAIT_REASONS",
    "OVERRIDE_SOURCE_HUMAN_CLI",
    "DigestCandidate",
    "ManualWindowOverride",
    "WindowOverrideError",
    "DigestItem",
    "DigestPlan",
    "StockSummary",
    "plan_digest",
]
