"""外の Threads の観察の 1 日の計画 (T6.5B.2)。**計画だけ。ブラウザを開かない・DB に書かない。**

方針は ``app/config/threads_observation_policy.json`` (git で管理する。``.env`` には置かない)。

- 1 日の件数は **重複を除いた投稿の数** (unique)。目安: 最低 50・通常 90・上限 100。
  これは **目標であって約束ではない**。安全に読めた数が少なければ、少ないまま (無理に足さない)。
- 出どころごとの割り当て: for_you 25・search 20 (語を回す)・topic_for_you 15
  (おすすめのトピック 3 × 5)・known_account 30 (投稿者の基準を作るため 3 人 × 10)。
- 段階 (rollout): 0 = 5 件の確認 (済み)、1 = 約 20〜30 件の保存つきの試験 (人の許可が要る)、
  2 = 50 件/日、3 = 80〜90 件/日 (通常)、4 = 上限 100 件まで。段階ごとの割り当ては方針にある。
- custom_feed (このアカウントでは使えない) と global_trending (未確認) は計画に入れない。
  topic_for_you を global_trending の代わりにしない。
- 同じ語を毎日使わないよう、検索の語は **JST の日付から決まる順** で回す (乱数は使わない)。
- 知っているアカウントは、すでに観察した公開の投稿者から選ぶ (有名人を決め打ちしない)。
  基準がまだ足りない投稿者を先に、見つけた出どころの近さ (検索 → おすすめのトピック →
  For You) と、最初に見た順で決める。いいねの多さでは選ばない。
- 同じ投稿の再観測 (repeat snapshot) は unique の数に入れない (今は使わない。予算 0)。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from app.models.threads_observer import (
    SOURCE_FOR_YOU,
    SOURCE_KNOWN_ACCOUNT,
    SOURCE_SEARCH,
    SOURCE_TOPIC_FOR_YOU,
)
from app.social.threads.observer import selectors as sel

#: 作業ディレクトリに依らない (ほかの方針のファイルと同じく、このパッケージの位置から)。
POLICY_PATH = Path(__file__).resolve().parents[3] / "config" / "threads_observation_policy.json"
PLAN_SOURCES = (SOURCE_FOR_YOU, SOURCE_SEARCH, SOURCE_TOPIC_FOR_YOU, SOURCE_KNOWN_ACCOUNT)
#: 見つけた出どころの近さ (BizFluxLab の関心に近い順)。知っているアカウントの選び方に使う。
DISCOVERY_RELEVANCE = (SOURCE_SEARCH, SOURCE_TOPIC_FOR_YOU, SOURCE_FOR_YOU, SOURCE_KNOWN_ACCOUNT)
NOT_SUBSTITUTED = ("custom_feed", "global_trending")


class PolicyError(ValueError):
    """方針の数が上限を超える・形が違う。計画を作らない。"""


def load_policy(path: Path = POLICY_PATH) -> dict:
    policy = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_policy(policy)
    return policy


def stage_total(allocation: dict) -> int:
    return (int(allocation["for_you"])
            + int(allocation["search"]["queries"]) * int(allocation["search"]["posts_per_query"])
            + int(allocation["topic_for_you"]["topics"])
            * int(allocation["topic_for_you"]["posts_per_topic"])
            + int(allocation["known_account"]["accounts"])
            * int(allocation["known_account"]["posts_per_account"]))  # fmt: skip


def validate_policy(policy: dict) -> None:
    unique = policy["unique_posts"]
    if not unique["soft_min"] <= unique["target"] <= unique["hard_max"]:
        raise PolicyError("unique_posts must satisfy soft_min <= target <= hard_max")
    limits = policy["page_limits"]
    stages = {s["stage"]: s for s in policy["rollout"]["stages"]}
    for key, allocation in policy["allocations"].items():
        stage = int(key)
        total = stage_total(allocation)
        cap = min(int(stages[stage]["unique_posts"]), int(unique["hard_max"]))
        if total > cap:
            raise PolicyError(f"stage {stage} allocation {total} exceeds its cap {cap}")
        if int(allocation["search"]["queries"]) > int(limits["max_search_queries_per_day"]):
            raise PolicyError(f"stage {stage}: too many search queries")
        if int(allocation["topic_for_you"]["topics"]) > int(limits["max_topic_pages_per_day"]):
            raise PolicyError(f"stage {stage}: too many topic pages")
        if int(allocation["known_account"]["accounts"]) > int(limits["max_known_accounts_per_day"]):
            raise PolicyError(f"stage {stage}: too many known accounts")
        per_page = max(int(allocation["for_you"]),
                       int(allocation["search"]["posts_per_query"]),
                       int(allocation["topic_for_you"]["posts_per_topic"]),
                       int(allocation["known_account"]["posts_per_account"]))  # fmt: skip
        if per_page > int(limits["max_accepted_per_page"]):
            raise PolicyError(f"stage {stage}: a page budget exceeds max_accepted_per_page")
    normal = policy["allocations"]["3"]
    if stage_total(normal) != int(unique["target"]):
        raise PolicyError("the stage 3 (normal) allocation must equal the unique target")
    if policy["repeat_observation"]["counts_toward_unique"]:
        raise PolicyError("repeat snapshots never count toward the unique target")


def rotate_queries(pool: list[str], day: date, count: int) -> list[str]:
    """JST の日付から決まる順で ``count`` 個 (乱数なし・再現できる)。毎日 1 つずつずれる。"""

    if not pool or count <= 0:
        return []
    start = day.toordinal() % len(pool)
    return [pool[(start + i) % len(pool)] for i in range(min(count, len(pool)))]


def select_known_accounts(authors: list[dict], *, count: int, exclude: tuple[str, ...] = (),
                          unreachable: tuple[str, ...] = ()) -> list[dict]:  # fmt: skip
    """基準を作るための投稿者を ``count`` 人 (決まった規則)。

    候補: すでに観察した公開の投稿者 (自分・行けなかった投稿者を除く)。順:
    1. 基準がまだ足りない投稿者が先 (``bootstrap_needed``)
    2. 最初に見つけた出どころの近さ (search → topic_for_you → for_you → known_account)
    3. 最初に見た時刻 → 名前 (同じなら)
    いいね・フォロワーの多さは使わない。
    """

    skip = {h.lower() for h in (*exclude, *unreachable)}
    pool = [a for a in authors if a.get("author_handle") and a["author_handle"].lower() not in skip]

    def relevance(author: dict) -> int:
        source = author.get("first_source_type")
        return DISCOVERY_RELEVANCE.index(source) if source in DISCOVERY_RELEVANCE else 99

    pool.sort(key=lambda a: (not a.get("bootstrap_needed", True), relevance(a),
                             str(a.get("first_seen_at") or ""), a["author_handle"]))  # fmt: skip
    return pool[:count]


@dataclass(frozen=True)
class PlanStep:
    source_type: str
    query: str | None
    budget: int
    max_pages: int
    max_scrolls: int
    note: str | None = None


@dataclass(frozen=True)
class DailyPlan:
    day: str
    policy_version: str
    stage: int
    stage_name: str
    stage_cap: int
    soft_min: int
    unique_target: int
    hard_max: int
    query_pool_version: str
    selected_queries: tuple[str, ...]
    steps: tuple[PlanStep, ...]
    topic_for_you: dict
    known_accounts: tuple[dict, ...]
    skipped: tuple[dict, ...]
    repeat_observation: dict
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def planned_posts(self) -> int:
        return sum(s.budget for s in self.steps)

    @property
    def estimated_max_pages(self) -> int:
        return sum(s.max_pages for s in self.steps)

    @property
    def estimated_max_scrolls(self) -> int:
        return sum(s.max_scrolls for s in self.steps)

    def as_dict(self) -> dict:
        out = asdict(self)
        out["planned_posts"] = self.planned_posts
        out["estimated_max_pages"] = self.estimated_max_pages
        out["estimated_max_scrolls"] = self.estimated_max_scrolls
        out["estimated_max_accepted_posts"] = self.planned_posts
        return out


def build_daily_plan(policy: dict, *, day: date, stage: int, authors: list[dict] | None = None,
                     unreachable: tuple[str, ...] = ()) -> DailyPlan:  # fmt: skip
    """1 日の計画 (決まった規則。同じ入力なら同じ計画)。ブラウザも DB も使わない。"""

    validate_policy(policy)
    key = str(stage)
    if key not in policy["allocations"]:
        raise PolicyError(f"no allocation for stage {stage}")
    stage_info = next(s for s in policy["rollout"]["stages"] if s["stage"] == stage)
    allocation = policy["allocations"][key]
    unique = policy["unique_posts"]
    limits = policy["page_limits"]
    scrolls = int(limits["max_scrolls_per_page"])
    steps: list[PlanStep] = []
    skipped: list[dict] = []
    notes: list[str] = []

    def allowed(surface: str) -> bool:
        if sel.surface_verification(surface)["verified"]:
            return True
        skipped.append({"source_type": surface, "reason": "surface_unverified"})
        return False

    if allowed(SOURCE_FOR_YOU):
        steps.append(PlanStep(SOURCE_FOR_YOU, None, int(allocation["for_you"]), 1, scrolls))
    search = policy["sources"]["search"]
    queries = rotate_queries(list(search["query_pool"]), day, int(allocation["search"]["queries"]))
    if allowed(SOURCE_SEARCH):
        for query in queries:
            per_query = int(allocation["search"]["posts_per_query"])
            steps.append(PlanStep(SOURCE_SEARCH, query, per_query, 1, scrolls))
    topic = allocation["topic_for_you"]
    topic_plan = {"max_topics": int(topic["topics"]),
                  "max_posts_per_topic": int(topic["posts_per_topic"]),
                  "topic_list_max": int(policy["sources"]["topic_for_you"]["topic_list_max"]),
                  "selection": "topic list rank order; first topics whose page yields posts",
                  "page_topic_copied_to_posts": False}  # fmt: skip
    if allowed("topic_for_you_list") and allowed(SOURCE_TOPIC_FOR_YOU):
        steps.append(PlanStep(SOURCE_TOPIC_FOR_YOU, None,
                              topic_plan["max_topics"] * topic_plan["max_posts_per_topic"],
                              1 + topic_plan["max_topics"], topic_plan["max_topics"] * scrolls,
                              note="1 topic-list page + one page per followed topic"))  # fmt: skip
    known = allocation["known_account"]
    chosen = select_known_accounts(authors or [], count=int(known["accounts"]),
                                   exclude=tuple(policy.get("exclude_authors", ())),
                                   unreachable=unreachable)  # fmt: skip
    if allowed(SOURCE_KNOWN_ACCOUNT):
        for author in chosen:
            steps.append(PlanStep(SOURCE_KNOWN_ACCOUNT, author["author_handle"],
                                  int(known["posts_per_account"]), 1, scrolls,
                                  note="author-baseline bootstrap"))  # fmt: skip
        if len(chosen) < int(known["accounts"]):
            notes.append(f"only {len(chosen)} known-account author(s) available; the unused "
                         "known-account budget is not re-assigned (no back-fill)")  # fmt: skip
    for surface, reason in policy.get("not_collected", {}).items():
        skipped.append({"source_type": surface, "reason": reason})
    total = sum(s.budget for s in steps)
    cap = min(int(stage_info["unique_posts"]), int(unique["hard_max"]))
    if total > cap:
        raise PolicyError(f"plan total {total} exceeds the stage cap {cap}")
    pages = sum(s.max_pages for s in steps)
    if pages > int(limits["max_pages_per_run"]):
        raise PolicyError(f"plan opens {pages} pages (> max_pages_per_run)")
    return DailyPlan(
        day=day.isoformat(), policy_version=policy["policy_version"], stage=stage,
        stage_name=stage_info["name"], stage_cap=cap, soft_min=int(unique["soft_min"]),
        unique_target=int(unique["target"]), hard_max=int(unique["hard_max"]),
        query_pool_version=search["query_pool_version"], selected_queries=tuple(queries),
        steps=tuple(steps), topic_for_you=topic_plan,
        known_accounts=tuple({k: a.get(k) for k in ("author_handle", "first_source_type",
                                                     "baseline_sample", "bootstrap_needed")}
                             for a in chosen),
        skipped=tuple(skipped), repeat_observation=dict(policy["repeat_observation"]),
        notes=tuple(notes),
    )  # fmt: skip


__all__ = ["DISCOVERY_RELEVANCE", "NOT_SUBSTITUTED", "PLAN_SOURCES", "POLICY_PATH", "DailyPlan",
           "PlanStep", "PolicyError", "build_daily_plan", "load_policy", "rotate_queries",
           "select_known_accounts", "stage_total", "validate_policy"]  # fmt: skip
