"""収益最適化候補の型・証拠・判定ルール (C7、pure)。

C6 の :mod:`app.seo.candidates` と同じ約束を、収益側にも適用する:

- 不透明な合成スコアを作らない。候補は名前の付いたゲートを全部通ったときだけ出る。
- 優先度は明示規則のみから決まる。
- 証拠の出所 (``structural`` / ``behavioral`` / ``program_level`` / ``data_quality``)
  を区別し、混同しない。

**やらないこと** (根拠が無い推論はしない):

- CTA の位置・文言の良し悪しを、実験なしに判定しない。
- 帰属できない報酬から記事別の収益・EPC・転換率を作らない。
- 自分たちの計測用クリックを読者行動として扱わない。
- 公開直後にコンバージョンがゼロであることを「転換率が悪い」と呼ばない。

DB にも外部にも触れない。入力は既に集計済みの事実だけ。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.revenue.maturity import (
    ATTRIBUTION_ARTICLE,
    ATTRIBUTION_PROGRAM_ONLY,
    MEASURABLE,
    MONETIZATION_SETUP_INCOMPLETE,
    NOT_MONETIZABLE,
    RevenueMaturity,
)
from app.revenue.policy import RevenuePolicy

# -- candidate types (V1) ------------------------------------------------------
MONETIZATION_COVERAGE_GAP = "MONETIZATION_COVERAGE_GAP"
TRACKING_SETUP_REQUIRED = "TRACKING_SETUP_REQUIRED"
AFFILIATE_CLICK_THROUGH_REVIEW = "AFFILIATE_CLICK_THROUGH_REVIEW"
HIGH_TRAFFIC_UNMONETIZED = "HIGH_TRAFFIC_UNMONETIZED"
ZERO_CLICK_REVIEW = "ZERO_CLICK_REVIEW"
AFFILIATE_LINK_HEALTH_REVIEW = "AFFILIATE_LINK_HEALTH_REVIEW"
COMMISSION_SIGNAL_REVIEW = "COMMISSION_SIGNAL_REVIEW"
AFFILIATE_DATA_QUALITY_REVIEW = "AFFILIATE_DATA_QUALITY_REVIEW"

CANDIDATE_TYPES = (
    AFFILIATE_LINK_HEALTH_REVIEW,
    MONETIZATION_COVERAGE_GAP,
    TRACKING_SETUP_REQUIRED,
    HIGH_TRAFFIC_UNMONETIZED,
    ZERO_CLICK_REVIEW,
    AFFILIATE_CLICK_THROUGH_REVIEW,
    COMMISSION_SIGNAL_REVIEW,
    AFFILIATE_DATA_QUALITY_REVIEW,
)

# -- evidence basis ------------------------------------------------------------
#: DB / カタログの状態から導いた証拠 (読者の行動ではない)。
BASIS_STRUCTURAL = "structural"
#: 実際の流入・クリックから導いた証拠。
BASIS_BEHAVIORAL = "behavioral"
#: プログラム単位でしか分からない事実 (記事へは配分しない)。
BASIS_PROGRAM_LEVEL = "program_level"
#: 計測そのものの問題 (読者の行動に関する主張ではない)。
BASIS_DATA_QUALITY = "data_quality"

# -- reason codes --------------------------------------------------------------
REASON_NO_AFFILIATE_PROGRAM = "NO_AFFILIATE_PROGRAM_LINKED"
REASON_PROGRAM_WITHOUT_TRACKING_URL = "PROGRAM_HAS_NO_TRACKING_URL"
REASON_PROGRAM_NOT_ACTIVE = "PROGRAM_NOT_ACTIVE"
REASON_TARGET_MISSING = "NO_ACTIVE_AFFILIATE_TARGET"
REASON_MAPPING_MISSING = "NO_ACTIVE_SUBSTITUTION_MAPPING"
REASON_TARGET_INACTIVE = "AFFILIATE_TARGET_NOT_ACTIVE"
REASON_MAPPING_TARGET_MISMATCH = "MAPPING_REFERENCES_UNUSABLE_TARGET"
REASON_TRAFFIC_WITHOUT_MONETIZATION = "TRAFFIC_WITHOUT_MONETIZATION_PATH"
REASON_NO_CLEAN_CLICKS = "NO_TRUSTED_CLICKS_DESPITE_EXPOSURE"
REASON_LOW_CLICK_THROUGH = "LOW_TRUSTED_CLICK_THROUGH"
REASON_COMMISSION_PROGRAM_LEVEL_ONLY = "COMMISSION_ATTRIBUTION_PROGRAM_LEVEL_ONLY"
REASON_UNATTRIBUTED_CLICK_TOKENS = "UNATTRIBUTED_CLICK_TOKENS"
REASON_CLICKS_BEFORE_TRUSTED_BASELINE = "CLICKS_BEFORE_TRUSTED_BASELINE"
REASON_WINDOWS_DO_NOT_OVERLAP = "TRAFFIC_AND_CLICK_WINDOWS_DO_NOT_OVERLAP"


@dataclass(frozen=True)
class RevenueCandidate:
    """1 件の収益改善候補。証拠なしでは作れない。"""

    candidate_type: str
    reason_code: str
    priority: str
    evidence_basis: str
    suggested_action: str
    article_id: int | None = None
    affiliate_program_id: int | None = None
    evidence: dict = field(default_factory=dict)
    dedupe_key: str = ""

    def with_dedupe_key(self, key: str) -> RevenueCandidate:
        return RevenueCandidate(
            candidate_type=self.candidate_type,
            reason_code=self.reason_code,
            priority=self.priority,
            evidence_basis=self.evidence_basis,
            suggested_action=self.suggested_action,
            article_id=self.article_id,
            affiliate_program_id=self.affiliate_program_id,
            evidence=self.evidence,
            dedupe_key=key,
        )


def resolve_priority(policy: RevenuePolicy, candidate_type: str, *, sessions: int = 0) -> str:
    """明示規則だけで優先度を決める (隠れた重み付けは無い)。"""

    priority = policy.base_priority(candidate_type)
    for rule in policy.escalations_for(candidate_type):
        target = rule.get("to")
        if target is None:
            continue
        if rule.get("when") == "sessions >= traffic.minimum_sessions_for_unmonetized_review * 3":
            gate = int(policy.gate("traffic", "minimum_sessions_for_unmonetized_review", 300)) * 3
            if sessions >= gate:
                priority = target
    return priority


# ==================== 構造的な判定 (読者の行動を必要としない) ==================
def evaluate_link_health(
    *,
    article_id: int,
    monetization_mode: str | None,
    targets: list[dict],
    mappings: list[dict],
    policy: RevenuePolicy,
) -> list[RevenueCandidate]:
    """DB / runtime の状態だけで分かるリンクの不整合。

    **live の ``/go/`` を叩いて確認はしない** -- そのリクエスト自体がクリック行を
    作ってしまい、計測を汚すため。
    """

    out: list[RevenueCandidate] = []
    active_targets = [t for t in targets if t["status"] == "active"]
    inactive_targets = [t for t in targets if t["status"] != "active"]
    active_mappings = [m for m in mappings if m["status"] == "active"]
    active_target_ids = {t["id"] for t in active_targets}

    for mapping in active_mappings:
        if mapping["affiliate_link_target_id"] not in active_target_ids:
            out.append(
                RevenueCandidate(
                    candidate_type=AFFILIATE_LINK_HEALTH_REVIEW,
                    reason_code=REASON_MAPPING_TARGET_MISMATCH,
                    priority=resolve_priority(policy, AFFILIATE_LINK_HEALTH_REVIEW),
                    evidence_basis=BASIS_STRUCTURAL,
                    suggested_action=(
                        "active な mapping が参照している target が使えない状態。"
                        "target を再作成するか mapping を無効化する"
                    ),
                    article_id=article_id,
                    evidence={
                        "mapping_id": mapping["id"],
                        "affiliate_link_target_id": mapping["affiliate_link_target_id"],
                        "active_target_ids": sorted(active_target_ids),
                    },
                )
            )

    if active_targets and not active_mappings:
        out.append(
            RevenueCandidate(
                candidate_type=AFFILIATE_LINK_HEALTH_REVIEW,
                reason_code=REASON_MAPPING_MISSING,
                priority=resolve_priority(policy, AFFILIATE_LINK_HEALTH_REVIEW),
                evidence_basis=BASIS_STRUCTURAL,
                suggested_action=(
                    "active な target があるのに置換 mapping が無い。mapping を確認する"
                ),
                article_id=article_id,
                evidence={
                    "active_target_ids": sorted(active_target_ids),
                    "mapping_count": len(mappings),
                },
            )
        )

    if (
        monetization_mode in (policy.gate("link_health", "expect_target_for_modes", []) or [])
        and inactive_targets
        and not active_targets
    ):
        out.append(
            RevenueCandidate(
                candidate_type=AFFILIATE_LINK_HEALTH_REVIEW,
                reason_code=REASON_TARGET_INACTIVE,
                priority=resolve_priority(policy, AFFILIATE_LINK_HEALTH_REVIEW),
                evidence_basis=BASIS_STRUCTURAL,
                suggested_action="target が無効化されたまま。差し替えるか収益化方針を見直す",
                article_id=article_id,
                evidence={
                    "inactive_target_ids": sorted(t["id"] for t in inactive_targets),
                    "statuses": sorted({t["status"] for t in inactive_targets}),
                },
            )
        )
    return out


def evaluate_coverage(
    *,
    article_id: int,
    maturity: RevenueMaturity,
    monetization_mode: str | None,
    linked_programs: list[dict],
    policy: RevenuePolicy,
) -> list[RevenueCandidate]:
    """収益化を意図しているのに、使える導線が無い記事。

    代わりのプログラムを **でっち上げない**。何が足りないのかを区別して示すだけ。
    """

    if maturity.state != MONETIZATION_SETUP_INCOMPLETE:
        return []

    if not linked_programs:
        return [
            RevenueCandidate(
                candidate_type=MONETIZATION_COVERAGE_GAP,
                reason_code=REASON_NO_AFFILIATE_PROGRAM,
                priority=resolve_priority(policy, MONETIZATION_COVERAGE_GAP),
                evidence_basis=BASIS_STRUCTURAL,
                suggested_action=(
                    "この記事に紐づくアフィリエイトプログラムが無い。"
                    "適合するプログラムがあるかをカタログで確認する (自動では作らない)"
                ),
                article_id=article_id,
                evidence={"monetization_mode": monetization_mode, "linked_program_count": 0},
            )
        ]

    tracked = [p for p in linked_programs if p.get("tracking_url")]
    if tracked:
        # プログラムも tracking URL もあるのに target/mapping が無い状態。
        return [
            RevenueCandidate(
                candidate_type=MONETIZATION_COVERAGE_GAP,
                reason_code=REASON_TARGET_MISSING,
                priority=resolve_priority(policy, MONETIZATION_COVERAGE_GAP),
                evidence_basis=BASIS_STRUCTURAL,
                suggested_action=(
                    "tracking URL のあるプログラムが紐づいているのに、managed な "
                    "target / mapping が無い。既存の収益化フローで作成を検討する"
                ),
                article_id=article_id,
                evidence={
                    "monetization_mode": monetization_mode,
                    "tracked_program_ids": sorted(p["id"] for p in tracked),
                },
            )
        ]

    inactive = [p for p in linked_programs if p.get("status") != "active"]
    if len(inactive) == len(linked_programs):
        return [
            RevenueCandidate(
                candidate_type=MONETIZATION_COVERAGE_GAP,
                reason_code=REASON_PROGRAM_NOT_ACTIVE,
                priority=resolve_priority(policy, MONETIZATION_COVERAGE_GAP),
                evidence_basis=BASIS_STRUCTURAL,
                suggested_action=(
                    "紐づくプログラムがいずれも active でない。カタログ側の状態を確認する"
                ),
                article_id=article_id,
                evidence={
                    "monetization_mode": monetization_mode,
                    "program_statuses": sorted({p.get("status") for p in linked_programs}),
                },
            )
        ]
    return []


def evaluate_tracking_setup(
    *,
    article_id: int,
    maturity: RevenueMaturity,
    linked_programs: list[dict],
    policy: RevenuePolicy,
) -> list[RevenueCandidate]:
    """プログラムはあるが tracking URL が無い -- 設定作業が残っている、とだけ言う。

    「この提携が承認済み」とは **DB がそう言っていない限り主張しない**。
    ここで使う事実は ``affiliate_programs.status`` と ``tracking_url`` の有無だけ。
    """

    if maturity.state != MONETIZATION_SETUP_INCOMPLETE:
        return []
    untracked = [
        p for p in linked_programs if p.get("status") == "active" and not p.get("tracking_url")
    ]
    if not untracked:
        return []

    primary = [p for p in untracked if p.get("is_primary")] or untracked
    return [
        RevenueCandidate(
            candidate_type=TRACKING_SETUP_REQUIRED,
            reason_code=REASON_PROGRAM_WITHOUT_TRACKING_URL,
            priority=resolve_priority(policy, TRACKING_SETUP_REQUIRED),
            evidence_basis=BASIS_STRUCTURAL,
            suggested_action=(
                f"カタログの「{primary[0]['name']}」に tracking URL が未設定。"
                "提携状況を確認し、取得できた場合のみ既存フローで登録する "
                "(公式リンクを勝手にアフィリエイトリンクへ置き換えない)"
            ),
            article_id=article_id,
            affiliate_program_id=primary[0]["id"],
            evidence={
                "program_id": primary[0]["id"],
                "program_name": primary[0]["name"],
                "program_status": primary[0].get("status"),
                "tracking_url_present": False,
                "untracked_program_ids": sorted(p["id"] for p in untracked),
            },
        )
    ]


# ==================== 行動に基づく判定 (十分な標本が要る) ======================
def evaluate_high_traffic_unmonetized(
    *,
    article_id: int,
    maturity: RevenueMaturity,
    sessions: int | None,
    linked_programs: list[dict],
    policy: RevenuePolicy,
) -> RevenueCandidate | None:
    """流入が十分にあるのに収益化の導線が無い記事だけ。"""

    if maturity.monetized or not maturity.traffic_data_available or sessions is None:
        return None
    if maturity.state == NOT_MONETIZABLE and not linked_programs:
        # 収益化を意図していない記事に、無理に導線を足す提案はしない。
        return None
    gate = int(policy.gate("traffic", "minimum_sessions_for_unmonetized_review", 300))
    if sessions < gate:
        return None

    return RevenueCandidate(
        candidate_type=HIGH_TRAFFIC_UNMONETIZED,
        reason_code=REASON_TRAFFIC_WITHOUT_MONETIZATION,
        priority=resolve_priority(policy, HIGH_TRAFFIC_UNMONETIZED, sessions=sessions),
        evidence_basis=BASIS_BEHAVIORAL,
        suggested_action=(
            "流入があるのに収益化の導線が無い。適合するプログラムがあるかを確認する "
            "(無ければ公式リンクのままで問題ない)"
        ),
        article_id=article_id,
        evidence={
            "sessions": sessions,
            "organic_sessions": maturity.organic_sessions,
            "gate_minimum_sessions": gate,
            "linked_program_ids": sorted(p["id"] for p in linked_programs),
        },
    )


def evaluate_zero_click(
    *,
    article_id: int,
    maturity: RevenueMaturity,
    sessions: int | None,
    windows_overlap: bool,
    policy: RevenuePolicy,
) -> RevenueCandidate | None:
    """十分な露出があり、導線が生きていて、それでも信頼できるクリックが 0 のときだけ。

    CTA が悪いと **断定しない** -- 見る価値がある、とだけ言う。
    """

    if not maturity.monetized or not maturity.traffic_data_available or sessions is None:
        return None
    if policy.require_overlapping_windows and not windows_overlap:
        return None
    gate = int(policy.gate("traffic", "minimum_sessions_for_zero_click_review", 200))
    if sessions < gate or maturity.clean_clicks > 0:
        return None

    return RevenueCandidate(
        candidate_type=ZERO_CLICK_REVIEW,
        reason_code=REASON_NO_CLEAN_CLICKS,
        priority=resolve_priority(policy, ZERO_CLICK_REVIEW),
        evidence_basis=BASIS_BEHAVIORAL,
        suggested_action=(
            "CTA の見え方・掲載位置・提案しているサービスの適合を確認する。"
            "CTA が原因だと断定はできない"
        ),
        article_id=article_id,
        evidence={
            "sessions": sessions,
            "organic_sessions": maturity.organic_sessions,
            "clean_clicks": 0,
            "excluded_clicks": maturity.excluded_clicks,
            "gate_minimum_sessions": gate,
        },
    )


def evaluate_click_through(
    *,
    article_id: int,
    maturity: RevenueMaturity,
    organic_sessions: int | None,
    windows_overlap: bool,
    policy: RevenuePolicy,
) -> RevenueCandidate | None:
    """クリック率を語れるのは、標本が十分で窓が重なっているときだけ。"""

    if maturity.state != MEASURABLE or organic_sessions is None or organic_sessions <= 0:
        return None
    if policy.require_overlapping_windows and not windows_overlap:
        return None

    per_100 = round(maturity.clean_clicks / organic_sessions * 100, 2)
    return RevenueCandidate(
        candidate_type=AFFILIATE_CLICK_THROUGH_REVIEW,
        reason_code=REASON_LOW_CLICK_THROUGH,
        priority=resolve_priority(policy, AFFILIATE_CLICK_THROUGH_REVIEW),
        evidence_basis=BASIS_BEHAVIORAL,
        suggested_action="オーガニック流入に対する送客の割合を、同種の記事と見比べる",
        article_id=article_id,
        evidence={
            # 分母は organic を使う (SEO からの導線を見たいため)。明示する。
            "denominator": "organic_sessions",
            "organic_sessions": organic_sessions,
            "clean_clicks": maturity.clean_clicks,
            "excluded_clicks": maturity.excluded_clicks,
            "clicks_per_100_organic_sessions": per_100,
        },
    )


# ==================== プログラム単位 / データ品質 ==============================
def evaluate_commission_signal(
    *,
    affiliate_program_id: int,
    program_name: str,
    attribution: str,
    conversions: int,
    commission_amount: str | None,
    currency: str | None,
    article_ids_with_clean_clicks: list[int],
    policy: RevenuePolicy,
) -> RevenueCandidate | None:
    """報酬は **プログラム単位のまま** 報告する。記事へは決して配分しない。"""

    if attribution == ATTRIBUTION_ARTICLE:
        return None
    return RevenueCandidate(
        candidate_type=COMMISSION_SIGNAL_REVIEW,
        reason_code=REASON_COMMISSION_PROGRAM_LEVEL_ONLY,
        priority=resolve_priority(policy, COMMISSION_SIGNAL_REVIEW),
        evidence_basis=BASIS_PROGRAM_LEVEL,
        suggested_action=(
            "報酬はプログラム単位でしか分からない。記事別の収益・EPC・転換率は"
            "算出しない。記事別に見たい場合は、provider 側で記事を識別できる "
            "パラメータが使えるかを確認する"
        ),
        affiliate_program_id=affiliate_program_id,
        evidence={
            "program_name": program_name,
            "attribution": attribution,
            "conversions": conversions,
            "commission_amount": commission_amount,
            "currency": currency,
            # クリックは記事単位で分かるが、報酬は結び付けられない -- 両方示す。
            "article_ids_with_clean_clicks": sorted(article_ids_with_clean_clicks),
            "article_level_revenue": None,
        },
    )


def evaluate_data_quality(
    *,
    baseline,
    ga4_data_through,
    windows_overlap: bool,
    attribution: str,
    policy: RevenuePolicy,
) -> list[RevenueCandidate]:
    """計測そのものの問題。読者の行動に関する主張ではない。"""

    out: list[RevenueCandidate] = []
    if baseline.unattributed_raw_clicks:
        out.append(
            RevenueCandidate(
                candidate_type=AFFILIATE_DATA_QUALITY_REVIEW,
                reason_code=REASON_UNATTRIBUTED_CLICK_TOKENS,
                priority=resolve_priority(policy, AFFILIATE_DATA_QUALITY_REVIEW),
                evidence_basis=BASIS_DATA_QUALITY,
                suggested_action="target を持たない token のクリックの出所を確認する",
                evidence={
                    "unattributed_raw_clicks": baseline.unattributed_raw_clicks,
                    "token_count": len(baseline.unattributed_tokens),
                },
            )
        )
    if baseline.excluded_clicks:
        out.append(
            RevenueCandidate(
                candidate_type=AFFILIATE_DATA_QUALITY_REVIEW,
                reason_code=REASON_CLICKS_BEFORE_TRUSTED_BASELINE,
                priority=resolve_priority(policy, AFFILIATE_DATA_QUALITY_REVIEW),
                evidence_basis=BASIS_DATA_QUALITY,
                suggested_action=(
                    "信頼できる計測開始時刻より前のクリックは行動評価から除外している。"
                    "行は監査のためそのまま保持する"
                ),
                evidence={
                    "raw_clicks": baseline.raw_clicks,
                    "excluded_clicks": baseline.excluded_clicks,
                    "clean_clicks": baseline.clean_clicks,
                    "trusted_measurement_start_at": (
                        baseline.trusted_measurement_start_at.isoformat()
                        if baseline.trusted_measurement_start_at
                        else None
                    ),
                },
            )
        )
    if not windows_overlap:
        out.append(
            RevenueCandidate(
                candidate_type=AFFILIATE_DATA_QUALITY_REVIEW,
                reason_code=REASON_WINDOWS_DO_NOT_OVERLAP,
                priority=resolve_priority(policy, AFFILIATE_DATA_QUALITY_REVIEW),
                evidence_basis=BASIS_DATA_QUALITY,
                suggested_action=(
                    "流入データとクリックデータの対象期間が重なっていないため、"
                    "比率を出さない。取り込みの期間を揃える"
                ),
                evidence={
                    "ga4_data_through": (
                        ga4_data_through.isoformat() if ga4_data_through else None
                    ),
                    "clean_click_data_through": (
                        baseline.clean_data_through.isoformat()
                        if baseline.clean_data_through
                        else None
                    ),
                },
            )
        )
    if attribution in (ATTRIBUTION_PROGRAM_ONLY, "unavailable"):
        out.append(
            RevenueCandidate(
                candidate_type=AFFILIATE_DATA_QUALITY_REVIEW,
                reason_code=REASON_COMMISSION_PROGRAM_LEVEL_ONLY,
                priority=resolve_priority(policy, AFFILIATE_DATA_QUALITY_REVIEW),
                evidence_basis=BASIS_DATA_QUALITY,
                suggested_action=(
                    "成果を記事へ結び付ける join key が provider データに無い。"
                    "記事別の収益指標は出さない"
                ),
                evidence={"attribution": attribution, "article_level_revenue": None},
            )
        )
    return out
