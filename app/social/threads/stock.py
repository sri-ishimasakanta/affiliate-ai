"""Threads の投稿案の在庫を保守する計画 (T6、pure)。

問いは「使える候補を **もう少し** 作ったほうが役に立つか」であって、「常に N 本
なければならない」ではない。数えるのも決めるのも、明示した規則だけで行う
(スコアは作らない)。

**在庫の区分** (1 件は必ずどれか 1 つ。上から順に判定する):

======================  ====================================================
``published``           公開済み (同じ提案から公開がある)
``rejected``            人が却下した
``superseded``          作り直しで置き換わった
``stale``               陳腐化した (記事の変更・状態 stale・整合性の問題)
``expired``             ``expires_at`` を過ぎた
``held``                人が保留にした
``scheduled``           承認済み・未公開で、``not_before`` がまだ先
``approved_unpublished``  承認済み・未公開 (すぐ公開の候補になりうる)
``requested``           承認待ちで、携帯の承認依頼が有効
``prepared``            承認待ちで、まだ依頼していない
======================  ====================================================

**使える在庫** = ``prepared + requested + approved_unpublished + scheduled``。
``held`` / ``expired`` / ``stale`` は健全な在庫として数えない (人の再確認の対象)。

目安の下限・上限は T4.2 の承認在庫の目安と同じ計算 (日次の目安 × 在庫日数。
いまは 3 と 15)。**ノルマではない。**

**作るかどうか**:

- 使える在庫が下限より少ない → 足りない分だけ (ただし 1 回の上限まで)。
- 下限以上・上限未満で、使える在庫が 1 つの記事/トピックに偏っている → 1 本だけ。
- それ以外 → 作らない。
- 生成の依頼がまだ答えを待っているなら、新しい依頼は出さない (積み上げない)。

**1 回の上限** は ``max_new_proposals_per_cycle`` (3 以下にコードで固定)。1 記事につき 1 本。

**記事の選び方** (決定的):

1. 使える在庫を既に持つ記事は選ばない (同じ記事を掘り続けない)。
2. 最近使っていない記事から: (クールダウン中でない, 最後に使った時刻が古い,
   T5.5 のトピックの参考, 記事 ID) の順。一度も使っていない記事が先頭に来るので、
   どの記事も永久には後回しにならない。
3. この回の中では、まず違うトピックから選ぶ。

**切り口の選び方**: 使える在庫と最近の公開で **少ない** 切り口から。この回の中で
同じ切り口を 2 回選ばない。同じ記事で最近使った切り口は避ける。T5.5 の参考は
同数のときの並べ替えにだけ使う (多様性が学習より強い)。

**リンク**: 単体で価値のある投稿を基本にする。使える在庫と最近の公開でリンク付きの
割合が上限 (既定 1/3) 未満のときだけ、この回の 1 本をリンク付きにしてよい。
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.social.threads.style import POST_ANGLES

STATE_PUBLISHED = "published"
STATE_REJECTED = "rejected"
STATE_SUPERSEDED = "superseded"
STATE_STALE = "stale"
STATE_EXPIRED = "expired"
STATE_HELD = "held"
STATE_SCHEDULED = "scheduled"
STATE_APPROVED_UNPUBLISHED = "approved_unpublished"
STATE_REQUESTED = "requested"
STATE_PREPARED = "prepared"
STOCK_STATES = (
    STATE_PREPARED,
    STATE_REQUESTED,
    STATE_APPROVED_UNPUBLISHED,
    STATE_SCHEDULED,
    STATE_HELD,
    STATE_EXPIRED,
    STATE_STALE,
    STATE_REJECTED,
    STATE_SUPERSEDED,
    STATE_PUBLISHED,
)
USABLE_STATES = frozenset(
    {STATE_PREPARED, STATE_REQUESTED, STATE_APPROVED_UNPUBLISHED, STATE_SCHEDULED}
)
#: 人の再確認を勧める区分 (自動では何もしない)。
REVIEW_STATES = frozenset({STATE_HELD, STATE_EXPIRED, STATE_STALE})

LINK_NONE = "none"
LINK_ARTICLE = "article"


@dataclass(frozen=True)
class ProposalFact:
    proposal_id: int
    article_id: int
    angle: str
    link_mode: str
    state: str
    created_at: datetime
    topic: str | None = None


@dataclass(frozen=True)
class ArticleFact:
    article_id: int
    title: str
    topic: str | None
    has_url: bool
    #: この記事から最後に提案を作った / 公開した時刻 (無ければ None)。
    last_used_at: datetime | None = None
    #: 同じ記事で最近 (抑制の期間内に) 作った提案の切り口。
    recent_angles: frozenset[str] = frozenset()

    @property
    def topic_key(self) -> str:
        return self.topic or f"article:{self.article_id}"


@dataclass(frozen=True)
class StockFacts:
    now: datetime
    proposals: tuple[ProposalFact, ...]
    articles: tuple[ArticleFact, ...]
    #: 最近の公開 (新しい順)。切り口とリンクの偏りを見る。
    recent_publications: tuple[tuple[str, str], ...] = ()
    #: 答えを待っている生成の依頼の数。
    pending_generation_requests: int = 0


@dataclass(frozen=True)
class PlannedRequest:
    article_id: int
    article_title: str
    topic: str | None
    angle: str
    link_mode: str
    reasons: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "article_id": self.article_id,
            "article_title": self.article_title,
            "topic": self.topic,
            "angle": self.angle,
            "link_mode": self.link_mode,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class StockPlan:
    now: datetime
    counts: Mapping[str, int]
    usable: int
    target_low: int
    target_high: int
    needs_generation: bool
    reasons: tuple[str, ...]
    requested_count: int
    requests: tuple[PlannedRequest, ...]
    blocked_by: tuple[str, ...] = ()
    topics: Mapping[str, int] = field(default_factory=dict)
    angles: Mapping[str, int] = field(default_factory=dict)
    link_modes: Mapping[str, int] = field(default_factory=dict)
    re_review: tuple[dict, ...] = ()
    skipped_articles: Mapping[str, int] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "now": self.now.isoformat(),
            "counts": dict(self.counts),
            "usable": self.usable,
            "target_low": self.target_low,
            "target_high": self.target_high,
            "advisory": True,
            "needs_generation": self.needs_generation,
            "reasons": list(self.reasons),
            "requested_count": self.requested_count,
            "requests": [r.as_dict() for r in self.requests],
            "blocked_by": list(self.blocked_by),
            "usable_topics": dict(self.topics),
            "usable_angles": dict(self.angles),
            "usable_link_modes": dict(self.link_modes),
            "re_review": list(self.re_review),
            "skipped_articles": dict(self.skipped_articles),
            "notes": list(self.notes),
        }


def plan_stock(facts: StockFacts, policy, *, guidance=None) -> StockPlan:
    """在庫の保守の計画 (決定的。DB も外部も見ない)。"""

    now = _aware(facts.now)
    counts = Counter(p.state for p in facts.proposals)
    counts = {state: counts.get(state, 0) for state in STOCK_STATES}
    usable_rows = [p for p in facts.proposals if p.state in USABLE_STATES]
    usable = len(usable_rows)
    low, high = policy.stock_target_low, policy.stock_target_high
    per_cycle = policy.max_new_proposals_per_cycle

    topics = Counter(p.topic or f"article:{p.article_id}" for p in usable_rows)
    angles = Counter(p.angle for p in usable_rows)
    links = Counter(p.link_mode for p in usable_rows)

    reasons: list[str] = []
    need = 0
    if usable < low:
        need = low - usable
        reasons.append(f"usable stock {usable} is below the advisory floor {low}")
    elif (
        usable < high
        and bool(policy.proposal_stock("diversity_top_up", True))
        and usable >= 2
        and len(topics) == 1
    ):
        need = 1
        reasons.append(
            f"usable stock {usable} is within the advisory range, but only one "
            f"article/topic is represented ({next(iter(topics))})"
        )
    elif usable >= high:
        reasons.append(f"usable stock {usable} is at or above the advisory ceiling {high}")
    else:
        reasons.append(
            f"usable stock {usable} is within the advisory range {low}-{high} and "
            f"{len(topics)} topics are represented"
        )
    missing_angles = [a for a in POST_ANGLES if angles.get(a, 0) == 0]
    if missing_angles and usable:
        reasons.append(f"no usable candidate for angle(s): {', '.join(missing_angles)}")
    if counts[STATE_REQUESTED]:
        reasons.append(f"{counts[STATE_REQUESTED]} approval request(s) are outstanding")

    blocked: list[str] = []
    if need and facts.pending_generation_requests:
        blocked.append(
            f"{facts.pending_generation_requests} generation request(s) are still waiting "
            "for output; no new request until they are answered or go stale"
        )

    wanted = min(need, per_cycle) if not blocked else 0
    requests, skipped = _select(facts, policy, guidance, wanted, usable_rows)
    if wanted and len(requests) < wanted:
        reasons.append(
            f"only {len(requests)} source article(s) are available for {wanted} request(s)"
        )

    return StockPlan(
        now=now,
        counts=counts,
        usable=usable,
        target_low=low,
        target_high=high,
        needs_generation=need > 0,
        reasons=tuple(reasons),
        requested_count=len(requests),
        requests=tuple(requests),
        blocked_by=tuple(blocked),
        topics=dict(sorted(topics.items())),
        angles={a: angles.get(a, 0) for a in POST_ANGLES},
        link_modes=dict(sorted(links.items())),
        re_review=tuple(_re_review(facts, policy, now)),
        skipped_articles=skipped,
        notes=(
            "stock targets are advisory (T4.2), not quotas",
            f"at most {per_cycle} new proposal(s) per cycle, one per article",
            "new proposals are stored as awaiting_approval; a human approves each one",
        ),
    )


# == selection ==================================================================
def _select(facts, policy, guidance, wanted, usable_rows):
    now = _aware(facts.now)
    cooldown = timedelta(days=float(policy.proposal_stock("article_cooldown_days", 7)))
    stocked = {p.article_id for p in usable_rows}
    skipped = Counter()
    candidates = []
    for article in facts.articles:
        if article.article_id in stocked:
            skipped["already has usable stock"] += 1
            continue
        candidates.append(article)
    topic_rank = _topic_ranks(guidance)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)

    def key(a: ArticleFact):
        last = _aware(a.last_used_at) if a.last_used_at else None
        in_cooldown = last is not None and now - last < cooldown
        return (in_cooldown, last or epoch, topic_rank.get(a.topic_key, 1), a.article_id)

    ordered = sorted(candidates, key=key)
    chosen: list[ArticleFact] = []
    seen_topics: set[str] = set()
    for article in ordered:  # まず違うトピックから
        if len(chosen) >= wanted:
            break
        if article.topic_key in seen_topics:
            continue
        chosen.append(article)
        seen_topics.add(article.topic_key)
    for article in ordered:  # 足りなければ同じトピックからも
        if len(chosen) >= wanted:
            break
        if article not in chosen:
            chosen.append(article)
    if wanted:
        skipped["waiting for a later cycle (fairness order)"] = len(ordered) - len(chosen)

    requests = _assign(chosen, facts, policy, guidance, usable_rows)
    return requests, dict(sorted(skipped.items()))


def _assign(chosen, facts, policy, guidance, usable_rows) -> list[PlannedRequest]:
    window = int(policy.proposal_stock("recent_publication_window", 10))
    recent = facts.recent_publications[:window]
    angle_counts = Counter(p.angle for p in usable_rows) + Counter(a for a, _l in recent)
    angle_rank = _angle_ranks(guidance)

    link_total = len(usable_rows) + len(recent)
    link_count = sum(1 for p in usable_rows if p.link_mode == LINK_ARTICLE) + sum(
        1 for _a, link in recent if link == LINK_ARTICLE
    )
    share = link_count / link_total if link_total else 0.0
    allow_link = share < float(policy.proposal_stock("article_link_share_max", 0.34))
    link_article = next((a for a in reversed(chosen) if a.has_url), None) if allow_link else None

    used: set[str] = set()
    requests = []
    for article in chosen:
        reasons = []
        options = [a for a in POST_ANGLES if a not in used and a not in article.recent_angles]
        if not options:
            options = [a for a in POST_ANGLES if a not in used] or list(POST_ANGLES)
            reasons.append("every angle was used recently for this article")
        angle = min(
            options,
            key=lambda a: (angle_counts.get(a, 0), angle_rank.get(a, 1), POST_ANGLES.index(a)),
        )
        used.add(angle)
        angle_counts[angle] += 1
        reasons.append(
            f"angle {angle}: least represented in usable stock and recent posts"
            + (
                " (T5.5 weak preference used as a tie-break)"
                if angle_rank.get(angle, 1) == 0
                else ""
            )
        )
        link_mode = LINK_ARTICLE if article is link_article else LINK_NONE
        reasons.append(
            "link article: link share is below the limit"
            if link_mode == LINK_ARTICLE
            else "link none: standalone-value post"
        )
        last = article.last_used_at
        reasons.insert(
            0,
            "article never used for Threads"
            if last is None
            else f"article last used {(_aware(facts.now) - _aware(last)).days} day(s) ago",
        )
        requests.append(
            PlannedRequest(
                article_id=article.article_id,
                article_title=article.title,
                topic=article.topic,
                angle=angle,
                link_mode=link_mode,
                reasons=tuple(reasons),
            )
        )
    return requests


def _topic_ranks(guidance) -> dict[str, int]:
    return _ranks(guidance, "topic")


def _angle_ranks(guidance) -> dict[str, int]:
    return _ranks(guidance, "angle")


def _ranks(guidance, dimension) -> dict[str, int]:
    """T5.5 の参考を **同数のときの並べ替え** にだけ使う (0 = prefer, 2 = de-emphasize)。"""

    ranks: dict[str, int] = {}
    if guidance is None:
        return ranks
    for preference in guidance.preferences:
        if preference.dimension != dimension or not preference.actionable:
            continue
        ranks[preference.value] = 0 if preference.direction == "prefer" else 2
    return ranks


def _re_review(facts, policy, now) -> list[dict]:
    """人に見直しを勧める提案 (**自動では消さない・却下しない**)。"""

    days = float(policy.proposal_stock("re_review_after_days", 14))
    out = []
    for p in facts.proposals:
        if p.state in REVIEW_STATES:
            out.append({"proposal_id": p.proposal_id, "reason": p.state})
        elif p.state == STATE_PREPARED and now - _aware(p.created_at) > timedelta(days=days):
            out.append(
                {
                    "proposal_id": p.proposal_id,
                    "reason": f"awaiting approval for more than {days:g} days (not expired)",
                }
            )
    return out


def request_id(*, article_id: int, angle: str, link_mode: str, fingerprint: str, as_of) -> str:
    """生成の依頼の決定的な ID (同じ計画を 2 回出しても同じ ID になる)。"""

    payload = chr(31).join(
        [str(article_id), angle, link_mode, fingerprint, _aware(as_of).isoformat()]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


__all__ = [
    "STOCK_STATES",
    "USABLE_STATES",
    "ArticleFact",
    "PlannedRequest",
    "ProposalFact",
    "StockFacts",
    "StockPlan",
    "plan_stock",
    "request_id",
]
