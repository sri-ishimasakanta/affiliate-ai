"""承認した Growth Action を、既存のどの流れへ渡せるか (C9 Batch 2、pure)。**実行しない。**

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

    def as_dict(self) -> dict:
        return asdict(self)


def plan_conversion(candidate: Mapping) -> ConversionPlan:
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
            action, SUPPORTED, "keyword → article plan",
            f"scripts/export_article_plan.py --keyword-id {kid}" if kid else
            "scripts/export_article_plan.py --keyword <query>",
            ("export the read-only article plan for the keyword",
             "drafting, review and publication stay in the existing article workflow"),
            "the article plan export is read-only; publishing is a separate human step")
    if action == ga.CREATE_GROWTH_POST:
        return ConversionPlan(
            action, LANE, "Growth lane (account_growth_maintenance)", None,
            ("the worker's Growth lane generates the day's post (awaiting human approval)",),
            "no conversion: the lane already does this")
    if action in (ga.CREATE_REGULAR_THREADS_POST, ga.CREATE_THREADS_ALTERNATIVE_ANGLE):
        return ConversionPlan(
            action, UNSUPPORTED, "Threads proposal stock maintenance", None,
            ("the stock maintenance chooses articles and angles itself; it has no targeted "
             "request for a given article or angle",),
            "unsupported conversion: a targeted Threads proposal request does not exist yet")
    if action == ga.REVIEW_AFFILIATE_PLACEMENT:
        return ConversionPlan(
            action, MANUAL, "monetization review", "scripts/manage_article_link_mapping.py",
            ("review the article's affiliate placement by hand",
             "if a tracked target is missing, bind it with manage_article_link_mapping "
             "(PLAN by default)"),
            "no automated placement change; affiliate destinations are never changed here")
    if action in (ga.UPDATE_EXISTING_ARTICLE, ga.IMPROVE_SEARCH_SNIPPET):
        return ConversionPlan(
            action, UNSUPPORTED, "article content/meta change", None,
            ("change requests generate only add_internal_link in v1; text and meta edits have "
             "no generator",),
            "unsupported conversion: edit by hand through the editorial workflow")
    return ConversionPlan(action, NOT_APPLICABLE, None, None, (),
                          "informational: not a review or execution target")


__all__ = ["LANE", "MANUAL", "NOT_APPLICABLE", "SUPPORTED", "UNSUPPORTED", "ConversionPlan",
           "plan_conversion"]
