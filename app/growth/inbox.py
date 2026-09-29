"""Growth Action の受け箱 (Inbox) の決まり: いま動ける候補か・既存の仕事が担っているか・まとめ方。

pure。DB は読まない (必要な事実は ``CoverageContext`` で渡す)。

- ``informational``: 待つ・データの質の確認。承認を求める対象ではない。
- ``covered_by_existing_work``: 既存の仕事 (開いている Threads の提案・在庫の保守の計画・
  Growth の枠の自動の生成・開いている変更の依頼) がすでに担っている。人に新しく知らせない。
- ``blocked``: 止める理由がある (古いデータ・人の設定の作業など)。表示はするが、承認には出さない。
- ``actionable_now``: 上のどれでもない。人のレビューに出せる。

Growth の枠と通常の投稿を混ぜない: 通常の提案は Growth の候補を担わず、Growth の提案は
記事の候補を担わない。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from app.growth import analysis as ga

ACTIONABLE_NOW = "actionable_now"
COVERED = "covered_by_existing_work"
BLOCKED = "blocked"
INFORMATIONAL = "informational"
AVAILABILITIES = (ACTIONABLE_NOW, COVERED, BLOCKED, INFORMATIONAL)

INFORMATIONAL_ACTIONS = frozenset({ga.WAIT_FOR_MORE_DATA, ga.INVESTIGATE_DATA_QUALITY})
THREADS_ACTIONS = frozenset({ga.CREATE_REGULAR_THREADS_POST, ga.CREATE_THREADS_ALTERNATIVE_ANGLE})
WORDPRESS_ACTIONS = frozenset({ga.CREATE_NEW_ARTICLE, ga.UPDATE_EXISTING_ARTICLE,
                               ga.IMPROVE_SEARCH_SNIPPET, ga.REVIEW_INTERNAL_LINKS,
                               ga.REVIEW_AFFILIATE_PLACEMENT})  # fmt: skip
#: この時間より新しい通常の投稿がある記事には、別の切り口をすぐには勧めない (先の投稿を見届ける)。
RECENT_REGULAR_POST_HOURS = 72
#: まとめ (digest) の既定: 全体の件数と、1 つの行動の種類あたりの上限。
DIGEST_LIMIT = 8
DIGEST_PER_ACTION = 2


@dataclass(frozen=True)
class CoverageContext:
    """既存の仕事の事実 (読むだけで集めたもの)。"""

    #: 記事 → 開いている通常の提案 (``proposed`` / ``awaiting_approval`` / 公開前の ``approved``)。
    open_regular_proposals: Mapping[int, Sequence[Mapping]] = field(default_factory=dict)
    #: 記事 → 最後の通常の投稿からの時間。
    latest_regular_post_hours: Mapping[int, float] = field(default_factory=dict)
    #: 記事 → 在庫の保守が次に依頼する予定の切り口 (``ThreadsProposalStockService.plan``)。
    stock_planned: Mapping[int, Sequence[str]] = field(default_factory=dict)
    #: その日の Growth Post を worker が自動で作るか (Growth の枠は自分の経路で担う)。
    growth_lane_automatic: bool = True
    #: その日の Growth の提案 (あれば ID)。
    growth_proposal_today: int | None = None
    #: 記事 → 開いている変更の依頼 (ChangeRequest) の ID。
    open_change_requests: Mapping[int, Sequence[int]] = field(default_factory=dict)
    #: 機会 → 開いているレビュー (C9 の自分の履歴)。
    open_reviews: Mapping[str, int] = field(default_factory=dict)


def assess(candidate: Mapping, context: CoverageContext) -> tuple[str, list[str]]:
    """候補がいま動けるか。返り値: (状態, 理由)。"""

    action = candidate["action_type"]
    aid = candidate.get("article_id")
    reasons: list[str] = []
    if action in INFORMATIONAL_ACTIONS:
        return INFORMATIONAL, ["informational: not a review request"]
    if candidate.get("opportunity_key") in context.open_reviews:
        reasons.append(f"a C9 review is already open "
                       f"(#{context.open_reviews[candidate['opportunity_key']]})")
    if action in THREADS_ACTIONS and aid is not None:
        for p in context.open_regular_proposals.get(aid, ()):
            reasons.append(f"regular Threads proposal #{p.get('id')} ({p.get('status')}"
                           f"{', angle ' + str(p['angle']) if p.get('angle') else ''}) "
                           "already covers this article")
        hours = context.latest_regular_post_hours.get(aid)
        if (action == ga.CREATE_THREADS_ALTERNATIVE_ANGLE and hours is not None
                and hours < RECENT_REGULAR_POST_HOURS):
            reasons.append(f"a regular post was published {round(hours, 1)}h ago; let it "
                           "mature before another angle")
        planned = context.stock_planned.get(aid)
        if planned:
            reasons.append("the proposal stock maintenance plans this article next "
                           f"(angle {', '.join(planned)})")
    if action == ga.CREATE_GROWTH_POST:
        if context.growth_proposal_today is not None:
            reasons.append(f"today's Growth proposal #{context.growth_proposal_today} exists")
        elif context.growth_lane_automatic:
            reasons.append("the Growth lane (account_growth_maintenance) generates it")
    if action in WORDPRESS_ACTIONS and aid is not None and context.open_change_requests.get(aid):
        ids = ", ".join(f"#{i}" for i in context.open_change_requests[aid])
        reasons.append(f"an open change request already exists for this article ({ids})")
    if reasons:
        return COVERED, reasons
    if candidate.get("blockers"):
        return BLOCKED, list(candidate["blockers"])
    return ACTIONABLE_NOW, []


def entry_sort_key(entry: Mapping) -> tuple:
    """候補と同じ並べ方 (成分の順。1 つの点数にしない)。"""

    p = entry["priority"]
    return (-p["evidence_strength"]["rank"], -p["potential_opportunity"]["rank"],
            -p["monetization_relevance"]["rank"], p["effort"]["rank"],
            -p["recency_urgency"]["rank"], entry["action_type"], entry["subject_id"],
            entry.get("variant") or "")  # fmt: skip


def group_by_action(entries: Iterable[Mapping]) -> dict[str, list[Mapping]]:
    out: dict[str, list[Mapping]] = {}
    for entry in sorted(entries, key=entry_sort_key):
        out.setdefault(entry["action_type"], []).append(entry)
    return dict(sorted(out.items()))


def select_digest(entries: Iterable[Mapping], *, limit: int = DIGEST_LIMIT,
                  per_action: int = DIGEST_PER_ACTION
                  ) -> tuple[list[dict], list[dict]]:  # fmt: skip
    """人が見る少数の候補を選ぶ (決定的)。

    いま動ける候補だけ。行動の種類ごとに上限を置き、1 つの種類で埋め尽くさない。種類を回して
    (round robin) 選ぶので、上位の種類から順に 1 件ずつ入る。
    返り値: (選んだもの, 選ばなかったもの)。
    """

    groups = group_by_action(e for e in entries if e.get("availability") == ACTIONABLE_NOW)
    order = sorted(groups, key=lambda a: entry_sort_key(groups[a][0]))
    selected: list[dict] = []
    taken = {a: 0 for a in order}
    for round_ in range(per_action):
        for action in order:
            if len(selected) >= limit:
                break
            items = groups[action]
            if round_ < len(items) and taken[action] < per_action:
                taken[action] += 1
                selected.append({**items[round_], "selection_reason": (
                    f"top {round_ + 1} of {len(items)} actionable {action} "
                    f"(per-type cap {per_action})")})
    chosen = {id(e) for e in selected}
    skipped = []
    for action in order:
        for i, item in enumerate(groups[action]):
            if not any(s["candidate_fingerprint"] == item["candidate_fingerprint"]
                       for s in selected) and id(item) not in chosen:  # fmt: skip
                skipped.append({**item, "not_selected_reason": (
                    f"per-type cap {per_action}" if i >= per_action else f"digest limit {limit}")})
    return selected, skipped


__all__ = ["ACTIONABLE_NOW", "AVAILABILITIES", "BLOCKED", "COVERED", "CoverageContext",
           "DIGEST_LIMIT", "DIGEST_PER_ACTION", "INFORMATIONAL", "INFORMATIONAL_ACTIONS",
           "RECENT_REGULAR_POST_HOURS", "THREADS_ACTIONS", "WORDPRESS_ACTIONS", "assess",
           "entry_sort_key", "group_by_action", "select_digest"]
