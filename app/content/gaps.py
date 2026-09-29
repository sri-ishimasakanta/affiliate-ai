"""Content Gap (C10-2、``content-gap/1``、pure)。

クラスタごとに「何が足りないか」を、**既存のデータから根拠を出せるものだけ** 分類する。
記事の数が少ないだけでは gap にしない (そのクラスタに、その役割を求める根拠の語があるとき
だけ ``missing_<role>`` にする)。

- ``no_article``: クラスタに所属の語があり、公開の記事が 1 つも無い
- ``missing_pillar``: 設定の pillar の語に記事が無い
- ``missing_<role>``: その役割になる語 (keyword・計画・予備・発見の候補) がクラスタにあり、
  同じ役割の公開の記事が無い
- ``weak_internal_linking``: 最新の C6 の実行で、クラスタの記事に内部リンクの候補が残っている
- ``outdated_article``: クラスタの記事の事実 (料金など) が古い (``fact_freshness`` の境)
- ``search_index_gap``: クラスタの記事が索引されていない系 (URL Inspection、古くない調べ)
- ``monetization_gap``: 商用の役割の記事が supporting で、対象になるプログラムがある

各 gap: クラスタ・根拠・今の広がり・求める役割・理由・鮮度・止める理由 (``blockers``)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

from app.content import content_types as ct
from app.content.clusters import TopicCluster
from app.content.text import phrase_key

GAP_SCHEMA = "content-gap/1"
GAP_NO_ARTICLE = "no_article"
GAP_MISSING_PILLAR = "missing_pillar"
GAP_WEAK_LINKING = "weak_internal_linking"
GAP_OUTDATED = "outdated_article"
GAP_INDEX = "search_index_gap"
GAP_MONETIZATION = "monetization_gap"
GAP_TYPES = (GAP_NO_ARTICLE, GAP_MISSING_PILLAR,
             *[f"missing_{r}" for r in ct.STRATEGY_ROLES],
             GAP_WEAK_LINKING, GAP_OUTDATED, GAP_INDEX, GAP_MONETIZATION)  # fmt: skip
_NOT_INDEXED = frozenset({"not_indexed", "discovered_not_indexed", "crawled_not_indexed",
                          "excluded", "blocked"})  # fmt: skip


@dataclass(frozen=True)
class ContentGap:
    gap_key: str
    cluster_id: str
    gap_type: str
    expected_role: str | None
    reason: str
    evidence: tuple[dict, ...]
    current_coverage: Mapping[str, object]
    freshness: Mapping[str, object]
    blockers: tuple[str, ...] = ()
    version: str = GAP_SCHEMA

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RoleEvidence:
    """その役割を求める根拠の 1 つの語 (keyword・計画・予備・発見の候補)。"""

    text: str
    role: str
    source: str
    ref: str | None = None


def find_gaps(cluster: TopicCluster, *, role_evidence: Sequence[RoleEvidence],
              link_candidates: Mapping[int, int] | None = None,
              stale_fact_articles: Mapping[int, Sequence[str]] | None = None,
              index_states: Mapping[int, Mapping] | None = None,
              monetization: Mapping[int, Mapping] | None = None,
              source_freshness: Mapping[str, str] | None = None) -> list[ContentGap]:
    """1 つのクラスタの gap (決定論的。根拠の無いものは作らない)。"""

    link_candidates = link_candidates or {}
    stale_fact_articles = stale_fact_articles or {}
    index_states = index_states or {}
    monetization = monetization or {}
    freshness = dict(source_freshness or {})
    published = [a for a in cluster.articles if a["status"] == "published"]
    covered_keys = {phrase_key(a["keyword"]) for a in cluster.articles}
    coverage = {"published_articles": len(published), "roles_present": list(cluster.roles_present),
                "coverage_state": cluster.coverage_state, "mix": dict(cluster.mix)}
    out: list[ContentGap] = []

    def add(gap_type, reason, evidence, *, role=None, blockers=(), subject=""):
        key = f"{cluster.cluster_id}:{gap_type}" + (f":{subject}" if subject else "")
        out.append(ContentGap(key, cluster.cluster_id, gap_type, role, reason, tuple(evidence),
                              coverage, freshness, tuple(blockers)))

    uncovered_members = [m for m in cluster.members if phrase_key(m["text"]) not in covered_keys]
    if not published and cluster.members:
        add(GAP_NO_ARTICLE, "the cluster has member keywords but no published article",
            [{"text": m["text"], "source": m["source"]} for m in uncovered_members[:10]],
            blockers=(("cluster deferred in content_clusters.json",)
                      if cluster.config_status == "deferred" else ()))
    if cluster.pillar.get("keyword") and not cluster.pillar.get("covered"):
        add(GAP_MISSING_PILLAR, "the configured pillar keyword has no article",
            [{"text": cluster.pillar["keyword"], "source": "cluster_config"}], role="pillar")
    present = set(cluster.roles_present)
    by_role: dict[str, list[RoleEvidence]] = {}
    for e in role_evidence:
        if e.role in ct.STRATEGY_ROLES and phrase_key(e.text) not in covered_keys:
            by_role.setdefault(e.role, []).append(e)
    for role in ct.STRATEGY_ROLES:
        if role in present or role not in by_role:
            continue
        evidence = sorted(by_role[role], key=lambda e: (e.source, e.text))
        add(f"missing_{role}", f"{len(evidence)} term(s) in the cluster call for a {role} "
            f"article and no published {role} article exists",
            [{"text": e.text, "source": e.source, "ref": e.ref} for e in evidence[:10]],
            role=role)
    links = {a["article_id"]: link_candidates[a["article_id"]] for a in published
             if link_candidates.get(a["article_id"])}
    if links:
        add(GAP_WEAK_LINKING, f"{sum(links.values())} open internal link candidate(s) in the "
            "latest C6 run", [{"article_id": k, "open_candidates": v}
                              for k, v in sorted(links.items())])
    stale = {a["article_id"]: list(stale_fact_articles[a["article_id"]]) for a in published
             if stale_fact_articles.get(a["article_id"])}
    for aid, keys in sorted(stale.items()):
        add(GAP_OUTDATED, f"article {aid} has stale facts: {', '.join(sorted(keys)[:5])}",
            [{"article_id": aid, "stale_fact_keys": sorted(keys)}], subject=str(aid))
    for a in published:
        state = index_states.get(a["article_id"]) or {}
        if state.get("normalized_status") in _NOT_INDEXED and state.get("freshness") == "fresh":
            add(GAP_INDEX, f"article {a['article_id']} is {state['normalized_status']} "
                "(URL Inspection)", [{"article_id": a["article_id"], **dict(state)}],
                subject=str(a["article_id"]))
    for a in published:
        m = monetization.get(a["article_id"]) or {}
        if (a["strategy_role"] in ct.COMMERCIAL_ROLES and a["monetization_mode"] != "affiliate"
                and m.get("eligible_programs")):
            blockers = tuple(m.get("blockers") or ())
            add(GAP_MONETIZATION, f"article {a['article_id']} is a {a['strategy_role']} article "
                "in supporting mode although eligible affiliate programs exist",
                [{"article_id": a["article_id"], "eligible_programs":
                  list(m["eligible_programs"])}], blockers=blockers,
                subject=str(a["article_id"]))
    return out


__all__ = ["ContentGap", "GAP_INDEX", "GAP_MISSING_PILLAR", "GAP_MONETIZATION",
           "GAP_NO_ARTICLE", "GAP_OUTDATED", "GAP_SCHEMA", "GAP_TYPES", "GAP_WEAK_LINKING",
           "RoleEvidence", "find_gaps"]
