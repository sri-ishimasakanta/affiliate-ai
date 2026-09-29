"""承認した Growth Action を、既存のどの流れへ渡せるか (C9 Batch 2 / C9-B、pure)。**実行しない。**

承認は「次の段階へ進めてよい」という許可だけ。ここは、その次の段階として **実在する** 入口を
示す (計画 = PLAN) だけで、依頼を作らない・書かない。自然な入口が無いものは
``unsupported`` と明示する (架空の自動化の経路を作らない)。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass

from app.growth import analysis as ga

SUPPORTED = "supported"  # 既存の入口がある (その入口も既定は PLAN・書くのは人の --execute だけ)
LANE = "handled_by_existing_lane"  # 既存の自動の流れが担う (変える必要がない)
MANUAL = "manual_only"  # 人が手で見る (自動の入口は無い)
UNSUPPORTED = "unsupported"  # このシステムに自然な入口が無い
NOT_APPLICABLE = "not_applicable"  # レビュー・実行の対象ではない (情報)

# -- 実行の形 (C9 Batch 3) -----------------------------------------------------------------------
#: 承認のあと ``--execute`` で、既存の流れの **手元の** 依頼を作ってよい (外には書かない)。
EXEC_LOCAL_HANDOFF = "local_handoff"
#: 計画を示すだけ (手元にも依頼を作らない。変換済みにもしない)。
EXEC_PLAN_ONLY = "plan_only"
#: 安全な入口が無い (実行すれば断る)。
EXEC_UNSUPPORTED = "unsupported"
#: 対象ではない。
EXEC_NOT_APPLICABLE = "not_applicable"
EXECUTION_MODES = (EXEC_LOCAL_HANDOFF, EXEC_PLAN_ONLY, EXEC_UNSUPPORTED, EXEC_NOT_APPLICABLE)
#: 対応の表 (報告と試験のため): 行動 → (実行の形, いま足りないもの)。
CONVERSION_MATRIX = {
    ga.REVIEW_INTERNAL_LINKS: (EXEC_LOCAL_HANDOFF, None),
    ga.CREATE_NEW_ARTICLE: (EXEC_LOCAL_HANDOFF, None),
    ga.CREATE_GROWTH_POST: (EXEC_PLAN_ONLY, "the Growth lane owns generation (daily call cap, "
                                            "purpose gate); nothing to hand off"),
    ga.CREATE_REGULAR_THREADS_POST: (EXEC_LOCAL_HANDOFF, None),
    ga.CREATE_THREADS_ALTERNATIVE_ANGLE: (EXEC_LOCAL_HANDOFF, None),
    ga.REVIEW_AFFILIATE_PLACEMENT: (EXEC_LOCAL_HANDOFF, None),
    ga.UPDATE_EXISTING_ARTICLE: (EXEC_LOCAL_HANDOFF, None),
    ga.IMPROVE_SEARCH_SNIPPET: (EXEC_LOCAL_HANDOFF, None),
    ga.WAIT_FOR_MORE_DATA: (EXEC_NOT_APPLICABLE, None),
    ga.INVESTIGATE_DATA_QUALITY: (EXEC_NOT_APPLICABLE, None),
}  # fmt: skip

# -- 渡す先 (C9-B) ---------------------------------------------------------------------------------
TARGET_CHANGE_REQUEST = "change_request"
TARGET_THREADS_GENERATION = "threads_generation_request"
TARGET_ARTICLE_PLANNING = "article_planning_request"
TARGET_CHANGE_PREPARATION = "change_preparation_request"
TARGETS = (TARGET_CHANGE_REQUEST, TARGET_THREADS_GENERATION, TARGET_ARTICLE_PLANNING,
           TARGET_CHANGE_PREPARATION)  # fmt: skip
#: 行動 → 渡す先 (手元の依頼の種類)。無い行動は渡さない。
HANDOFF_TARGETS = {
    ga.REVIEW_INTERNAL_LINKS: TARGET_CHANGE_REQUEST,
    ga.CREATE_REGULAR_THREADS_POST: TARGET_THREADS_GENERATION,
    ga.CREATE_THREADS_ALTERNATIVE_ANGLE: TARGET_THREADS_GENERATION,
    ga.CREATE_NEW_ARTICLE: TARGET_ARTICLE_PLANNING,
    ga.UPDATE_EXISTING_ARTICLE: TARGET_CHANGE_PREPARATION,
    ga.IMPROVE_SEARCH_SNIPPET: TARGET_CHANGE_PREPARATION,
    ga.REVIEW_AFFILIATE_PLACEMENT: TARGET_CHANGE_PREPARATION,
}
#: 変更の準備の種類 (ChangeRequest V2)。
CHANGE_TYPE_FOR_ACTION = {
    ga.UPDATE_EXISTING_ARTICLE: "body_update",
    ga.IMPROVE_SEARCH_SNIPPET: "meta_description",
    ga.REVIEW_AFFILIATE_PLACEMENT: "affiliate_placement",
}
#: 変換の先ごとの支え (``supported`` / ``plan_only`` / ``unsupported``)。
TARGET_SUPPORT = {
    TARGET_CHANGE_REQUEST: "supported", TARGET_THREADS_GENERATION: "supported",
    TARGET_ARTICLE_PLANNING: "supported", TARGET_CHANGE_PREPARATION: "supported",
}  # fmt: skip


@dataclass(frozen=True)
class ConversionPlan:
    action_type: str
    support: str
    target_workflow: str | None
    entry_point: str | None
    steps: tuple[str, ...]
    note: str
    #: この変換そのものが外に書くか (どの入口も既定は PLAN で、書かない)。
    writes_on_plan: bool = False
    #: ``--execute`` で何をしてよいか (``EXEC_*``)。
    execution_mode: str = EXEC_NOT_APPLICABLE
    #: 実行できないときの、いま足りないもの。
    missing: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def plan_conversion(candidate: Mapping) -> ConversionPlan:
    plan = _plan_conversion(candidate)
    mode, missing = CONVERSION_MATRIX.get(plan.action_type, (EXEC_NOT_APPLICABLE, None))
    return ConversionPlan(**{**asdict(plan), "execution_mode": mode, "missing": missing})


def _plan_conversion(candidate: Mapping) -> ConversionPlan:
    action = candidate["action_type"]
    aid = candidate.get("article_id")
    kid = candidate.get("keyword_id")
    if action == ga.REVIEW_INTERNAL_LINKS:
        return ConversionPlan(
            action, SUPPORTED, "change request (C9.1 add_internal_link)",
            "scripts/propose_change.py --candidate-id <SEO candidate id>",
            ("persist the C6 run: scripts/report_seo_improvement_candidates.py (its --execute "
             "writes only seo_improvement_* history)",
             f"pick the INTERNAL_LINK_OPPORTUNITY candidate for article {aid}",
             "scripts/propose_change.py --candidate-id <id> (PLAN; --execute writes one "
             "change_requests row, awaiting approval)",
             "the change request has its own human approval and apply step"),
            "the change request flow owns the WordPress write; this approval does not")
    if action == ga.CREATE_NEW_ARTICLE:
        return ConversionPlan(
            action, SUPPORTED, TARGET_ARTICLE_PLANNING,
            "scripts/convert_growth_action.py <id> --execute (one article planning request)",
            ("--execute writes one growth_handoff_requests row (article_planning, pending); "
             "no article is created",
             "a human approves or rejects the planning request "
             "(scripts/manage_growth_actions.py handoff approve-plan / reject-plan)",
             f"the existing plan flow creates the article: scripts/export_article_plan.py "
             f"--keyword-id {kid} (read-only) then POST /api/v1/keywords/{kid}/article-plan/"
             f"approve" if kid else "the existing plan flow creates the article",
             "link the created article: handoff materialize <request> --article-id <id>",
             "drafting, review and publication stay in the existing article workflow"),
            "the planning request is local only; the article plan approval, drafting and "
            "publication are separate human steps")
    if action == ga.CREATE_GROWTH_POST:
        return ConversionPlan(
            action, LANE, "Growth lane (account_growth_maintenance)", None,
            ("the worker's Growth lane generates the day's post (awaiting human approval)",),
            "no conversion: the lane already does this")
    if action in (ga.CREATE_REGULAR_THREADS_POST, ga.CREATE_THREADS_ALTERNATIVE_ANGLE):
        angle = (candidate.get("recommendation") or {}).get("angle")
        lane = ("alternative angle " + str(angle)
                if action == ga.CREATE_THREADS_ALTERNATIVE_ANGLE else "regular")
        return ConversionPlan(
            action, SUPPORTED, TARGET_THREADS_GENERATION,
            "scripts/convert_growth_action.py <id> --execute (one targeted generation request)",
            (f"--execute writes one growth_handoff_requests row (threads_generation, {lane}, "
             f"article {aid}); no OpenAI call, no Threads write",
             "the existing proposal stock maintenance uses it only when it would generate "
             "anyway (floor / caps / cooldown / pending-request rules unchanged), and only while "
             "growth_action_policy.json threads_generation_requests.consume_in_stock_maintenance "
             "is true",
             "the proposal goes through the existing Threads approval and publication flow"),
            "Growth Action converted ≠ Threads proposal approved ≠ published")
    if action in CHANGE_TYPE_FOR_ACTION:
        change_type = CHANGE_TYPE_FOR_ACTION[action]
        downstream = {"body_update": "a change request or an editorial revision",
                      "meta_description": "an editorial revision",
                      "affiliate_placement": "a link mapping (manage_article_link_mapping.py) "
                                             "or a change request"}[change_type]
        return ConversionPlan(
            action, SUPPORTED, TARGET_CHANGE_PREPARATION,
            "scripts/convert_growth_action.py <id> --execute --expected-source-hash <hash>",
            (f"--execute writes one growth_handoff_requests row (change_preparation, "
             f"{change_type}) with the article's frozen source hashes; no content is "
             "generated, no URL is guessed, nothing is written to WordPress",
             f"a human prepares the concrete change as {downstream} in the existing flow",
             "link it: handoff prepare <request> --downstream-type <type> --downstream-id <id> "
             "(refused when it was not made from the frozen source)",
             "the downstream keeps its own approval and apply step"),
            "Growth Action converted ≠ change approved ≠ applied")
    return ConversionPlan(action, NOT_APPLICABLE, None, None, (),
                          "informational: not a review or execution target")


__all__ = ["CHANGE_TYPE_FOR_ACTION", "CONVERSION_MATRIX", "EXECUTION_MODES",
           "EXEC_LOCAL_HANDOFF", "EXEC_NOT_APPLICABLE", "EXEC_PLAN_ONLY", "EXEC_UNSUPPORTED",
           "HANDOFF_TARGETS", "LANE", "MANUAL", "NOT_APPLICABLE", "SUPPORTED", "TARGETS",
           "TARGET_ARTICLE_PLANNING", "TARGET_CHANGE_PREPARATION", "TARGET_CHANGE_REQUEST",
           "TARGET_SUPPORT", "TARGET_THREADS_GENERATION", "UNSUPPORTED", "ConversionPlan",
           "plan_conversion"]
