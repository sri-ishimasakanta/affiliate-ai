"""SEO 改善候補の型・証拠・判定ルール (C6、pure)。

設計上の約束:

- **不透明な合成スコアを作らない**。「SEO スコア 84/100」のような出力はしない。
  候補は「名前の付いたゲートを全部通ったか」で決まり、通らなければ出ない。
- **優先度は明示規則のみ**から決める (:mod:`app.seo.policy` の ``priority`` と
  ``priority_escalations``)。隠れた重み付けは無い。
- 候補には必ず **機械可読な証拠** を添える。どの指標を、どの期間の、いつまでの
  データで見たのかを後から再現できるようにする。
- 証拠の出所を ``evidence_strength`` で区別する。実測 (performance_derived) と
  構造的な関係 (structural)、メタデータ由来 (source_metadata) を混同しない。

DB にも外部にも触れない。入力は既に集計済みの事実だけ。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.seo.maturity import ArticleMaturity
from app.seo.policy import PRIORITY_HIGH, SeoPolicy

# -- candidate types (V1) ------------------------------------------------------
INDEXING_FOLLOWUP = "INDEXING_FOLLOWUP"
CTR_IMPROVEMENT = "CTR_IMPROVEMENT"
RANKING_IMPROVEMENT = "RANKING_IMPROVEMENT"
QUERY_EXPANSION = "QUERY_EXPANSION"
INTERNAL_LINK_OPPORTUNITY = "INTERNAL_LINK_OPPORTUNITY"
CANNIBALIZATION_REVIEW = "CANNIBALIZATION_REVIEW"
ENGAGEMENT_REVIEW = "ENGAGEMENT_REVIEW"
CONTENT_FRESHNESS_REVIEW = "CONTENT_FRESHNESS_REVIEW"

CANDIDATE_TYPES = (
    INDEXING_FOLLOWUP,
    CTR_IMPROVEMENT,
    RANKING_IMPROVEMENT,
    QUERY_EXPANSION,
    INTERNAL_LINK_OPPORTUNITY,
    CANNIBALIZATION_REVIEW,
    ENGAGEMENT_REVIEW,
    CONTENT_FRESHNESS_REVIEW,
)

# -- evidence strength ---------------------------------------------------------
#: 実測 (Search Console / GA4) から導いた証拠。
EVIDENCE_PERFORMANCE = "performance_derived"
#: ポートフォリオ構造・記事間の関係から導いた証拠 (実測ではない)。
EVIDENCE_STRUCTURAL = "structural"
#: 出典・ファクトのメタデータ (取得日など) から導いた証拠。
EVIDENCE_SOURCE_METADATA = "source_metadata"
#: ライブページ / URL Inspection の観測から導いた証拠。
EVIDENCE_INDEX_OBSERVATION = "index_observation"

# -- reason codes --------------------------------------------------------------
REASON_NOT_INDEXED_AFTER_GRACE = "NOT_INDEXED_AFTER_GRACE_PERIOD"
REASON_WEAK_CTR = "WEAK_CTR_FOR_POSITION"
REASON_RANKING_IN_OPPORTUNITY_BAND = "RANKING_IN_OPPORTUNITY_BAND"
REASON_QUERY_NOT_COVERED = "QUERY_NOT_COVERED_BY_ARTICLE"
REASON_SHARED_QUERY_ACROSS_PAGES = "SHARED_QUERY_ACROSS_PAGES"
REASON_MISSING_STRUCTURAL_LINK = "MISSING_STRUCTURAL_INTERNAL_LINK"
REASON_WEAK_ENGAGEMENT = "WEAK_ORGANIC_ENGAGEMENT"
REASON_SOURCES_AGED = "SOURCES_AGED_BEYOND_POLICY"

# -- suggested action scope (E4: 既存記事の拡張 / 将来の別記事) -----------------
SCOPE_EXPAND_EXISTING = "expand_existing_article"
SCOPE_POSSIBLE_NEW_ARTICLE = "possible_future_article"


@dataclass(frozen=True)
class SeoCandidate:
    """1 件の改善候補。証拠なしでは作れない。"""

    article_id: int
    candidate_type: str
    reason_code: str
    priority: str
    evidence_strength: str
    suggested_action: str
    evidence: dict = field(default_factory=dict)
    #: 同一評価内での重複排除・run 間の比較に使う安定キー。
    dedupe_key: str = ""

    def with_dedupe_key(self, key: str) -> SeoCandidate:
        return SeoCandidate(
            article_id=self.article_id,
            candidate_type=self.candidate_type,
            reason_code=self.reason_code,
            priority=self.priority,
            evidence_strength=self.evidence_strength,
            suggested_action=self.suggested_action,
            evidence=self.evidence,
            dedupe_key=key,
        )


def resolve_priority(
    policy: SeoPolicy, candidate_type: str, *, impressions: int = 0, article_type: str | None = None
) -> str:
    """明示規則だけで優先度を決める (隠れた重み付けは無い)。"""

    priority = policy.base_priority(candidate_type)
    for rule in policy.escalations_for(candidate_type):
        condition = rule.get("when", "")
        target = rule.get("to")
        if target is None:
            continue
        if condition == "impressions >= ctr.high_priority_impressions":
            gate = int(policy.gate("ctr", "high_priority_impressions", 10**9))
            if impressions >= gate:
                priority = target
        elif condition == "article_type == 'pricing'":
            if article_type == "pricing":
                priority = target
    return priority


# ==================== 個別の判定ルール ========================================
def evaluate_indexing_followup(
    *,
    article_id: int,
    maturity: ArticleMaturity,
    live_state: str | None,
    sitemap_state: str | None,
    google_index_state: str | None,
    index_observed_days_ago: int | None,
    policy: SeoPolicy,
) -> SeoCandidate | None:
    """技術的に健全なのに、猶予期間を過ぎても Google が認識していない場合のみ。

    1 回の古いスナップショットを恒久的な真実として扱わないため、観測が新しい
    ことを要求する (観測が古ければ「判断しない」)。
    """

    if maturity.age_days is None:
        return None
    grace = int(policy.gate("indexing", "followup_after_days", 14))
    if maturity.age_days < grace:
        return None
    if live_state != "LIVE_HEALTHY" or sitemap_state != "SITEMAP_PRESENT":
        return None
    if google_index_state in (None, "GSC_UNKNOWN", "GSC_INDEXED"):
        # 取得できていない / インデックス済みなら追撃しない。
        return None
    if index_observed_days_ago is None or index_observed_days_ago > policy.inspection_max_age_days:
        return None

    return SeoCandidate(
        article_id=article_id,
        candidate_type=INDEXING_FOLLOWUP,
        reason_code=REASON_NOT_INDEXED_AFTER_GRACE,
        priority=resolve_priority(policy, INDEXING_FOLLOWUP),
        evidence_strength=EVIDENCE_INDEX_OBSERVATION,
        suggested_action=(
            "Search Console の URL 検査からインデックス登録をリクエストし、"
            "内部リンクからの導線が足りているかを確認する"
        ),
        evidence={
            "age_days": maturity.age_days,
            "grace_period_days": grace,
            "live_state": live_state,
            "sitemap_state": sitemap_state,
            "google_index_state": google_index_state,
            "index_observed_days_ago": index_observed_days_ago,
        },
    )


def evaluate_ctr(
    *,
    article_id: int,
    maturity: ArticleMaturity,
    impressions: int,
    clicks: int,
    ctr: float | None,
    average_position: float | None,
    policy: SeoPolicy,
) -> SeoCandidate | None:
    """十分な表示回数があり、順位のわりに CTR が弱いときだけ。

    タイトル/メタが原因だと **断定しない** -- 見直す価値がある、とだけ言う。
    """

    if not maturity.search_sample_sufficient:
        return None
    gate = int(policy.gate("ctr", "minimum_impressions", 200))
    if impressions < gate or ctr is None or average_position is None:
        return None
    position_gate = float(policy.gate("ctr", "only_when_position_at_or_better_than", 20.0))
    if average_position > position_gate:
        # 順位が低すぎる場合、CTR の低さは露出位置の問題であって
        # タイトル/メタの問題とは言えない。
        return None
    weak_below = float(policy.gate("ctr", "weak_ctr_below", 0.01))
    if ctr >= weak_below:
        return None

    return SeoCandidate(
        article_id=article_id,
        candidate_type=CTR_IMPROVEMENT,
        reason_code=REASON_WEAK_CTR,
        priority=resolve_priority(policy, CTR_IMPROVEMENT, impressions=impressions),
        evidence_strength=EVIDENCE_PERFORMANCE,
        suggested_action=(
            "タイトルとメタディスクリプションが検索意図と一致しているかを確認する。"
            "CTR の低さの原因がタイトル/メタであると断定はできない"
        ),
        evidence={
            "impressions": impressions,
            "clicks": clicks,
            "ctr": ctr,
            "average_position": average_position,
            "gate_minimum_impressions": gate,
            "gate_weak_ctr_below": weak_below,
            "gate_position_at_or_better_than": position_gate,
        },
    )


def evaluate_ranking(
    *,
    article_id: int,
    maturity: ArticleMaturity,
    impressions: int,
    average_position: float | None,
    policy: SeoPolicy,
) -> SeoCandidate | None:
    """既にある程度の露出があり、順位が「あと一歩」の帯にあるときだけ。"""

    if not maturity.search_sample_sufficient:
        return None
    gate = int(policy.gate("ranking", "minimum_impressions", 100))
    if impressions < gate or average_position is None:
        return None
    low = float(policy.gate("ranking", "opportunity_position_from", 5.0))
    high = float(policy.gate("ranking", "opportunity_position_to", 30.0))
    if not low <= average_position <= high:
        return None

    return SeoCandidate(
        article_id=article_id,
        candidate_type=RANKING_IMPROVEMENT,
        reason_code=REASON_RANKING_IN_OPPORTUNITY_BAND,
        priority=resolve_priority(policy, RANKING_IMPROVEMENT),
        evidence_strength=EVIDENCE_PERFORMANCE,
        suggested_action=("上位クエリとの整合、内容の深さ、関連記事からの内部リンクを見直す"),
        evidence={
            "impressions": impressions,
            "average_position": average_position,
            "gate_minimum_impressions": gate,
            "gate_position_band": [low, high],
        },
    )


def evaluate_query_expansion(
    *,
    article_id: int,
    maturity: ArticleMaturity,
    queries: list[dict],
    body_text: str,
    policy: SeoPolicy,
) -> list[SeoCandidate]:
    """実データのクエリのうち、本文が明らかに扱っていないものだけを挙げる。

    「既存記事を広げる」か「将来の別記事」かを必ず区別する。キーワードや記事を
    自動生成することはしない。
    """

    if not maturity.old_enough_to_evaluate:
        return []
    minimum = int(policy.gate("query_expansion", "minimum_query_impressions", 50))
    position_gate = float(policy.gate("query_expansion", "minimum_query_position", 30.0))
    limit = int(policy.gate("query_expansion", "max_queries_per_article", 5))
    normalized_body = _normalize(body_text)

    out: list[SeoCandidate] = []
    for row in sorted(queries, key=lambda q: -int(q.get("impressions") or 0)):
        impressions = int(row.get("impressions") or 0)
        position = row.get("average_position")
        query = str(row.get("query") or "")
        if impressions < minimum or not query:
            continue
        if position is not None and float(position) > position_gate:
            continue
        if _covered(normalized_body, query):
            continue
        scope = SCOPE_EXPAND_EXISTING if impressions < minimum * 4 else SCOPE_POSSIBLE_NEW_ARTICLE
        out.append(
            SeoCandidate(
                article_id=article_id,
                candidate_type=QUERY_EXPANSION,
                reason_code=REASON_QUERY_NOT_COVERED,
                priority=resolve_priority(policy, QUERY_EXPANSION),
                evidence_strength=EVIDENCE_PERFORMANCE,
                suggested_action=(
                    "本文にこのクエリの意図を扱う節を足せるかを検討する"
                    if scope == SCOPE_EXPAND_EXISTING
                    else "独立した記事として扱うべきかを検討する (自動作成はしない)"
                ),
                evidence={
                    "query": query,
                    "impressions": impressions,
                    "clicks": int(row.get("clicks") or 0),
                    "average_position": position,
                    "scope": scope,
                    "gate_minimum_query_impressions": minimum,
                    "gate_minimum_query_position": position_gate,
                },
            )
        )
        if len(out) >= limit:
            break
    return out


def evaluate_engagement(
    *,
    article_id: int,
    maturity: ArticleMaturity,
    organic_sessions: int,
    engagement_rate: float | None,
    average_engagement_seconds: float | None,
    policy: SeoPolicy,
) -> SeoCandidate | None:
    """GA4 の **オーガニック** セッションが十分あるときだけ。

    総トラフィック (直接流入・SNS 含む) からオーガニック検索の品質を推測しない。
    """

    if not maturity.engagement_sample_sufficient:
        return None
    weak_rate = float(policy.gate("engagement", "weak_engagement_rate_below", 0.4))
    weak_seconds = float(policy.gate("engagement", "weak_average_engagement_seconds_below", 30.0))
    failing = []
    if engagement_rate is not None and engagement_rate < weak_rate:
        failing.append("engagement_rate")
    if average_engagement_seconds is not None and average_engagement_seconds < weak_seconds:
        failing.append("average_engagement_time_seconds")
    if not failing:
        return None

    return SeoCandidate(
        article_id=article_id,
        candidate_type=ENGAGEMENT_REVIEW,
        reason_code=REASON_WEAK_ENGAGEMENT,
        priority=resolve_priority(policy, ENGAGEMENT_REVIEW),
        evidence_strength=EVIDENCE_PERFORMANCE,
        suggested_action="導入部と見出し構成が検索意図に答えているかを確認する",
        evidence={
            "organic_sessions": organic_sessions,
            "engagement_rate": engagement_rate,
            "average_engagement_time_seconds": average_engagement_seconds,
            "failing_metrics": failing,
            "gate_minimum_organic_sessions": int(
                policy.gate("engagement", "minimum_organic_sessions", 100)
            ),
            "gate_weak_engagement_rate_below": weak_rate,
            "gate_weak_average_engagement_seconds_below": weak_seconds,
        },
    )


def evaluate_freshness(
    *,
    article_id: int,
    article_type: str | None,
    latest_source_checked_on: date | None,
    today: date,
    source_count: int,
    policy: SeoPolicy,
) -> SeoCandidate | None:
    """出典の取得日が、記事タイプごとの明示ポリシーを超えて古い場合だけ。

    「N 日経ったから古い」とは言わない -- ポリシーが定めた日数を超えたときだけ。
    トラフィック量とは独立に働く。
    """

    if latest_source_checked_on is None or source_count <= 0:
        return None
    limit = policy.freshness_days_for(article_type)
    age = (today - latest_source_checked_on).days
    if age < limit:
        return None

    return SeoCandidate(
        article_id=article_id,
        candidate_type=CONTENT_FRESHNESS_REVIEW,
        reason_code=REASON_SOURCES_AGED,
        priority=resolve_priority(policy, CONTENT_FRESHNESS_REVIEW, article_type=article_type),
        evidence_strength=EVIDENCE_SOURCE_METADATA,
        suggested_action="公式ページで料金・プラン・版数を再確認し、必要なら改訂する",
        evidence={
            "article_type": article_type,
            "latest_source_checked_on": latest_source_checked_on.isoformat(),
            "source_age_days": age,
            "gate_freshness_days": limit,
            "source_count": source_count,
        },
    )


def evaluate_cannibalization(
    *,
    query: str,
    pages: list[dict],
    policy: SeoPolicy,
) -> list[SeoCandidate]:
    """同一クエリが複数の正規ページに表示回数を生んでいる場合だけ。

    過去の n-gram 類似度は **補強証拠** であって、これ単独では候補にしない
    (この関数は実データのクエリ x ページのみを入力に取る)。
    """

    minimum = int(policy.gate("cannibalization", "minimum_shared_query_impressions", 30))
    minimum_pages = int(policy.gate("cannibalization", "minimum_pages_per_query", 2))
    eligible = [p for p in pages if int(p.get("impressions") or 0) >= minimum]
    if len(eligible) < minimum_pages:
        return []

    ranked = sorted(eligible, key=lambda p: -int(p.get("impressions") or 0))
    shared = [
        {
            "article_id": p.get("article_id"),
            "url": p.get("url"),
            "impressions": int(p.get("impressions") or 0),
            "clicks": int(p.get("clicks") or 0),
            "average_position": p.get("average_position"),
        }
        for p in ranked
    ]
    return [
        SeoCandidate(
            article_id=int(page["article_id"]),
            candidate_type=CANNIBALIZATION_REVIEW,
            reason_code=REASON_SHARED_QUERY_ACROSS_PAGES,
            priority=resolve_priority(policy, CANNIBALIZATION_REVIEW),
            evidence_strength=EVIDENCE_PERFORMANCE,
            suggested_action=("どの記事がこのクエリの主担当かを決め、他方は補助的な扱いに寄せる"),
            evidence={
                "query": query,
                "pages": shared,
                "gate_minimum_shared_query_impressions": minimum,
                "gate_minimum_pages_per_query": minimum_pages,
            },
        )
        for page in shared
        if page["article_id"] is not None
    ]


def evaluate_internal_link(
    *,
    source_article_id: int,
    target_article_id: int,
    relation: str,
    target_url: str,
    target_title: str | None,
    policy: SeoPolicy,
    performance_evidence: dict | None = None,
) -> SeoCandidate:
    """構造的な内部リンク候補 (実測ではないことを ``evidence_strength`` で示す)。"""

    return SeoCandidate(
        article_id=source_article_id,
        candidate_type=INTERNAL_LINK_OPPORTUNITY,
        reason_code=REASON_MISSING_STRUCTURAL_LINK,
        priority=resolve_priority(policy, INTERNAL_LINK_OPPORTUNITY),
        evidence_strength=(EVIDENCE_PERFORMANCE if performance_evidence else EVIDENCE_STRUCTURAL),
        suggested_action=(
            f"本文の文脈に合う箇所から「{target_title or target_url}」への内部リンクを "
            "1 本足せるかを検討する (自動では追加しない)"
        ),
        evidence={
            "target_article_id": target_article_id,
            "target_url": target_url,
            "relation": relation,
            "performance_evidence": performance_evidence,
        },
    )


# -- helpers -------------------------------------------------------------------
def _normalize(text: str) -> str:
    return "".join(ch for ch in (text or "").lower() if not ch.isspace())


def _covered(normalized_body: str, query: str) -> bool:
    """クエリの構成語が本文に十分現れているか (粗い被覆判定)。"""

    terms = [t for t in query.lower().split() if t]
    if not terms:
        return False
    hits = sum(1 for term in terms if term.replace(" ", "") in normalized_body)
    return hits == len(terms)


def is_high_priority(candidate: SeoCandidate) -> bool:
    return candidate.priority == PRIORITY_HIGH
