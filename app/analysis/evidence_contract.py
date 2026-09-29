"""分析の証拠の共通の形 (C10-A、C10-B / C10-C のため、pure)。

候補 (keyword / 記事) ごとに、成分を **別々に** 持つ (1 つの合成の点数は作らない):

- keyword: search_demand / commercial_intent / trend (Google Ads、保存済み)、site_relevance /
  originality / affiliate_opportunity (手元で導く)、competition_ease (人の入力)。
- 記事: 索引の状態 (URL Inspection、保存済み)、収益の証拠 (信頼できるクリック・帰属の段階)。

成分ごとに ``state`` (usable / stale / missing / insufficient)・値・出所・観測の時刻・版・
手に入れ方 (``access``: local / cached / manual) と、新しいデータを得るのに外の取り込みが要るか
(``needs_external_refresh``) を持つ。夜の分析 (C10-C、1 日 1 回・約 80 候補) は、この印を見て
**提供元ごとにまとめて** 取り直しを計画する (候補ごと × 提供元ごとに呼ばない)。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field

USABLE = "usable"
STALE = "stale"
MISSING = "missing"
INSUFFICIENT = "insufficient"
COMPONENT_STATES = (USABLE, STALE, MISSING, INSUFFICIENT)

ACCESS_LOCAL = "local"
ACCESS_CACHED = "cached"
ACCESS_MANUAL = "manual"

#: keyword の成分 → (出所, 手に入れ方)。
KEYWORD_COMPONENTS = {
    "search_demand": ("google_ads", ACCESS_CACHED),
    "commercial_intent": ("google_ads", ACCESS_CACHED),
    "trend": ("google_ads", ACCESS_CACHED),
    "site_relevance": ("site_profile", ACCESS_LOCAL),
    "originality": ("internal_corpus", ACCESS_LOCAL),
    "affiliate_opportunity": ("affiliate_catalog", ACCESS_LOCAL),
    "competition_ease": ("manual_keyword_difficulty", ACCESS_MANUAL),
}
#: 提供元ごとに 1 回でまとめて取り直せるか (Google Ads の historical metrics は一括)。
BATCHED_PROVIDERS = {"google_ads": True}


@dataclass(frozen=True)
class ComponentEvidence:
    name: str
    state: str
    source: str
    access: str
    value: float | str | None = None
    provider: str | None = None
    observed_at: str | None = None
    version: str | None = None
    reason: str | None = None
    quality_flags: tuple[str, ...] = ()
    provenance: str | None = None

    @property
    def needs_external_refresh(self) -> bool:
        return self.access == ACCESS_CACHED and self.state in (STALE, MISSING)

    def as_dict(self) -> dict:
        return {**asdict(self), "quality_flags": list(self.quality_flags),
                "needs_external_refresh": self.needs_external_refresh}


@dataclass(frozen=True)
class CandidateEvidence:
    subject_type: str  # keyword / article
    subject_id: str
    components: dict = field(default_factory=dict)  # name -> ComponentEvidence

    def as_dict(self) -> dict:
        return {"subject_type": self.subject_type, "subject_id": self.subject_id,
                "components": {k: v.as_dict() for k, v in sorted(self.components.items())},
                "needs_external_refresh": sorted(k for k, v in self.components.items()
                                                 if v.needs_external_refresh),
                "composite_score": None}


def refresh_plan(candidates: list[CandidateEvidence]) -> dict:
    """外の取り直しの計画 (提供元ごとにまとめる。呼ばない)。"""

    by_source: dict[str, set[str]] = {}
    for c in candidates:
        for comp in c.components.values():
            if comp.needs_external_refresh:
                by_source.setdefault(comp.source, set()).add(c.subject_id)
    return {source: {"subjects": sorted(ids), "batched": BATCHED_PROVIDERS.get(source, False),
                     "calls_if_run": 1 if BATCHED_PROVIDERS.get(source) else len(ids)}
            for source, ids in sorted(by_source.items())}


def component_summary(candidates: list[CandidateEvidence]) -> dict:
    counts: dict[str, Counter] = {}
    for c in candidates:
        for name, comp in c.components.items():
            counts.setdefault(name, Counter())[comp.state] += 1
    return {k: dict(sorted(v.items())) for k, v in sorted(counts.items())}


__all__ = ["ACCESS_CACHED", "ACCESS_LOCAL", "ACCESS_MANUAL", "BATCHED_PROVIDERS",
           "COMPONENT_STATES", "CandidateEvidence", "ComponentEvidence", "INSUFFICIENT",
           "KEYWORD_COMPONENTS", "MISSING", "STALE", "USABLE", "component_summary",
           "refresh_plan"]
