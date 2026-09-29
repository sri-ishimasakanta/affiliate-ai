"""Topic Cluster (C10-2、``topic-cluster/1``、pure)。

サイト全体の話題の広がりを、keyword ごとではなくクラスタで見る。**決定論的・規則だけ** (LLM は
使わない)。

- **登録簿** (``ClusterRegistry``): 既存の 2 つの設定を 1 つにまとめる (新しい定義は作らない):
  ``app/config/content_clusters.json`` (A〜D と、保留の E) と ``app/config/content_portfolio.json``
  (記事の計画・予備・既にある記事の、クラスタへの割り当て。E を含む)。設定が食い違うとき
  (例: E は cluster 設定では保留、portfolio では記事がある) は両方の状態を持つ。
- **識別** (``cluster_id`` = ``cluster:<id>``): 設定の ID だけで決まる。順位・日々の signal の
  小さな動きでは変わらない。今の証拠 (記事・keyword・種類の広がり) は ``evidence_fingerprint``
  として別に持つ。
- **所属**: まず設定に書かれた語 (正規化して一致)、無ければ語の主題 (``intent_profile`` の
  theme token) の重なりが **ただ 1 つ** のクラスタだけで最も大きいとき。どれとも重ならない・
  同点なら所属しない (推測で割り当てない)。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.article.cluster_plan import Vocabulary, intent_profile, load_cluster_config
from app.content import content_types as ct
from app.content.text import phrase_key, phrase_text, stable_hash

CLUSTER_SCHEMA = "topic-cluster/1"
CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
CLUSTERS_PATH = CONFIG_DIR / "content_clusters.json"
PORTFOLIO_PATH = CONFIG_DIR / "content_portfolio.json"

ROLE_PILLAR = "pillar"
ROLE_SUPPORTING = "supporting"

COVERAGE_UNCOVERED = "uncovered"
COVERAGE_PARTIAL = "partial"
COVERAGE_COVERED = "covered"


@dataclass(frozen=True)
class ClusterMember:
    text: str
    role: str
    source: str  # cluster_config / portfolio_plan / portfolio_existing / portfolio_reserve
    planned_type: str | None = None


@dataclass(frozen=True)
class ClusterDefinition:
    cluster_key: str  # 設定の ID (A〜E)
    canonical_topic: str
    priority: int | None
    config_status: str  # active / deferred (content_clusters.json の状態)
    members: tuple[ClusterMember, ...]
    theme_tokens: frozenset[str]
    deferred_reason: str | None = None

    @property
    def cluster_id(self) -> str:
        return f"cluster:{self.cluster_key}"


@dataclass(frozen=True)
class ClusterRegistry:
    clusters: tuple[ClusterDefinition, ...]
    vocabulary: Vocabulary
    versions: Mapping[str, object] = field(default_factory=dict)

    def by_key(self) -> dict[str, ClusterDefinition]:
        return {c.cluster_key: c for c in self.clusters}


def load_registry(clusters_path: Path | str = CLUSTERS_PATH,
                  portfolio_path: Path | str = PORTFOLIO_PATH) -> ClusterRegistry:
    config = load_cluster_config(clusters_path)
    portfolio = json.loads(Path(portfolio_path).read_text(encoding="utf-8"))
    vocab = config.vocabulary
    members: dict[str, list[ClusterMember]] = {}
    names: dict[str, tuple[str, int | None, str, str | None]] = {}
    for cluster in config.clusters:
        names[cluster.id] = (cluster.name, cluster.priority, "active", None)
        for k in cluster.keywords:
            members.setdefault(cluster.id, []).append(
                ClusterMember(phrase_text(k.keyword), k.role, "cluster_config"))
    for deferred in config.deferred:
        names.setdefault(deferred.id, (deferred.name, None, "deferred", deferred.reason))
    for source, rows in (("portfolio_existing", portfolio.get("existing_articles") or []),
                         ("portfolio_plan", portfolio.get("articles") or []),
                         ("portfolio_reserve", portfolio.get("reserve") or [])):
        for row in rows:
            key = row.get("cluster")
            if not key or not row.get("keyword"):
                continue
            names.setdefault(key, (f"cluster {key}", None, "portfolio_only", None))
            role = ROLE_PILLAR if row.get("role") == ROLE_PILLAR else ROLE_SUPPORTING
            members.setdefault(key, []).append(ClusterMember(
                phrase_text(row["keyword"]), role, source, row.get("article_type")))
    clusters = []
    for key in sorted(names):
        name, priority, status, reason = names[key]
        seen: dict[str, ClusterMember] = {}
        for m in members.get(key, []):
            current = seen.get(phrase_key(m.text))
            if current is None or (m.role == ROLE_PILLAR and current.role != ROLE_PILLAR):
                seen[phrase_key(m.text)] = m
        tokens: set[str] = set()
        for m in seen.values():
            tokens |= set(intent_profile(m.text, vocab).theme_tokens)
        clusters.append(ClusterDefinition(key, name, priority, status,
                                          tuple(sorted(seen.values(), key=lambda m: m.text)),
                                          frozenset(tokens), reason))
    return ClusterRegistry(tuple(clusters), vocab, {"content_clusters": config.version,
                                                    "content_portfolio": portfolio.get("version")})


@dataclass(frozen=True)
class Assignment:
    cluster_key: str | None
    basis: str  # explicit / theme_match / ambiguous / no_theme_match
    detail: str = ""


def assign_cluster(text: str, registry: ClusterRegistry) -> Assignment:
    key = phrase_key(text)
    for c in registry.clusters:
        for m in c.members:
            if phrase_key(m.text) == key:
                return Assignment(c.cluster_key, "explicit", m.source)
    tokens = set(intent_profile(text, registry.vocabulary).theme_tokens)
    overlaps = sorted(((len(tokens & c.theme_tokens), c.cluster_key) for c in registry.clusters
                       if tokens & c.theme_tokens), reverse=True)
    if not overlaps:
        return Assignment(None, "no_theme_match")
    if len(overlaps) > 1 and overlaps[0][0] == overlaps[1][0]:
        return Assignment(None, "ambiguous", ", ".join(k for n, k in overlaps
                                                       if n == overlaps[0][0]))
    best = overlaps[0][1]
    shared = sorted(tokens & registry.by_key()[best].theme_tokens)
    return Assignment(best, "theme_match", "shared theme " + ", ".join(shared))


@dataclass(frozen=True)
class KeywordFact:
    keyword_id: int
    text: str
    status: str


@dataclass(frozen=True)
class ArticleFact:
    article_id: int
    keyword_id: int | None
    keyword: str | None
    status: str
    article_type: str | None
    monetization_mode: str | None


@dataclass(frozen=True)
class TopicCluster:
    cluster_id: str
    cluster_key: str
    canonical_topic: str
    priority: int | None
    config_status: str
    members: tuple[dict, ...]
    articles: tuple[dict, ...]
    pillar: dict
    roles_present: tuple[str, ...]
    mix: Mapping[str, int]
    coverage_state: str
    identity_fingerprint: str
    evidence_fingerprint: str
    provenance: Mapping[str, object]
    observed_at: str
    version: str = CLUSTER_SCHEMA

    @property
    def supporting_articles(self) -> tuple[dict, ...]:
        return tuple(a for a in self.articles if a["cluster_role"] == ROLE_SUPPORTING)

    def as_dict(self) -> dict:
        return {**asdict(self), "supporting_articles": list(self.supporting_articles)}


_LIVE = frozenset({"planned", "drafting", "review", "approved", "published", "rewrite"})


def build_clusters(registry: ClusterRegistry, *, keywords: Sequence[KeywordFact],
                   articles: Sequence[ArticleFact], observed_at: str) -> list[TopicCluster]:
    """クラスタごとの所属・記事・広がり (決定論的)。"""

    by_key = registry.by_key()
    kw_by_id = {k.keyword_id: k for k in keywords}
    member_rows: dict[str, list[dict]] = {c.cluster_key: [] for c in registry.clusters}
    for c in registry.clusters:
        for m in c.members:
            member_rows[c.cluster_key].append({"text": m.text, "role": m.role,
                                               "source": m.source, "keyword_id": None,
                                               "basis": "explicit"})  # fmt: skip
    for k in keywords:
        a = assign_cluster(k.text, registry)
        if a.cluster_key is None:
            continue
        rows = member_rows[a.cluster_key]
        existing = next((r for r in rows if phrase_key(r["text"]) == phrase_key(k.text)), None)
        if existing is not None:
            existing["keyword_id"] = k.keyword_id
        else:
            rows.append({"text": k.text, "role": ROLE_SUPPORTING, "source": "keyword",
                         "keyword_id": k.keyword_id, "basis": a.basis})
    article_rows: dict[str, list[dict]] = {c.cluster_key: [] for c in registry.clusters}
    for art in articles:
        if art.status not in _LIVE:
            continue
        text = art.keyword or (kw_by_id[art.keyword_id].text
                               if art.keyword_id in kw_by_id else None)  # fmt: skip
        if not text:
            continue
        a = assign_cluster(text, registry)
        if a.cluster_key is None:
            continue
        member = next((m for m in by_key[a.cluster_key].members
                       if phrase_key(m.text) == phrase_key(text)), None)  # fmt: skip
        article_rows[a.cluster_key].append({
            "article_id": art.article_id, "keyword": phrase_text(text), "status": art.status,
            "article_type": art.article_type, "monetization_mode": art.monetization_mode,
            "strategy_role": ct.role_of_article(text, art.article_type),
            "cluster_role": member.role if member else ROLE_SUPPORTING, "basis": a.basis})
    out = []
    for c in registry.clusters:
        arts = sorted(article_rows[c.cluster_key], key=lambda a: a["article_id"])
        published = [a for a in arts if a["status"] == "published"]
        roles = sorted({a["strategy_role"] for a in published if a["strategy_role"]})
        pillar_member = next((m for m in c.members if m.role == ROLE_PILLAR), None)
        pillar_covered = bool(pillar_member) and any(
            phrase_key(a["keyword"]) == phrase_key(pillar_member.text) for a in published)
        mix = {"commercial": sum(1 for a in published
                                 if a["strategy_role"] in ct.COMMERCIAL_ROLES),
               "informational": sum(1 for a in published
                                    if a["strategy_role"] and a["strategy_role"]
                                    not in ct.COMMERCIAL_ROLES)}  # fmt: skip
        if not published:
            coverage = COVERAGE_UNCOVERED
        elif (pillar_member and not pillar_covered) or not (mix["commercial"]
                                                            and mix["informational"]):
            coverage = COVERAGE_PARTIAL
        else:
            coverage = COVERAGE_COVERED
        members = sorted(member_rows[c.cluster_key], key=lambda r: (r["role"] != ROLE_PILLAR,
                                                                    r["text"]))
        evidence = {"members": [(m["text"], m["keyword_id"]) for m in members],
                    "articles": [(a["article_id"], a["status"], a["strategy_role"])
                                 for a in arts]}  # fmt: skip
        out.append(TopicCluster(
            cluster_id=c.cluster_id, cluster_key=c.cluster_key, canonical_topic=c.canonical_topic,
            priority=c.priority, config_status=c.config_status, members=tuple(members),
            articles=tuple(arts),
            pillar={"keyword": pillar_member.text if pillar_member else None,
                    "covered": pillar_covered},
            roles_present=tuple(roles), mix=mix, coverage_state=coverage,
            identity_fingerprint=stable_hash({"schema": CLUSTER_SCHEMA, "id": c.cluster_id}),
            evidence_fingerprint=stable_hash(evidence),
            provenance={"config_versions": dict(registry.versions),
                        "config_status": c.config_status,
                        "deferred_reason": c.deferred_reason},
            observed_at=observed_at))
    return out


def cluster_of_texts(texts: Iterable[str], registry: ClusterRegistry) -> dict[str, Assignment]:
    return {t: assign_cluster(t, registry) for t in texts}


__all__ = ["ArticleFact", "Assignment", "CLUSTER_SCHEMA", "COVERAGE_COVERED",
           "COVERAGE_PARTIAL", "COVERAGE_UNCOVERED", "ClusterDefinition", "ClusterMember",
           "ClusterRegistry", "KeywordFact", "ROLE_PILLAR", "ROLE_SUPPORTING", "TopicCluster",
           "assign_cluster", "build_clusters", "cluster_of_texts", "load_registry"]
