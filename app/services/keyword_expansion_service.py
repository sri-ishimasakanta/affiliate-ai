"""KeywordExpansionService — keyword idea 候補を read-only で expansion planner に掛ける。

**DB write 0 / HTTP 0 / LLM 0 / keyword insert 0。** production の keyword・article・active
affiliate catalog を読み (URL は読まない)、C2.2 の制作キューと合わせて純粋ロジック
(:mod:`app.article.keyword_expansion`) に渡すだけ。session が pending 変更を持つと flush 前に
例外で止まる (:func:`app.services.content_queue_service.read_only_session`)。

affiliate coverage は Google Ads の分かち書き (``議事 録``) を吸収する照合 (spacing option) で
解決し、C2.5.4 で strong / weak の tier を報告に追加した (判定・順序・score は変えない)。scoring /
article planning / C2.2 queue は legacy の照合 (spacing option なし) のままなので、両者の差
(``japanese_spacing_only_coverage``) を summary に報告する。統一は後の phase。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from sqlalchemy.orm import Session

from app.article.cluster_plan import (
    ClusterConfig,
    affiliate_matches_from_tiered,
    build_content_queue,
)
from app.article.keyword_expansion import (
    ExpansionPlan,
    ExpansionRules,
    IdeaCandidate,
    plan_expansion,
)
from app.keyword.affiliate_matching import match_programs
from app.keyword.affiliate_tiers import match_programs_tiered
from app.services.content_queue_service import ContentQueueService


class KeywordExpansionService:
    def __init__(self, session: Session) -> None:
        self._queue_service = ContentQueueService(session)

    def plan(
        self,
        config: ClusterConfig,
        rules: ExpansionRules,
        candidates: Sequence[IdeaCandidate],
    ) -> ExpansionPlan:
        """候補を keep / merge / reject に振り分ける。未知の cluster keyword は
        ``ClusterConfigError``。"""

        keywords, articles = self._queue_service.load_inputs()
        catalog = self._queue_service.load_catalog()
        queue = build_content_queue(config, keywords, articles)

        enriched = []
        spacing_only: set[str] = set()
        for candidate in candidates:
            tiered = match_programs_tiered(candidate.keyword, catalog, ignore_japanese_spacing=True)
            enriched.append(
                replace(candidate, affiliate_matches=affiliate_matches_from_tiered(tiered))
            )
            # spacing option の有無で「legacy の covered」が変わる候補 (scoring 側には見えない)
            if any(t.legacy_matched for t in tiered) and not match_programs(
                candidate.keyword, catalog
            ):
                spacing_only.add(candidate.keyword)
        plan = plan_expansion(config, rules, enriched, keywords, articles, queue)
        summary = {
            **plan.summary,
            "japanese_spacing_only_coverage": len(spacing_only),
            "keep_japanese_spacing_only_coverage": sum(
                1 for d in plan.decisions if d.decision == "keep" and d.keyword in spacing_only
            ),
        }
        return replace(plan, summary=summary)
