"""Site Growth Orchestrator (C10-3 / C10-E、``site-growth/1``、pure)。

**既存の流れを置き換えない。** 行動ごとに「今どの安全な流れへ渡せるか」と、その流れの段
(証拠 → 機会 → Growth Action → 人のレビュー → 安全な引き渡し → 先の承認 → 実行 → 観測 →
新しい証拠) の準備度を並べる。承認はそれぞれの流れのまま (Growth の承認は先の承認ではない)。

``ACTION_MATRIX`` はソースで確かめた本番の状態 (``production``):
``enabled`` (本番で端から端まで動く) / ``human_driven`` (人の手の段がある) /
``pending_activation`` (実装済み・本番の最初の実行は人の判断) / ``plan_only`` / ``deferred`` /
``informational``。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass

from app.growth import analysis as ga

SCHEMA = "site-growth/1"
ENABLED = "enabled"
HUMAN_DRIVEN = "human_driven"
PENDING_ACTIVATION = "pending_activation"
PLAN_ONLY = "plan_only"
DEFERRED = "deferred"
INFORMATIONAL = "informational"


@dataclass(frozen=True)
class ActionPath:
    action_type: str
    handoff: str | None
    downstream_approval: str | None
    execution: str | None
    effective_at: str | None
    measurement: str | None
    production: str
    note: str

    def as_dict(self) -> dict:
        return asdict(self)


ACTION_MATRIX: Mapping[str, ActionPath] = {
    ga.REVIEW_INTERNAL_LINKS: ActionPath(
        ga.REVIEW_INTERNAL_LINKS, "change_request (add_internal_link, awaiting_approval)",
        "manage_change_requests.py approve --proposal-hash",
        "apply_approved_change.py --execute (managed WordPress content update)",
        "change_applications.finished_at", "C9-C GSC/GA4/clicks windows", ENABLED,
        "end to end in production (human approval + human apply)"),
    ga.CREATE_NEW_ARTICLE: ActionPath(
        ga.CREATE_NEW_ARTICLE, "article_planning request (C9-B)",
        "handoff approve-plan, then the existing article plan approval (REST)",
        "existing drafting / review / publication workflow",
        "article.published_at", "C9-C after-only windows", HUMAN_DRIVEN,
        "never creates an Article automatically; cannibalization rechecked at conversion"),
    ga.CREATE_REGULAR_THREADS_POST: ActionPath(
        ga.CREATE_REGULAR_THREADS_POST, "targeted threads_generation request (C9-B)",
        "existing Threads proposal approval",
        "existing stock maintenance (consume_in_stock_maintenance=true) + publication",
        "threads_publication.published_at", "T6.5 24h / 72h", ENABLED,
        "consumed only when the stock would generate anyway; no direct model call"),
    ga.CREATE_THREADS_ALTERNATIVE_ANGLE: ActionPath(
        ga.CREATE_THREADS_ALTERNATIVE_ANGLE, "targeted threads_generation request (frozen angle)",
        "existing Threads proposal approval",
        "existing stock maintenance + publication", "threads_publication.published_at",
        "T6.5 24h / 72h", ENABLED, "same as regular, with the angle frozen at conversion"),
    ga.UPDATE_EXISTING_ARTICLE: ActionPath(
        ga.UPDATE_EXISTING_ARTICLE, "change_preparation (body_update)",
        "text_edit change request approval (C10-3, human-written body)",
        "ChangeApplication text_edit gates → existing managed content update",
        "change_applications.finished_at", "C9-C windows", PENDING_ACTIVATION,
        "text_edit_apply_enabled=false until the first production apply is approved"),
    ga.IMPROVE_SEARCH_SNIPPET: ActionPath(
        ga.IMPROVE_SEARCH_SNIPPET, "change_preparation (meta_description)",
        "meta_description change request approval (C10-3, human-written meta)",
        "MetaDescriptionApplyService → update_post_excerpt_exact (new write form)",
        "change_applications.finished_at", "C9-C windows (CTR)", PENDING_ACTIVATION,
        "meta_description_apply_enabled=false until the first production write is approved"),
    ga.REVIEW_AFFILIATE_PLACEMENT: ActionPath(
        ga.REVIEW_AFFILIATE_PLACEMENT,
        "change_preparation (affiliate_placement)",
        "link mapping (manual, existing) or a change request",
        None, None, "clicks (article-level); commissions provider-level only", DEFERRED,
        "automated placement apply waits for C11 attribution (no tracking URL / SubID change)"),
    ga.CREATE_GROWTH_POST: ActionPath(
        ga.CREATE_GROWTH_POST, None, None, "Growth lane (daily model-call cap, purpose gate)",
        "threads_publication.published_at", "T6.5", PLAN_ONLY,
        "the Growth lane owns generation; nothing to hand off"),
    ga.WAIT_FOR_MORE_DATA: ActionPath(ga.WAIT_FOR_MORE_DATA, None, None, None, None, None,
                                      INFORMATIONAL, "informational"),
    ga.INVESTIGATE_DATA_QUALITY: ActionPath(ga.INVESTIGATE_DATA_QUALITY, None, None, None, None,
                                            None, INFORMATIONAL, "informational"),
}

# -- 1 つの Growth Action の今の段 -----------------------------------------------------------------
STAGE_OBSERVED = "observed"
STAGE_AWAITING_REVIEW = "awaiting_review"
STAGE_REVIEW_PENDING = "review_pending"
STAGE_APPROVED = "approved_not_converted"
STAGE_HANDED_OFF = "handed_off"
STAGE_DOWNSTREAM = "downstream_in_progress"
STAGE_EFFECTIVE = "effective"
STAGE_CLOSED = "closed"


def stage_of(*, candidate_status: str, review_status: str | None, converted: bool,
             effective_at: str | None, downstream_open: bool) -> str:
    """今の段 (決定論的)。"""

    if candidate_status in ("rejected", "dismissed", "superseded"):
        return STAGE_CLOSED
    if effective_at:
        return STAGE_EFFECTIVE
    if converted:
        return STAGE_DOWNSTREAM if downstream_open else STAGE_HANDED_OFF
    if review_status == "approved" or candidate_status == "approved":
        return STAGE_APPROVED
    if review_status == "pending":
        return STAGE_REVIEW_PENDING
    if candidate_status == "active":
        return STAGE_AWAITING_REVIEW
    return STAGE_OBSERVED


def next_step(action_type: str, stage: str) -> str:
    path = ACTION_MATRIX.get(action_type)
    if path is None or path.production == INFORMATIONAL:
        return "informational: nothing to do"
    if stage == STAGE_AWAITING_REVIEW:
        return "a human reviews it (manage_growth_actions.py review <id> --execute)"
    if stage == STAGE_REVIEW_PENDING:
        return "a human approves or rejects the review"
    if stage == STAGE_APPROVED:
        if path.production == PLAN_ONLY:
            return "plan only: " + path.note
        return "convert (convert_growth_action.py <id> --execute) → " + (path.handoff or "")
    if stage in (STAGE_HANDED_OFF, STAGE_DOWNSTREAM):
        if path.production == PENDING_ACTIVATION:
            return f"downstream: {path.downstream_approval}; apply is pending activation"
        return f"downstream: {path.downstream_approval or '—'} → {path.execution or '—'}"
    if stage == STAGE_EFFECTIVE:
        return f"measuring: {path.measurement or '—'}"
    return "—"


__all__ = ["ACTION_MATRIX", "ActionPath", "DEFERRED", "ENABLED", "HUMAN_DRIVEN",
           "INFORMATIONAL", "PENDING_ACTIVATION", "PLAN_ONLY", "SCHEMA", "STAGE_APPROVED",
           "STAGE_AWAITING_REVIEW", "STAGE_CLOSED", "STAGE_DOWNSTREAM", "STAGE_EFFECTIVE",
           "STAGE_HANDED_OFF", "STAGE_OBSERVED", "STAGE_REVIEW_PENDING", "next_step",
           "stage_of"]
