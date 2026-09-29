"""成長の証拠 (Growth Evidence) と、次の行動の候補 (C9、``growth-analysis/1``、pure)。

既存の計測を **作り直さない**。SEO (C6)・収益 (C7)・計測 (C8)・Threads (T6.5) の結果を、記事・
キーワードごとの証拠にまとめ、観測できる状況を行動につながる型に分け、次の行動の **候補** を作る。
**行動はしない** (候補を作るまで)。

設計の約束:

- 欠測は 0 にしない (``None``)。出所ごとに ``unavailable`` / ``stale`` / ``insufficient`` /
  ``usable`` を持ち、出所と鮮度と来歴を追えるようにする。
- 出所ごとの成熟度は、その出所の既存の規則 (SEO の成熟度・収益の成熟度・T6.5 の証拠の段階) を
  使う。Threads の本数の閾値を SEO やアフィリエイトに流用しない。
- 勝ち負けの判定を作らない。「これをすれば伸びる」と言わない (観測された状況と、試す価値の
  ある行動の候補だけ)。
- 1 つの総合点で並べない。優先の成分 (証拠の強さ・機会の大きさ・急ぎ・手間・収益との関係) を
  別々に持ち、並べ方もその成分の順で示す。キーワードの機会スコアを総合点に使わない。
- 報酬は記事に帰属できない限り、記事の収益として扱わない (プログラム単位のまま)。
- 信頼できる計測開始より前のアフィリエイトのクリック (自分たちの検査のクリック) は、読者の
  行動として使わない (数は監査のために残す)。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field

SCHEMA_VERSION = "growth-analysis/1"

# -- 出所の状態 -------------------------------------------------------------------------------
UNAVAILABLE = "unavailable"
STALE = "stale"
INSUFFICIENT = "insufficient"
USABLE = "usable"
SOURCE_STATES = (UNAVAILABLE, STALE, INSUFFICIENT, USABLE)

SOURCES = ("seo", "ga4", "affiliate", "commission", "keyword", "threads", "index")
#: 読者の行動の出所 (全体の証拠の段階を決める)。index・keyword・commission は入れない。
BEHAVIORAL_SOURCES = ("seo", "ga4", "affiliate", "threads")

# -- 全体の証拠の段階 (T6.5 と同じ語。ただし決め方はこの層の規則) ----------------------------------
EVIDENCE_INSUFFICIENT = "insufficient_data"
EVIDENCE_HYPOTHESIS = "hypothesis"
EVIDENCE_PRELIMINARY = "preliminary"
EVIDENCE_DESCRIPTIVE = "descriptive"
#: 行動ではなく構造 (リンクが無い・追跡が無い・投稿が無い) から分かること。
EVIDENCE_STRUCTURAL = "structural"
EVIDENCE_STATES = (EVIDENCE_INSUFFICIENT, EVIDENCE_HYPOTHESIS, EVIDENCE_PRELIMINARY,
                   EVIDENCE_DESCRIPTIVE, EVIDENCE_STRUCTURAL)  # fmt: skip
#: 証拠の強さの順 (行動の証拠は構造だけの証拠より前)。名前は段階そのもので示す (low/high にしない)。
_EVIDENCE_RANK = {EVIDENCE_INSUFFICIENT: 0, EVIDENCE_STRUCTURAL: 1, EVIDENCE_HYPOTHESIS: 2,
                  EVIDENCE_PRELIMINARY: 3, EVIDENCE_DESCRIPTIVE: 4}  # fmt: skip

# -- 観測の型 (opportunity) ------------------------------------------------------------------
SEARCH_VISIBILITY = "search_visibility_opportunity"
CONTENT_REFRESH = "content_refresh_candidate"
MONETIZATION = "monetization_opportunity"
AFFILIATE_INTEREST = "affiliate_interest_signal"
THREADS_REPROMOTION = "threads_repromotion_candidate"
THREADS_ALTERNATIVE_ANGLE = "threads_alternative_angle_candidate"
NEW_CONTENT = "new_content_opportunity"
INSUFFICIENT_EVIDENCE = "insufficient_evidence"
DATA_QUALITY = "data_quality_issue"
GROWTH_LANE = "growth_lane_due"
PATTERNS = (SEARCH_VISIBILITY, CONTENT_REFRESH, MONETIZATION, AFFILIATE_INTEREST,
            THREADS_REPROMOTION, THREADS_ALTERNATIVE_ANGLE, NEW_CONTENT, INSUFFICIENT_EVIDENCE,
            DATA_QUALITY, GROWTH_LANE)  # fmt: skip

# -- 行動の候補 ------------------------------------------------------------------------------
CREATE_NEW_ARTICLE = "create_new_article"
UPDATE_EXISTING_ARTICLE = "update_existing_article"
IMPROVE_SEARCH_SNIPPET = "improve_search_snippet"
REVIEW_INTERNAL_LINKS = "review_internal_links"
REVIEW_AFFILIATE_PLACEMENT = "review_affiliate_placement"
CREATE_REGULAR_THREADS_POST = "create_regular_threads_post"
CREATE_THREADS_ALTERNATIVE_ANGLE = "create_threads_alternative_angle"
CREATE_GROWTH_POST = "create_growth_post"
WAIT_FOR_MORE_DATA = "wait_for_more_data"
INVESTIGATE_DATA_QUALITY = "investigate_data_quality"


@dataclass(frozen=True)
class ActionSpec:
    effort: str  # none / low / medium / high
    reversible: bool
    requires_human_approval: bool
    #: 実行に外のサービスへの書き込みが要るか (WordPress / Threads / アフィリエイトの提供元)。
    external_write: str | None


ACTIONS: Mapping[str, ActionSpec] = {
    CREATE_NEW_ARTICLE: ActionSpec("high", True, True, "wordpress"),
    UPDATE_EXISTING_ARTICLE: ActionSpec("medium", True, True, "wordpress"),
    IMPROVE_SEARCH_SNIPPET: ActionSpec("low", True, True, "wordpress"),
    REVIEW_INTERNAL_LINKS: ActionSpec("low", True, True, "wordpress"),
    REVIEW_AFFILIATE_PLACEMENT: ActionSpec("low", True, True, "wordpress"),
    # 公開した投稿は、消しても見た人の記憶とインプレッションは戻らない。
    CREATE_REGULAR_THREADS_POST: ActionSpec("low", False, True, "threads"),
    CREATE_THREADS_ALTERNATIVE_ANGLE: ActionSpec("low", False, True, "threads"),
    CREATE_GROWTH_POST: ActionSpec("low", False, True, "threads"),
    WAIT_FOR_MORE_DATA: ActionSpec("none", True, False, None),
    INVESTIGATE_DATA_QUALITY: ActionSpec("low", True, False, None),
}
ACTION_TYPES = tuple(ACTIONS)
_EFFORT_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}
_LEVEL_RANK = {"unknown": 0, "low": 1, "medium": 2, "high": 3}

# -- 既存のエンジンの候補 → 型と行動 -----------------------------------------------------------
#: (型, 行動, 証拠の種類)。証拠の種類 ``behavioral`` は、そのエンジンの成熟度の門を通ったもの。
SEO_MAP: Mapping[str, tuple[str, str, str]] = {
    "CTR_IMPROVEMENT": (SEARCH_VISIBILITY, IMPROVE_SEARCH_SNIPPET, "behavioral"),
    "RANKING_IMPROVEMENT": (SEARCH_VISIBILITY, UPDATE_EXISTING_ARTICLE, "behavioral"),
    "QUERY_EXPANSION": (SEARCH_VISIBILITY, UPDATE_EXISTING_ARTICLE, "behavioral"),
    "CANNIBALIZATION_REVIEW": (SEARCH_VISIBILITY, UPDATE_EXISTING_ARTICLE, "behavioral"),
    "INTERNAL_LINK_OPPORTUNITY": (SEARCH_VISIBILITY, REVIEW_INTERNAL_LINKS, "structural"),
    "INDEXING_FOLLOWUP": (SEARCH_VISIBILITY, INVESTIGATE_DATA_QUALITY, "structural"),
    "ENGAGEMENT_REVIEW": (CONTENT_REFRESH, UPDATE_EXISTING_ARTICLE, "behavioral"),
    "CONTENT_FRESHNESS_REVIEW": (CONTENT_REFRESH, UPDATE_EXISTING_ARTICLE, "structural"),
}
REVENUE_MAP: Mapping[str, tuple[str, str, str]] = {
    "MONETIZATION_COVERAGE_GAP": (MONETIZATION, REVIEW_AFFILIATE_PLACEMENT, "structural"),
    "TRACKING_SETUP_REQUIRED": (MONETIZATION, REVIEW_AFFILIATE_PLACEMENT, "structural"),
    "HIGH_TRAFFIC_UNMONETIZED": (MONETIZATION, REVIEW_AFFILIATE_PLACEMENT, "behavioral"),
    "ZERO_CLICK_REVIEW": (MONETIZATION, REVIEW_AFFILIATE_PLACEMENT, "behavioral"),
    "AFFILIATE_CLICK_THROUGH_REVIEW": (MONETIZATION, REVIEW_AFFILIATE_PLACEMENT, "behavioral"),
    "AFFILIATE_LINK_HEALTH_REVIEW": (MONETIZATION, INVESTIGATE_DATA_QUALITY, "structural"),
    "COMMISSION_SIGNAL_REVIEW": (MONETIZATION, REVIEW_AFFILIATE_PLACEMENT, "program_level"),
    "AFFILIATE_DATA_QUALITY_REVIEW": (DATA_QUALITY, INVESTIGATE_DATA_QUALITY, "data_quality"),
}
#: 最後の Threads の通常の投稿から、これだけ経てば「もう一度紹介する」候補にする (日)。
THREADS_REPROMOTE_AFTER_DAYS = 14
#: 別の切り口を試す候補にするのは、少なくとも 1 本がこの経過時間の比較に届いてから (時間)。
THREADS_ALTERNATIVE_MIN_AGE_HOURS = 24
#: キーワードのスコアがこれより古ければ ``stale`` (日)。
KEYWORD_SCORE_STALE_AFTER_DAYS = 60
#: 公開からこの日数までは「新しい記事」(Threads の紹介の急ぎに使う)。
NEW_ARTICLE_DAYS = 30


# -- 証拠の形 ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceEvidence:
    """1 つの出所の証拠 (状態・理由・鮮度・来歴・値)。値の欠測は ``None``。"""

    source: str
    state: str
    reason: str
    provenance: str
    data_through: str | None = None
    coverage_through: str | None = None
    observed_at: str | None = None
    metrics: Mapping = field(default_factory=dict)
    #: 出所ごとの証拠の段階 (Threads なら T6.5 の段階、SEO なら SEO の成熟度) をそのまま。
    maturity: str | None = None

    def as_dict(self) -> dict:
        return {**asdict(self), "metrics": dict(self.metrics)}


@dataclass(frozen=True)
class GrowthEvidence:
    subject_type: str  # article / keyword / site / program
    subject_id: str
    article_id: int | None = None
    keyword_id: int | None = None
    article: Mapping = field(default_factory=dict)
    sources: Mapping[str, SourceEvidence] = field(default_factory=dict)
    #: 既存のエンジン (SEO C6・収益 C7) がこの対象に出した候補 (そのまま、再計算しない)。
    existing_candidates: Mapping[str, tuple[dict, ...]] = field(default_factory=dict)

    @property
    def usable_sources(self) -> tuple[str, ...]:
        return tuple(s for s in BEHAVIORAL_SOURCES
                     if (self.sources.get(s) and self.sources[s].state == USABLE))  # fmt: skip

    @property
    def evidence_state(self) -> str:
        return overall_evidence(self.sources)

    def as_dict(self) -> dict:
        return {
            "subject_type": self.subject_type, "subject_id": self.subject_id,
            "article_id": self.article_id, "keyword_id": self.keyword_id,
            "article": dict(self.article),
            "sources": {k: v.as_dict() for k, v in sorted(self.sources.items())},
            "existing_candidates": {k: list(v)
                                    for k, v in sorted(self.existing_candidates.items())},
            "usable_sources": list(self.usable_sources),
            "evidence_state": self.evidence_state,
        }  # fmt: skip


def overall_evidence(sources: Mapping[str, SourceEvidence]) -> str:
    """読者の行動の出所のうち、使えるものの数で決める (出所の中の閾値は各出所の規則)。

    0 → ``insufficient_data`` / 1 → ``hypothesis`` / 2 → ``preliminary`` /
    3 以上 → ``descriptive`` (それでも記述だけで、原因は言わない)。
    """

    usable = sum(1 for s in BEHAVIORAL_SOURCES if sources.get(s) and sources[s].state == USABLE)
    return (EVIDENCE_INSUFFICIENT, EVIDENCE_HYPOTHESIS, EVIDENCE_PRELIMINARY,
            EVIDENCE_DESCRIPTIVE)[min(usable, 3)]  # fmt: skip


# -- 出所ごとの成熟度 (各出所の既存の規則) ------------------------------------------------------

SEO_USABLE = frozenset({"sufficient_search_sample"})
GA4_USABLE = frozenset({"sufficient_engagement_sample"})
REVENUE_USABLE = frozenset({"measurable"})
REVENUE_NOT_TRACKED = frozenset({"not_monetizable", "monetization_setup_incomplete"})


def seo_source(gsc: Mapping | None, *, maturity: str | None, freshness: str,
               coverage_through: str | None,
               maturity_reason: str = "") -> SourceEvidence:  # fmt: skip
    """GSC。状態は C6 の成熟度 (``assess_maturity``) と取り込みの鮮度から。"""

    gsc = dict(gsc or {})
    metrics = {k: gsc.get(k) for k in ("impressions", "clicks", "ctr", "average_position",
                                        "query_count")}  # fmt: skip
    if freshness == UNAVAILABLE:
        state, reason = UNAVAILABLE, "search console is not imported"
    elif freshness == STALE:
        state, reason = STALE, "search console imports are stale"
    elif maturity in SEO_USABLE:
        state, reason = USABLE, "search sample passes the C6 gate"
    else:
        state, reason = INSUFFICIENT, maturity_reason or f"search maturity: {maturity}"
    return SourceEvidence("seo", state, reason, "SeoImprovementCandidateService.evaluate (C6)",
                          data_through=gsc.get("data_through"),
                          coverage_through=coverage_through, metrics=metrics,
                          maturity=maturity)  # fmt: skip


def ga4_source(ga4: Mapping | None, *, maturity: str | None, freshness: str,
               coverage_through: str | None,
               maturity_reason: str = "") -> SourceEvidence:  # fmt: skip
    ga4 = dict(ga4 or {})
    metrics = {k: ga4.get(k) for k in ("sessions", "organic_sessions", "engagement_rate",
                                        "average_engagement_time_seconds")}  # fmt: skip
    if not ga4.get("configured") or freshness == UNAVAILABLE:
        state, reason = UNAVAILABLE, "GA4 is not configured or not imported"
    elif freshness == STALE:
        state, reason = STALE, "GA4 imports are stale"
    elif maturity in GA4_USABLE:
        state, reason = USABLE, "engagement sample passes the C6 gate"
    else:
        state, reason = INSUFFICIENT, maturity_reason or f"engagement maturity: {maturity}"
    return SourceEvidence("ga4", state, reason, "SeoImprovementCandidateService.evaluate (C6)",
                          data_through=ga4.get("data_through"),
                          coverage_through=coverage_through, metrics=metrics,
                          maturity=maturity)  # fmt: skip


def affiliate_source(revenue: Mapping | None, *, freshness: str, data_through: str | None,
                     trusted_start: str | None) -> SourceEvidence:  # fmt: skip
    """アフィリエイトのクリック。**信頼できる計測開始より後** のクリックだけを行動に使う。"""

    revenue = dict(revenue or {})
    maturity = revenue.get("maturity_state")
    metrics = {
        "clean_clicks": revenue.get("clean_clicks"),
        "raw_clicks": revenue.get("raw_clicks"),
        "excluded_instrumentation_clicks": revenue.get("excluded_clicks"),
        "clicks_per_100_organic_sessions": revenue.get("clicks_per_100_organic_sessions"),
        "monetized": revenue.get("monetized"),
        "linked_programs": [p.get("affiliate_program_id") if isinstance(p, Mapping) else p
                            for p in revenue.get("linked_programs") or []],
        "trusted_measurement_start_at": trusted_start,
    }  # fmt: skip
    if not revenue or maturity in REVENUE_NOT_TRACKED:
        state = UNAVAILABLE
        reason = f"no trusted click path ({maturity or 'no revenue evaluation'})"
    elif freshness == STALE:
        state, reason = STALE, "affiliate click imports are stale"
    elif maturity in REVENUE_USABLE:
        state, reason = USABLE, "click sample passes the C7 gate"
    else:
        state = INSUFFICIENT
        reason = revenue.get("maturity_reason") or f"revenue maturity: {maturity}"
    return SourceEvidence("affiliate", state, reason,
                          "RevenueOptimizationCandidateService.evaluate "
                          "+ AffiliateCleanClickService",
                          data_through=data_through, metrics=metrics,
                          maturity=maturity)  # fmt: skip


def commission_source(*, ever_had_data: bool, attribution: str | None,
                      coverage_through: str | None) -> SourceEvidence:  # fmt: skip
    """報酬。記事に帰属できない限り **記事の収益は None**。

    プログラム単位の値はサイトの側に置く (記事へ配分しない)。
    """

    metrics = {"attribution_scope": attribution or "unavailable", "article_level_revenue": None}
    if not ever_had_data:
        return SourceEvidence("commission", UNAVAILABLE, "no commission data yet",
                              "collect_source_freshness (make_commissions)",
                              coverage_through=coverage_through, metrics=metrics)  # fmt: skip
    return SourceEvidence("commission", UNAVAILABLE if attribution != "article" else USABLE,
                          "commissions are program-level only; not attributed to this article"
                          if attribution != "article" else "article-level attribution",
                          "RevenueOptimizationCandidateService (program_commissions)",
                          coverage_through=coverage_through, metrics=metrics)  # fmt: skip


def keyword_source(score: Mapping | None, *, age_days: int | None) -> SourceEvidence:
    if not score:
        return SourceEvidence("keyword", UNAVAILABLE, "no keyword score",
                              "KeywordScoreRepository.get_latest")  # fmt: skip
    stale = age_days is not None and age_days > KEYWORD_SCORE_STALE_AFTER_DAYS
    return SourceEvidence(
        "keyword", STALE if stale else USABLE,
        f"score is {age_days} day(s) old" + (" (stale)" if stale else ""),
        "KeywordScoreRepository.get_latest", observed_at=score.get("created_at"),
        metrics=dict(score), maturity=score.get("input_source"),
    )  # fmt: skip


def index_source(row: Mapping | None, *, observed_at: str | None) -> SourceEvidence:
    """索引の状態。保存済みの ``check_indexability`` の結果だけ (外に問い合わせない)。"""

    if not row:
        return SourceEvidence("index", UNAVAILABLE, "no saved indexability observation",
                              "operations_step_runs[check_indexability]")  # fmt: skip
    state = row.get("google_index_state")
    known = bool(state) and state != "GSC_UNKNOWN"
    return SourceEvidence(
        "index", USABLE if known else INSUFFICIENT,
        f"google index state {state}" if known else "live/sitemap only (no URL inspection)",
        "operations_step_runs[check_indexability]", observed_at=observed_at,
        metrics={k: row.get(k) for k in ("live_state", "sitemap_state", "google_index_state")},
        maturity=state,
    )  # fmt: skip


def threads_source(posts: Sequence[Mapping], *, open_proposals: Sequence[int] = (),
                   as_of_days: Mapping[int, float] | None = None) -> SourceEvidence:  # fmt: skip
    """Threads (通常の投稿だけ。Growth は記事に結びつけない)。段階は T6.5 の評価のまま。"""

    regular = [p for p in posts if p.get("lane") == "regular"]
    if not regular:
        return SourceEvidence("threads", UNAVAILABLE, "no regular Threads post for this article",
                              "ThreadsPerformanceAnalysisService.report (T6.5)",
                              metrics={"publications": 0, "open_proposals": list(open_proposals)})
    order = ["insufficient_data", "hypothesis", "preliminary", "descriptive"]
    statuses = [(p.get("evaluation") or {}).get("status") or "insufficient_data" for p in regular]
    best = max(statuses, key=lambda s: order.index(s) if s in order else 0)
    latest = max(regular, key=lambda p: p.get("published_at") or "")
    reach = [((p.get("evaluation") or {}).get("components") or {}).get("reach", {})
             .get("percentile_rank") for p in regular]  # fmt: skip
    metrics = {
        "publications": len(regular),
        "usable_insight_posts": sum(1 for p in regular
                                    if (p.get("completeness") or {}).get("status")
                                    in ("complete", "partial")),
        "evaluated_posts": sum(1 for s in statuses if s != "insufficient_data"),
        "latest_published_at": latest.get("published_at"),
        "latest_age_hours": latest.get("age_hours"),
        "oldest_age_hours": max((p.get("age_hours") or 0) for p in regular),
        "angles_tried": sorted({p.get("angle") for p in regular if p.get("angle")}),
        "reach_percentile_ranks": [r for r in reach if r is not None],
        "latest_views": (latest.get("latest_metrics") or {}).get("views"),
        "open_proposals": list(open_proposals),
    }  # fmt: skip
    state = USABLE if best != "insufficient_data" else INSUFFICIENT
    return SourceEvidence("threads", state,
                          f"{len(regular)} regular post(s); best evaluation {best}",
                          "ThreadsPerformanceAnalysisService.report (T6.5)",
                          observed_at=latest.get("published_at"), metrics=metrics,
                          maturity=best)  # fmt: skip


# -- 観測の型と行動の候補 -------------------------------------------------------------------------


@dataclass(frozen=True)
class Opportunity:
    pattern: str
    action_type: str
    basis: str  # behavioral / structural / program_level / data_quality
    reason: str
    evidence: Mapping = field(default_factory=dict)
    source_engine: str | None = None
    source_priority: str | None = None
    #: 判断に意味のある、時間が経つだけでは変わらない値 (``evidence_fingerprint`` に入る)。
    #: 経過日数・views・順位の値そのもの・data-through の日付は入れない (表示の ``evidence`` だけ)。
    material: Mapping = field(default_factory=dict)
    #: 同じ対象・同じ行動の中の別の機会 (例: Threads の切り口)。
    variant: str | None = None


IDENTITY_SCHEMA = "growth-action-identity/1"
#: 行動ごとに、判断に使う出所 (指紋にはこの出所の状態だけを入れる。ほかの出所の状態が変わっても
#: その行動の版は変わらない。例: Threads の状態の変化で内部リンクの候補が新しい版にならない)。
ACTION_EVIDENCE_SOURCES: Mapping[str, tuple[str, ...]] = {
    CREATE_NEW_ARTICLE: ("keyword", "seo"),
    UPDATE_EXISTING_ARTICLE: ("seo", "ga4", "index"),
    IMPROVE_SEARCH_SNIPPET: ("seo", "ga4", "index"),
    REVIEW_INTERNAL_LINKS: ("seo", "ga4", "index"),
    REVIEW_AFFILIATE_PLACEMENT: ("affiliate", "seo", "ga4", "commission"),
    CREATE_REGULAR_THREADS_POST: ("threads",),
    CREATE_THREADS_ALTERNATIVE_ANGLE: ("threads",),
    CREATE_GROWTH_POST: (),
    WAIT_FOR_MORE_DATA: BEHAVIORAL_SOURCES,
    INVESTIGATE_DATA_QUALITY: (),
}


def _sha(payload) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=str)  # fmt: skip
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GrowthActionCandidate:
    action_type: str
    subject_type: str
    subject_id: str
    article_id: int | None
    keyword_id: int | None
    patterns: tuple[str, ...]
    evidence: tuple[dict, ...]
    rationale: str
    evidence_state: str
    prerequisites: tuple[str, ...]
    blockers: tuple[str, ...]
    freshness: Mapping
    effort: str
    reversible: bool
    requires_human_approval: bool
    external_write_required: str | None
    priority: Mapping[str, Mapping]
    variant: str | None = None
    material: tuple[dict, ...] = ()
    #: 出所ごとの状態 (``usable`` など。鮮度は状態として入る。日付は ``freshness`` の表示だけ)。
    source_states: Mapping[str, str] = field(default_factory=dict)

    @property
    def opportunity_key(self) -> str:
        """同じ機会 (同じ対象・同じ行動・同じ変種)。証拠が変わっても同じ。"""

        parts = [self.action_type, self.subject_type, self.subject_id]
        if self.variant:
            parts.append(self.variant)
        return ":".join(parts)

    @property
    def evidence_fingerprint(self) -> str:
        """判断に使った証拠 (``material`` と、この行動に関係する出所の状態) の sha256。

        全体の証拠の段階 (``evidence_state``) は表示だけ (関係の無い出所で変わるため入れない)。
        """

        return _sha({"schema": IDENTITY_SCHEMA,
                     "source_states": dict(sorted(self.source_states.items())),
                     "material": sorted((dict(m) for m in self.material),
                                        key=lambda m: json.dumps(m, sort_keys=True,
                                                                 default=str))})  # fmt: skip

    @property
    def candidate_fingerprint(self) -> str:
        """機会 + 証拠 + 行動の意味 (承認・外への書き込み・戻せるか・前提・止める理由)。"""

        return _sha({"schema": IDENTITY_SCHEMA, "opportunity_key": self.opportunity_key,
                     "evidence_fingerprint": self.evidence_fingerprint,
                     "patterns": list(self.patterns), "effort": self.effort,
                     "reversible": self.reversible,
                     "requires_human_approval": self.requires_human_approval,
                     "external_write_required": self.external_write_required,
                     "prerequisites": list(self.prerequisites),
                     "blockers": list(self.blockers)})  # fmt: skip

    @property
    def sort_key(self) -> tuple:
        """並べ方 (成分の順。1 つの点数にはしない)。"""

        p = self.priority
        return (-p["evidence_strength"]["rank"], -p["potential_opportunity"]["rank"],
                -p["monetization_relevance"]["rank"], p["effort"]["rank"],
                -p["recency_urgency"]["rank"], self.action_type, self.subject_id)  # fmt: skip

    def as_dict(self) -> dict:
        out = asdict(self)
        out["opportunity_key"] = self.opportunity_key
        out["evidence_fingerprint"] = self.evidence_fingerprint
        out["candidate_fingerprint"] = self.candidate_fingerprint
        out["priority_order"] = ["evidence_strength", "potential_opportunity",
                                 "monetization_relevance", "effort (lower first)",
                                 "recency_urgency"]  # fmt: skip
        return out


#: 既存のエンジンの候補の証拠のうち、時間が経つだけでは変わらない識別の値。
_STABLE_ENGINE_KEYS = ("target_article_id", "relation", "query", "affiliate_program_id", "scope",
                       "affiliate_target_id")  # fmt: skip


def _engine_material(engine: str, c: Mapping) -> dict:
    evidence = c.get("evidence") or {}
    return {"engine": engine, "candidate_type": c.get("candidate_type"),
            "reason_code": c.get("reason_code"), "priority": c.get("priority"),
            **{k: evidence.get(k) for k in _STABLE_ENGINE_KEYS if evidence.get(k) is not None},
            **({"affiliate_program_id": c.get("affiliate_program_id")}
               if c.get("affiliate_program_id") is not None else {})}  # fmt: skip


def classify(evidence: GrowthEvidence, *, angles: Sequence[str] = (),
             recent_angles: Sequence[str] = ()) -> list[Opportunity]:  # fmt: skip
    """観測できる状況を型に分ける (決定的)。原因は言わない。

    ``recent_angles``: サイト全体の直近 1〜2 本の通常の投稿の切り口 (別の切り口を選ぶとき、
    既存の弱い好みと同じく避ける)。
    """

    out: list[Opportunity] = []
    if evidence.subject_type == "article" and evidence.article.get("status") != "published":
        return out  # 公開前の記事には、成長の行動の候補を出さない (証拠の行だけ)。
    for c in evidence.existing_candidates.get("seo", ()):
        mapped = SEO_MAP.get(c.get("candidate_type"))
        if mapped is None:
            continue
        pattern, action, basis = mapped
        if c.get("candidate_type") == "QUERY_EXPANSION" and (
                (c.get("evidence") or {}).get("scope") == "possible_future_article"):
            pattern, action = NEW_CONTENT, CREATE_NEW_ARTICLE
        out.append(Opportunity(pattern, action, basis,
                               f"C6 {c.get('candidate_type')} ({c.get('reason_code')})",
                               _compact(c.get("evidence")), "seo", c.get("priority"),
                               material=_engine_material("seo", c)))
    for c in evidence.existing_candidates.get("revenue", ()):
        mapped = REVENUE_MAP.get(c.get("candidate_type"))
        if mapped is None:
            continue
        pattern, action, basis = mapped
        out.append(Opportunity(pattern, action, basis,
                               f"C7 {c.get('candidate_type')} ({c.get('reason_code')})",
                               _compact(c.get("evidence")), "revenue", c.get("priority"),
                               material=_engine_material("revenue", c)))
    if evidence.subject_type == "article":
        out += _article_patterns(evidence, angles, recent_angles)
    if evidence.subject_type == "keyword":
        score = evidence.sources.get("keyword")
        if score is not None and score.state in (USABLE, STALE):
            out.append(Opportunity(
                NEW_CONTENT, CREATE_NEW_ARTICLE, "structural",
                "a scored keyword has no article yet",
                {"opportunity_score": score.metrics.get("total_score"),
                 "score_age_state": score.state}, "keyword",
                material={"score_id": score.metrics.get("score_id"),
                          "score_state": score.state}))  # fmt: skip
    return out


def _article_patterns(evidence: GrowthEvidence, angles: Sequence[str],
                      recent_angles: Sequence[str] = ()) -> list[Opportunity]:  # fmt: skip
    out: list[Opportunity] = []
    article = evidence.article
    affiliate = evidence.sources.get("affiliate")
    if affiliate is not None and (affiliate.metrics.get("clean_clicks") or 0) > 0:
        out.append(Opportunity(
            AFFILIATE_INTEREST, REVIEW_AFFILIATE_PLACEMENT, "behavioral",
            f"{affiliate.metrics['clean_clicks']} trusted affiliate click(s) observed "
            "(instrumentation clicks excluded)",
            {"clean_clicks": affiliate.metrics.get("clean_clicks"),
             "excluded_instrumentation_clicks":
                 affiliate.metrics.get("excluded_instrumentation_clicks")}, "affiliate",
            material={"clean_clicks": affiliate.metrics.get("clean_clicks")}))
    threads = evidence.sources.get("threads")
    if article.get("status") == "published" and threads is not None:
        publications = threads.metrics.get("publications") or 0
        latest_age = threads.metrics.get("latest_age_hours")
        if publications == 0 or (latest_age is not None
                                 and latest_age >= THREADS_REPROMOTE_AFTER_DAYS * 24):
            out.append(Opportunity(
                THREADS_REPROMOTION, CREATE_REGULAR_THREADS_POST, "structural",
                "no regular Threads post for this article" if publications == 0 else
                f"the last regular Threads post is {round(latest_age / 24, 1)} day(s) old",
                {"publications": publications, "latest_age_hours": latest_age}, "threads",
                material={"publications": publications,
                          "trigger": "no_post" if publications == 0 else "window_passed"}))
        tried = set(threads.metrics.get("angles_tried") or ())
        untried = [a for a in angles if a not in tried]
        if (publications and untried and (threads.metrics.get("oldest_age_hours") or 0)
                >= THREADS_ALTERNATIVE_MIN_AGE_HOURS):
            ranks = threads.metrics.get("reach_percentile_ranks") or []
            median = sorted(ranks)[len(ranks) // 2] if ranks else None
            # 行動の証拠になるのは、この記事の投稿が同じ経過時間の通常の投稿の中で下半分に
            # あるときだけ (上半分なら「まだ試していない切り口がある」という構造の話)。
            weak = threads.state == USABLE and median is not None and median < 0.5
            # 提案する切り口は 1 つ (変種)。サイト全体の直近 1〜2 本と同じ切り口は後ろへ。
            preferred = [a for a in untried if a not in recent_angles] or untried
            angle = preferred[0]
            out.append(Opportunity(
                THREADS_ALTERNATIVE_ANGLE, CREATE_THREADS_ALTERNATIVE_ANGLE,
                "behavioral" if weak else "structural",
                f"{len(tried)} angle(s) tried for this article; next untried angle: {angle}"
                + (f"; reach rank median {median} among equal-age regular posts"
                   if median is not None else ""),
                {"angles_tried": sorted(tried), "untried_angles": untried, "angle": angle,
                 "reach_rank_median": median, "threads_evidence": threads.maturity},
                "threads",
                material={"angle": angle, "angles_tried": sorted(tried),
                          "rank_band": None if median is None
                          else "below_median" if median < 0.5 else "not_below_median"},
                variant=f"angle={angle}"))  # fmt: skip
    behavioral = [o for o in out if o.basis == "behavioral"]
    if not behavioral and evidence.evidence_state == EVIDENCE_INSUFFICIENT:
        missing = {k: v.reason for k, v in sorted(evidence.sources.items())
                   if k in BEHAVIORAL_SOURCES and v.state != USABLE}
        out.append(Opportunity(INSUFFICIENT_EVIDENCE, WAIT_FOR_MORE_DATA, "data_quality",
                               "no reader-behaviour source is usable yet", missing, None,
                               material={k: evidence.sources[k].state for k in sorted(missing)}))
    return out


def build_candidates(evidence: GrowthEvidence, opportunities: Iterable[Opportunity], *,
                     context: Mapping | None = None) -> list[GrowthActionCandidate]:  # fmt: skip
    """型 → 行動の候補 (同じ行動は 1 つにまとめる)。**実行しない。**"""

    context = context or {}
    grouped: dict[str, list[Opportunity]] = {}
    for o in opportunities:
        grouped.setdefault(o.action_type, []).append(o)
    out = []
    for action, items in sorted(grouped.items()):
        spec = ACTIONS[action]
        variants = sorted({o.variant for o in items if o.variant})
        state = _candidate_evidence(evidence, items)
        blockers, prerequisites = _blockers(evidence, action, items, context)
        out.append(GrowthActionCandidate(
            action_type=action, subject_type=evidence.subject_type,
            subject_id=evidence.subject_id, article_id=evidence.article_id,
            keyword_id=evidence.keyword_id,
            patterns=tuple(sorted({o.pattern for o in items})),
            evidence=tuple({"pattern": o.pattern, "basis": o.basis, "reason": o.reason,
                            "source_engine": o.source_engine,
                            "source_priority": o.source_priority, "evidence": dict(o.evidence)}
                           for o in sorted(items, key=lambda o: (o.pattern, o.reason))),
            rationale=_rationale(action, items),
            evidence_state=state,
            prerequisites=prerequisites, blockers=blockers,
            freshness={k: {"state": v.state, "data_through": v.data_through,
                           "coverage_through": v.coverage_through,
                           "observed_at": v.observed_at}
                       for k, v in sorted(evidence.sources.items())},
            effort=spec.effort, reversible=spec.reversible,
            requires_human_approval=spec.requires_human_approval,
            external_write_required=spec.external_write,
            priority=_priority(evidence, action, items, state, context),
            variant=variants[0] if variants else None,
            material=tuple({"pattern": o.pattern, "basis": o.basis, **dict(o.material)}
                           for o in sorted(items, key=lambda o: (o.pattern, o.reason))),
            source_states={k: v.state for k, v in sorted(evidence.sources.items())
                           if k in ACTION_EVIDENCE_SOURCES.get(action, ())},
        ))  # fmt: skip
    return out


def _candidate_evidence(evidence: GrowthEvidence, items: Sequence[Opportunity]) -> str:
    if any(o.pattern == INSUFFICIENT_EVIDENCE for o in items):
        return EVIDENCE_INSUFFICIENT
    if all(o.basis in ("structural", "program_level", "data_quality") for o in items):
        return EVIDENCE_STRUCTURAL
    overall = evidence.evidence_state
    # 行動の根拠がある以上、少なくとも仮説 (1 つの出所の門を通った)。
    return overall if overall != EVIDENCE_INSUFFICIENT else EVIDENCE_HYPOTHESIS


def _blockers(evidence: GrowthEvidence, action: str, items: Sequence[Opportunity],
              context: Mapping) -> tuple[tuple[str, ...], tuple[str, ...]]:  # fmt: skip
    blockers: list[str] = []
    prerequisites: list[str] = []
    threads = evidence.sources.get("threads")
    if action in (CREATE_REGULAR_THREADS_POST, CREATE_THREADS_ALTERNATIVE_ANGLE):
        open_ = (threads.metrics.get("open_proposals") if threads else None) or []
        if open_:
            blockers.append(f"an open Threads proposal already exists for this article "
                            f"(#{', #'.join(str(i) for i in open_)})")
        prerequisites.append("human approval of the proposal (existing approval flow)")
    if action in (UPDATE_EXISTING_ARTICLE, IMPROVE_SEARCH_SNIPPET, REVIEW_INTERNAL_LINKS,
                  REVIEW_AFFILIATE_PLACEMENT, CREATE_NEW_ARTICLE):
        prerequisites.append("a change request approved by a human (existing change flow)")
    if any(o.evidence.get("reason_code") == "PROGRAM_HAS_NO_TRACKING_URL" for o in items) or any(
            "TRACKING_SETUP_REQUIRED" in o.reason for o in items):
        blockers.append("tracking setup at the affiliate provider is a human step")
    for source, v in sorted(evidence.sources.items()):
        if v.state == STALE and source in BEHAVIORAL_SOURCES:
            blockers.append(f"{source} data is stale; refresh imports before acting")
    if action == CREATE_NEW_ARTICLE and context.get("keyword_status") in ("assigned",):
        blockers.append("the keyword is already assigned")
    return tuple(blockers), tuple(prerequisites)


def _level(rank: int, reason: str) -> dict:
    name = {0: "unknown", 1: "low", 2: "medium", 3: "high"}[max(0, min(rank, 3))]
    return {"level": name, "rank": max(0, min(rank, 3)), "reason": reason}


def _priority(evidence: GrowthEvidence, action: str, items: Sequence[Opportunity], state: str,
              context: Mapping) -> dict:  # fmt: skip
    engine = [o.source_priority for o in items if o.source_priority]
    if engine:
        potential = _level(max(_LEVEL_RANK.get(p, 0) for p in engine),
                           "priority from the source engine (C6/C7)")
    elif context.get("keyword_percentile") is not None and action == CREATE_NEW_ARTICLE:
        pct = context["keyword_percentile"]
        potential = _level(3 if pct >= 0.75 else 2 if pct >= 0.25 else 1,
                           f"keyword score percentile {pct} among keywords without an article "
                           "(relative, not the score itself)")
    elif any(o.basis == "behavioral" for o in items):
        potential = _level(2, "reader behaviour observed")
    elif action == WAIT_FOR_MORE_DATA:
        potential = _level(0, "not assessable yet")
    else:
        potential = _level(1, "structural gap only")
    article = evidence.article
    age = article.get("age_days")
    stale = any(v.state == STALE for v in evidence.sources.values())
    if stale or any(o.pattern == DATA_QUALITY for o in items):
        urgency = _level(3, "data quality or stale sources block reliable decisions")
    elif any(o.pattern == CONTENT_REFRESH for o in items):
        urgency = _level(2, "content freshness")
    elif action == CREATE_REGULAR_THREADS_POST and age is not None and age <= NEW_ARTICLE_DAYS:
        urgency = _level(2, f"article is {age} day(s) old")
    else:
        urgency = _level(1, "no time pressure observed")
    if evidence.subject_type == "keyword":
        monetization = (_level(context["commercial_rank"], "keyword commercial/affiliate "
                               "components relative to other keywords")
                        if context.get("commercial_rank") else _level(0, "unknown"))  # fmt: skip
    else:
        affiliate = evidence.sources.get("affiliate")
        clean = (affiliate.metrics.get("clean_clicks") if affiliate else None) or 0
        mode = article.get("monetization_mode")
        if article.get("monetized") and clean > 0:
            monetization = _level(3, "monetized article with trusted clicks")
        elif mode == "affiliate":
            monetization = _level(2, "affiliate article")
        elif mode:
            monetization = _level(1, f"{mode} article")
        else:
            monetization = _level(0, "monetization mode unknown")
    return {
        "evidence_strength": {"level": state, "rank": _EVIDENCE_RANK.get(state, 0),
                              "reason": "evidence state (insufficient_data < structural < "
                                        "hypothesis < preliminary < descriptive)"},
        "potential_opportunity": potential,
        "recency_urgency": urgency,
        "effort": {"level": ACTIONS[action].effort,
                   "rank": _EFFORT_RANK[ACTIONS[action].effort],
                   "reason": "by action type"},
        "monetization_relevance": monetization,
    }


_RATIONALE = {
    SEARCH_VISIBILITY: "検索での見え方に改善の余地が観測された",
    CONTENT_REFRESH: "内容の鮮度・読まれ方に見直しの余地がある",
    MONETIZATION: "収益化の経路が弱い・未整備",
    AFFILIATE_INTEREST: "信頼できるアフィリエイトのクリックが観測された",
    THREADS_REPROMOTION: "Threads での紹介が無い・古い",
    THREADS_ALTERNATIVE_ANGLE: "まだ試していない切り口がある",
    NEW_CONTENT: "記事の無い機会が観測された",
    INSUFFICIENT_EVIDENCE: "判断に足りるデータがまだ無い",
    DATA_QUALITY: "データの質の確認が要る",
    GROWTH_LANE: "今日の Growth Post がまだ無い",
}


def _rationale(action: str, items: Sequence[Opportunity]) -> str:
    parts = sorted({_RATIONALE[o.pattern] for o in items})
    return ("、".join(parts) + "。試す価値のある候補で、効果は保証しない (原因は言わない)。")


def site_candidates(*, data_quality: Sequence[dict], growth_plan: Mapping | None,
                    freshness: Mapping[str, str]) -> list[GrowthActionCandidate]:  # fmt: skip
    """サイト全体の候補 (データの質・Growth の枠)。"""

    ops: list[Opportunity] = []
    for c in data_quality:
        mapped = REVENUE_MAP.get(c.get("candidate_type"))
        ops.append(Opportunity(DATA_QUALITY, INVESTIGATE_DATA_QUALITY, "data_quality",
                               f"{c.get('candidate_type') or 'measurement'} "
                               f"({c.get('reason_code') or c.get('finding')})",
                               _compact(c.get("evidence")), c.get("engine"),
                               c.get("priority") if mapped else None,
                               material={"candidate_type": c.get("candidate_type"),
                                         "reason_code": c.get("reason_code"),
                                         "finding": c.get("finding")}))
    for source, state in sorted(freshness.items()):
        if state == STALE:
            ops.append(Opportunity(DATA_QUALITY, INVESTIGATE_DATA_QUALITY, "data_quality",
                                   f"{source} imports are stale", {"source": source}, "freshness",
                                   material={"stale_source": source}))
    if growth_plan and growth_plan.get("due"):
        ops.append(Opportunity(GROWTH_LANE, CREATE_GROWTH_POST, "structural",
                               "today's Growth Post is due and not created yet",
                               {"date_jst": growth_plan.get("date_jst")}, "growth",
                               material={"date_jst": growth_plan.get("date_jst")},
                               variant=f"date={growth_plan.get('date_jst')}"))
    site = GrowthEvidence(subject_type="site", subject_id="site")
    return build_candidates(site, ops)


def sort_candidates(candidates: Iterable[GrowthActionCandidate]) -> list[GrowthActionCandidate]:
    return sorted(candidates, key=lambda c: c.sort_key)


def fingerprint(candidates: Iterable[GrowthActionCandidate]) -> str:
    """候補の集まりの指紋 (正規の JSON。同じ観測なら同じ)。"""

    blob = json.dumps([c.as_dict() for c in sort_candidates(candidates)], ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def safe_ratio(numerator, denominator) -> float | None:
    """0 で割らない (分母が 0 / 不明なら ``None``)。"""

    if numerator is None or not denominator:
        return None
    return round(numerator / denominator, 4)


def percentile_of(value: float, values: Sequence[float]) -> float | None:
    """ほかの値の中での位置 (midrank)。比べる相手が無ければ ``None``。"""

    others = list(values)
    if not others:
        return None
    below = sum(1 for v in others if v < value)
    equal = sum(1 for v in others if v == value)
    return round((below + 0.5 * equal) / len(others), 4)


def _compact(evidence: Mapping | None) -> dict:
    """候補の証拠の小さな写し (秘密らしい値・長い値は入れない)。"""

    out = {}
    for k, v in (evidence or {}).items():
        if any(s in str(k).lower() for s in ("token", "url", "tracking", "secret")):
            continue
        if isinstance(v, str | int | float | bool) or v is None:
            out[k] = v
        elif isinstance(v, list | tuple) and len(v) <= 10:
            out[k] = [x for x in v if isinstance(x, str | int | float | bool)]
    return out


__all__ = [name for name in dir() if name.isupper() or name in (
    "ActionSpec", "GrowthActionCandidate", "GrowthEvidence", "Opportunity", "SourceEvidence",
    "affiliate_source", "build_candidates", "classify", "commission_source", "fingerprint",
    "ga4_source", "index_source", "keyword_source", "overall_evidence", "percentile_of",
    "safe_ratio", "seo_source", "site_candidates", "sort_candidates", "threads_source")]
