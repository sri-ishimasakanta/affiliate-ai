"""Next Article Orchestrator (C10-2 / C10-D、``next-article/1``、pure)。

「次にどの記事を作るべきか」の **計画** (PLAN) を作る。**記事は作らない。**

人のレビュー → 既存の C9-B の記事の計画の依頼 (``article_planning``) → 既存の記事の計画の承認
→ 記事、の流れはそのまま。既にある Keyword の候補は、既存の Growth Action
(``create_new_article:keyword:keyword:<id>``) と同じ識別で結ぶ (2 つで別々に管理しない)。

重なり (cannibalization) は必ず確かめる: 同じ正規化した語の記事・Keyword、既存の意図の重なりの
規則 (``compare_profiles``)、計画中・下書き中の記事、開いている記事の計画の依頼、同じクラスタで
同じ役割の記事。1 つでも当たれば ``blocked`` (gap があっても重複の記事は作らない)。
1 つの点数は作らない (成分と理由を別々に持つ)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

from app.article.cluster_plan import Vocabulary, compare_profiles, intent_profile
from app.content import content_types as ct
from app.content.text import phrase_key

NEXT_ARTICLE_SCHEMA = "next-article/1"

READY_FOR_GROWTH_REVIEW = "ready_for_growth_review"
NEEDS_SIGNALS = "needs_signals"
NEEDS_KEYWORD_PROMOTION = "needs_keyword_promotion"
BLOCKED = "blocked"
HANDOFF_STATES = (READY_FOR_GROWTH_REVIEW, NEEDS_SIGNALS, NEEDS_KEYWORD_PROMOTION, BLOCKED)


@dataclass(frozen=True)
class ExistingContent:
    """重なりを確かめる相手 (記事・計画中の記事・開いている計画の依頼)。"""

    kind: str  # article / planned_article / planning_request / keyword
    ref: str
    text: str
    status: str | None = None
    cluster_key: str | None = None
    role: str | None = None


@dataclass(frozen=True)
class NextArticleCandidate:
    identity: str
    topic: str
    keyword_id: int | None
    discovery_key: str | None
    cluster_id: str | None
    content_type: Mapping[str, object]
    cluster_role: str
    gaps_filled: tuple[str, ...]
    monetization_role: str
    rationale: tuple[str, ...]
    evidence: Mapping[str, object]
    blockers: tuple[str, ...]
    freshness: Mapping[str, object]
    cannibalization: tuple[dict, ...]
    external_refresh: tuple[str, ...]
    handoff: Mapping[str, object]
    version: str = NEXT_ARTICLE_SCHEMA

    def as_dict(self) -> dict:
        return {**asdict(self), "composite_score": None, "creates_article": False}


def cannibalization(text: str, *, cluster_key: str | None, role: str,
                    existing: Sequence[ExistingContent],
                    vocabulary: Vocabulary | None = None) -> list[dict]:
    """重なりの一覧 (空なら重ならない)。"""

    key = phrase_key(text)
    profile = intent_profile(text, vocabulary)
    hits = []
    for e in existing:
        if phrase_key(e.text) == key and e.kind != "keyword":
            hits.append({"kind": e.kind, "ref": e.ref, "rule": "same_normalized_keyword",
                         "status": e.status})
            continue
        if e.kind == "keyword":
            continue
        overlap = compare_profiles(profile, intent_profile(e.text, vocabulary))
        if overlap is not None:
            hits.append({"kind": e.kind, "ref": e.ref, "rule": "intent_overlap",
                         "reason": overlap.reason, "status": e.status})
        elif (cluster_key and e.cluster_key == cluster_key and e.role and e.role == role
              and role in (ct.CATEGORY_LANDING, ct.ROUNDUP, ct.COMPARISON)
              and e.kind in ("article", "planned_article")
              and set(profile.theme_tokens) & set(intent_profile(e.text, vocabulary)
                                                   .theme_tokens)):
            hits.append({"kind": e.kind, "ref": e.ref, "rule": "same_cluster_role",
                         "reason": f"the cluster already has a {role} article on a shared theme",
                         "status": e.status})
    return hits


def build_candidate(*, topic: str, keyword_id: int | None, discovery_key: str | None,
                    cluster_id: str | None, cluster_key: str | None, cluster_role: str,
                    recommendation: ct.ContentTypeRecommendation, gaps: Sequence[str],
                    components: Mapping[str, Mapping], affiliate: Mapping[str, object],
                    facts_readiness: str | None, existing: Sequence[ExistingContent],
                    scored: bool, growth: Mapping[str, object] | None,
                    vocabulary: Vocabulary | None = None) -> NextArticleCandidate:
    """1 つの候補の計画 (決定論的)。"""

    hits = cannibalization(topic, cluster_key=cluster_key, role=recommendation.role,
                           existing=existing, vocabulary=vocabulary)
    rationale: list[str] = []
    blockers: list[str] = []
    if gaps:
        rationale.append("fills " + ", ".join(gaps))
    rationale += list(recommendation.reasons)
    commercial = recommendation.family == "commercial"
    eligible = list(affiliate.get("eligible_programs") or ())
    monetization = "affiliate" if (commercial and eligible) else "supporting"
    rationale.append(f"monetization {monetization}: "
                     + (f"eligible programs {', '.join(eligible[:3])}" if eligible
                        else "no eligible affiliate program" if commercial
                        else "informational role"))
    missing = sorted(k for k, v in components.items() if v.get("state") != "usable")
    refresh = sorted({v.get("source") for v in components.values()
                      if v.get("needs_external_refresh")} - {None})
    if missing:
        blockers.append("components not usable: " + ", ".join(missing))
    if recommendation.role == ct.UNDETERMINED:
        blockers.append("content type undetermined (human review)")
    if commercial and facts_readiness not in (None, "ready"):
        blockers.append(f"reusable SaaS facts are {facts_readiness}")
    if hits:
        blockers.append("cannibalization: " + "; ".join(
            f"{h['rule']} with {h['kind']} {h['ref']}" for h in hits[:3]))
    if hits:
        state, step = BLOCKED, "resolve the overlap (merge or drop); no new article"
    elif keyword_id is None:
        state = NEEDS_KEYWORD_PROMOTION
        step = ("a human adds the keyword (scripts/run_keyword_analysis.py --keyword ... "
                "--create-missing) and approves the batched Google Ads refresh")
    elif not scored or not growth:
        state = NEEDS_SIGNALS
        step = "complete the keyword signals (e.g. competition_ease) so Growth can score it"
    else:
        state = READY_FOR_GROWTH_REVIEW
        step = (f"review Growth Action {growth.get('id')} "
                f"(manage_growth_actions.py review {growth.get('id')}); approval converts it "
                "into an article planning request (C9-B)")
    identity = (f"create_new_article:keyword:keyword:{keyword_id}" if keyword_id is not None
                else f"discovery:{discovery_key}")
    return NextArticleCandidate(
        identity=identity, topic=topic, keyword_id=keyword_id, discovery_key=discovery_key,
        cluster_id=cluster_id, content_type=recommendation.as_dict(), cluster_role=cluster_role,
        gaps_filled=tuple(gaps), monetization_role=monetization, rationale=tuple(rationale),
        evidence={"components": {k: {"state": v.get("state"), "value": v.get("value"),
                                     "provenance": v.get("provenance")}
                                 for k, v in sorted(components.items())},
                  "affiliate": dict(affiliate), "facts_readiness": facts_readiness},
        blockers=tuple(blockers),
        freshness={k: v.get("observed_at") for k, v in sorted(components.items())},
        cannibalization=tuple(hits), external_refresh=tuple(refresh),
        handoff={"readiness": state, "next_step": step,
                 "growth_opportunity_key": identity if keyword_id is not None else None,
                 "growth_action_id": (growth or {}).get("id"),
                 "growth_status": (growth or {}).get("status"),
                 "creates_article": False})


__all__ = ["BLOCKED", "ExistingContent", "HANDOFF_STATES", "NEEDS_KEYWORD_PROMOTION",
           "NEEDS_SIGNALS", "NEXT_ARTICLE_SCHEMA", "NextArticleCandidate",
           "READY_FOR_GROWTH_REVIEW", "build_candidate", "cannibalization"]
