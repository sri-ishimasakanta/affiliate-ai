"""1 日の計画を順に実行する (T6.5B.2)。**この段階では本番で実行しない** (試験は偽のページ)。

- 計画の手順 (``planning.PlanStep``) ごとに ``collector.collect`` を 1 回 (読むだけ・上限つき)。
- 確かめていない画面の手順は実行しない (``surface_unverified``)。
- 受け入れた投稿の合計 (重複を含む) が段階の上限 (``stage_cap`` ≤ 100) を超えないように、
  各手順の予算を残りで切る → 重複を除いた投稿の数も上限を超えない。
- **足りない分を埋めない**: 手順が予算より少なく返しても、ほかの手順を増やさない・
  スクロールを増やさない。安全に読めた数がそのまま結果。
- ログインが要る → そこで全体を止める (何も保存させない)。画面の形の違い / 勘定の食い違い
  → その手順の投稿を捨て (collector の fail closed)、次の手順へ (手順ごとに別のページ)。
- 同じ投稿が複数の出どころで見つかったら、**重複を除いた数では 1 つ**。見つけた出どころ
  (provenance) は全部残す (観測の行は出どころごと、積むだけ)。
- 同じ投稿の再観測 (``repeat_snapshot``) は重複を除いた数に入れない (今は計画に出ない)。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.models.threads_observer import (
    RUN_ACCOUNTING_MISMATCH,
    RUN_DOM_UNRECOGNIZED,
    RUN_LOGIN_REQUIRED,
    RUN_PARTIAL,
    RUN_SUCCEEDED,
    SOURCE_FOR_YOU,
    SOURCE_KNOWN_ACCOUNT,
    SOURCE_SEARCH,
    SOURCE_TOPIC_FOR_YOU,
)
from app.social.threads.observer import selectors as sel
from app.social.threads.observer.collector import CollectionPlan, CollectionResult, collect
from app.social.threads.observer.driver import ReadOnlyPage
from app.social.threads.observer.planning import DailyPlan, PlanStep

REPEAT_SNAPSHOT = "repeat_snapshot"
STEP_SKIPPED_UNVERIFIED = "surface_unverified"
STEP_SKIPPED_CAP = "stage_cap_reached"
STEP_SKIPPED_AFTER_LOGIN = "stopped_login_required"


@dataclass
class StepOutcome:
    source_type: str
    query: str | None
    budget: int
    status: str
    accepted: int = 0
    new_unique: int = 0
    duplicates_of_earlier: int = 0
    shortfall: int = 0
    reason: str | None = None


@dataclass
class OrchestrationResult:
    plan: DailyPlan
    result: CollectionResult
    steps: list[StepOutcome] = field(default_factory=list)
    provenance: dict[str, list[dict]] = field(default_factory=dict)

    @property
    def unique_posts(self) -> int:
        return count_unique(self.result.posts)

    def summary(self) -> dict:
        unique = self.unique_posts
        return {
            "day": self.plan.day,
            "policy_version": self.plan.policy_version,
            "stage": self.plan.stage,
            "stage_cap": self.plan.stage_cap,
            "status": self.result.status,
            "reason": self.result.reason,
            "unique_posts": unique,
            "accepted_observations": len(self.result.posts),
            "soft_min": self.plan.soft_min,
            "soft_min_met": unique >= self.plan.soft_min,
            "unique_target": self.plan.unique_target,
            "shortfall_vs_plan": max(0, self.plan.planned_posts - unique),
            "backfilled": False,
            "repeat_snapshots": 0,
            "steps": [vars(s) for s in self.steps],
            "posts_with_multiple_sources": sum(1 for v in self.provenance.values() if len(v) > 1),
            "pages_opened": self.result.pages_opened,
            "scrolls": self.result.scrolls,
        }


def count_unique(posts, *, exclude_sources: tuple[str, ...] = (REPEAT_SNAPSHOT,)) -> int:
    """重複を除いた投稿の数。再観測 (repeat snapshot) は数えない。"""

    return len({p.record.external_post_key for p in posts if p.source_type not in exclude_sources})


def _collection(step: PlanStep, plan: DailyPlan, budget: int) -> tuple[CollectionPlan, dict]:
    # スクロールはページごとに collector の上限まで (計画の max_scrolls はページ数ぶんの合計)。
    base = {"run_total": budget, "max_scrolls": sel.LIMITS["max_scrolls"]}
    if step.source_type == SOURCE_FOR_YOU:
        return CollectionPlan(for_you=True), {**base, "for_you": budget}
    if step.source_type == SOURCE_SEARCH:
        return CollectionPlan(search_queries=(step.query,)), {**base, "search": budget}
    if step.source_type == SOURCE_KNOWN_ACCOUNT:
        return CollectionPlan(known_accounts=(step.query,)), {**base, "known_account": budget}
    if step.source_type == SOURCE_TOPIC_FOR_YOU:
        topic = plan.topic_for_you
        return CollectionPlan(trending=True), {
            **base, "trending_topics_max": topic["topic_list_max"],
            "trending_topics_follow": topic["max_topics"],
            "trending_topic": topic["max_posts_per_topic"],
        }  # fmt: skip
    raise ValueError(f"unknown plan step source {step.source_type!r}")


def _surface_ok(step: PlanStep) -> bool:
    surfaces = [step.source_type]
    if step.source_type == SOURCE_TOPIC_FOR_YOU:
        surfaces.append("topic_for_you_list")
    return all(sel.surface_verification(s)["verified"] for s in surfaces)


def execute_plan(
    page: ReadOnlyPage,
    plan: DailyPlan,
    *,
    screenshot_dir: Path | None = None,
    screenshots: bool = False,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> OrchestrationResult:
    merged = CollectionResult(started_at=clock(), item_limit=plan.stage_cap)
    out = OrchestrationResult(plan=plan, result=merged)
    stopped = False
    for index, step in enumerate(plan.steps):
        if stopped:
            out.steps.append(StepOutcome(step.source_type, step.query, step.budget,
                                         "skipped", reason=STEP_SKIPPED_AFTER_LOGIN))  # fmt: skip
            continue
        if not _surface_ok(step):
            out.steps.append(StepOutcome(step.source_type, step.query, step.budget, "skipped",
                                         reason=STEP_SKIPPED_UNVERIFIED))  # fmt: skip
            continue
        room = plan.stage_cap - len(merged.posts)
        budget = min(step.budget, room)
        if budget <= 0:
            out.steps.append(StepOutcome(step.source_type, step.query, step.budget, "skipped",
                                         reason=STEP_SKIPPED_CAP))  # fmt: skip
            continue
        collection_plan, limits = _collection(step, plan, budget)
        shot_dir = (screenshot_dir / f"step{index + 1}") if screenshot_dir is not None else None
        sub = collect(page, CollectionPlan(**{**vars(collection_plan), "screenshots": screenshots}),
                      limits=limits, screenshot_dir=shot_dir, clock=clock)  # fmt: skip
        outcome = StepOutcome(step.source_type, step.query, step.budget, sub.status,
                              reason=sub.reason)  # fmt: skip
        _merge(merged, sub, index)
        if sub.status == RUN_LOGIN_REQUIRED:
            merged.status, merged.reason = RUN_LOGIN_REQUIRED, sub.reason
            merged.posts.clear()  # ログインの画面が出た実行は保存させない
            out.provenance.clear()
            stopped = True
            out.steps.append(outcome)
            continue
        before = {p.record.external_post_key for p in merged.posts}
        for post in sub.posts:
            key = post.record.external_post_key
            out.provenance.setdefault(key, []).append(
                {"step": index + 1, "source_type": post.source_type,
                 "source_query": post.source_query})  # fmt: skip
        merged.posts.extend(sub.posts)
        keys = [p.record.external_post_key for p in sub.posts]
        outcome.accepted = len(keys)
        outcome.new_unique = len({k for k in keys if k not in before})
        outcome.duplicates_of_earlier = len([k for k in keys if k in before])
        outcome.shortfall = max(0, budget - len(keys))
        out.steps.append(outcome)
    if merged.status != RUN_LOGIN_REQUIRED:
        bad = (RUN_DOM_UNRECOGNIZED, RUN_ACCOUNTING_MISMATCH, RUN_PARTIAL)
        failed = [s for s in out.steps if s.status in bad]
        if failed:
            merged.status = RUN_PARTIAL
            merged.reason = "; ".join(f"{s.source_type}:{s.query or '-'}={s.status}"
                                      for s in failed)  # fmt: skip
        else:
            merged.status = RUN_SUCCEEDED
    merged.finished_at = clock()
    return out


def _merge(merged: CollectionResult, sub: CollectionResult, index: int) -> None:
    merged.pages_opened += sub.pages_opened
    merged.scrolls += sub.scrolls
    merged.rejected += sub.rejected
    for name, path in sub.screenshots.items():
        merged.screenshots[f"step{index + 1}-{name}"] = path
    for source in sub.source_types:
        if source not in merged.source_types:
            merged.source_types.append(source)
    merged.accounting.extend(sub.accounting)
    if sub.topic_accounting is not None:
        merged.topic_accounting = sub.topic_accounting
    merged.trending_topics.extend(sub.trending_topics)


__all__ = ["REPEAT_SNAPSHOT", "STEP_SKIPPED_AFTER_LOGIN", "STEP_SKIPPED_CAP",
           "STEP_SKIPPED_UNVERIFIED", "OrchestrationResult", "StepOutcome", "count_unique",
           "execute_plan"]  # fmt: skip
