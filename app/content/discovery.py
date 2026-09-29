"""Continuous Keyword Discovery (C10-2、``keyword-discovery/1``、pure)。

既存の 42 keyword だけで回し続けないために、手元にある出所から新しい語の候補を集める。
**新しい外の提供元は足さない** (保存済みのデータだけ)。

出所 (``SOURCES``): 検索クエリ (Search Console の保存済みの行)・記事の計画 / 予備
(``content_portfolio.json``)・クラスタの設定の語 (``content_clusters.json``)・人が書いた種
(``app/config/discovery_seeds.json``)。

候補は **Keyword にしない** (人が決める)。既にある Keyword と同じ語は ``existing_keyword``、
既にある記事の語と同じ・同じ意図 (``compare_profiles``) の語は ``overlaps_article`` /
``overlaps_keyword`` として残す (見えるように。新しい候補には数えない)。Google Ads の値が無い
候補は ``refresh_needs = ["google_ads"]`` (ここでは呼ばない)。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field

from app.article.cluster_plan import compare_profiles, intent_profile
from app.content.clusters import ClusterRegistry, assign_cluster
from app.content.text import phrase_key, phrase_text

DISCOVERY_SCHEMA = "keyword-discovery/1"
SRC_GSC_QUERY = "gsc_query"
SRC_PORTFOLIO_PLAN = "portfolio_plan"
SRC_PORTFOLIO_RESERVE = "portfolio_reserve"
SRC_CLUSTER_CONFIG = "cluster_config"
SRC_MANUAL_SEED = "manual_seed"
SOURCES = (SRC_GSC_QUERY, SRC_PORTFOLIO_PLAN, SRC_PORTFOLIO_RESERVE, SRC_CLUSTER_CONFIG,
           SRC_MANUAL_SEED)  # fmt: skip

STATE_NEW = "new"
STATE_EXISTING_KEYWORD = "existing_keyword"
STATE_EXISTING_ARTICLE = "existing_article"
STATE_OVERLAPS_ARTICLE = "overlaps_article"
STATE_OVERLAPS_KEYWORD = "overlaps_keyword"
DUPLICATE_STATES = (STATE_EXISTING_KEYWORD, STATE_EXISTING_ARTICLE, STATE_OVERLAPS_ARTICLE,
                    STATE_OVERLAPS_KEYWORD)  # fmt: skip
#: 語として短すぎる・長すぎるものは候補にしない (質問文など)。
MIN_CHARS = 2
MAX_CHARS = 40


@dataclass(frozen=True)
class DiscoveryObservation:
    source: str
    phrase: str
    observed_at: str | None = None
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DiscoveryCandidate:
    phrase_key: str
    phrase: str
    sources: tuple[dict, ...]
    first_seen: str | None
    last_seen: str | None
    cluster_key: str | None
    cluster_basis: str
    duplicate_state: str
    duplicate_of: dict | None
    refresh_needs: tuple[str, ...]
    evidence: dict
    version: str = DISCOVERY_SCHEMA

    @property
    def is_new(self) -> bool:
        return self.duplicate_state == STATE_NEW

    def as_dict(self) -> dict:
        return {**asdict(self), "is_new": self.is_new}


def _usable(text: str) -> bool:
    t = phrase_text(text)
    return MIN_CHARS <= len(t) <= MAX_CHARS and not t.endswith(("?", "？"))


def merge_candidates(observations: Sequence[DiscoveryObservation], *,
                     keywords: Sequence[tuple[int, str]],
                     article_keywords: Sequence[tuple[int, str]],
                     registry: ClusterRegistry) -> list[DiscoveryCandidate]:
    """観測をまとめて候補にする (同じ語は 1 つ。決定論的)。"""

    vocab = registry.vocabulary
    kw_keys = {phrase_key(t): kid for kid, t in keywords}
    art_keys = {phrase_key(t): aid for aid, t in article_keywords}
    kw_profiles = [(kid, t, intent_profile(t, vocab)) for kid, t in keywords]
    art_profiles = [(aid, t, intent_profile(t, vocab)) for aid, t in article_keywords]
    grouped: dict[str, list[DiscoveryObservation]] = {}
    for o in observations:
        if not _usable(o.phrase):
            continue
        grouped.setdefault(phrase_key(o.phrase), []).append(o)
    out = []
    for key in sorted(grouped):
        obs = sorted(grouped[key], key=lambda o: (o.source, o.observed_at or ""))
        phrase = phrase_text(obs[0].phrase)
        seen = sorted(o.observed_at for o in obs if o.observed_at)
        assignment = assign_cluster(phrase, registry)
        state, dup = STATE_NEW, None
        if key in kw_keys:
            state, dup = STATE_EXISTING_KEYWORD, {"keyword_id": kw_keys[key]}
        elif key in art_keys:
            state, dup = STATE_EXISTING_ARTICLE, {"article_id": art_keys[key]}
        else:
            profile = intent_profile(phrase, vocab)
            for aid, text, other in art_profiles:
                overlap = compare_profiles(profile, other)
                if overlap is not None:
                    state, dup = STATE_OVERLAPS_ARTICLE, {"article_id": aid, "keyword": text,
                                                          "reason": overlap.reason}
                    break
            if state == STATE_NEW:
                for kid, text, other in kw_profiles:
                    overlap = compare_profiles(profile, other)
                    if overlap is not None:
                        state, dup = STATE_OVERLAPS_KEYWORD, {"keyword_id": kid, "keyword": text,
                                                              "reason": overlap.reason}
                        break
        gsc = [o.evidence for o in obs if o.source == SRC_GSC_QUERY]
        evidence = {"gsc_impressions": sum(int(e.get("impressions") or 0) for e in gsc),
                    "gsc_clicks": sum(int(e.get("clicks") or 0) for e in gsc),
                    "gsc_pages": sorted({p for e in gsc for p in e.get("pages") or ()})}
        out.append(DiscoveryCandidate(
            phrase_key=key, phrase=phrase,
            sources=tuple({"source": o.source, "observed_at": o.observed_at,
                           **{k: v for k, v in o.evidence.items() if k != "pages"}}
                          for o in obs),
            first_seen=seen[0] if seen else None, last_seen=seen[-1] if seen else None,
            cluster_key=assignment.cluster_key, cluster_basis=assignment.basis,
            duplicate_state=state, duplicate_of=dup,
            refresh_needs=("google_ads",) if state == STATE_NEW else (), evidence=evidence))
    return out


__all__ = ["DISCOVERY_SCHEMA", "DUPLICATE_STATES", "DiscoveryCandidate", "DiscoveryObservation",
           "SOURCES", "SRC_CLUSTER_CONFIG", "SRC_GSC_QUERY", "SRC_MANUAL_SEED",
           "SRC_PORTFOLIO_PLAN", "SRC_PORTFOLIO_RESERVE", "STATE_EXISTING_ARTICLE",
           "STATE_EXISTING_KEYWORD", "STATE_NEW", "STATE_OVERLAPS_ARTICLE",
           "STATE_OVERLAPS_KEYWORD", "merge_candidates"]
