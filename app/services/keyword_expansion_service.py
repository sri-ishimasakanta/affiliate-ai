"""KeywordExpansionService — keyword idea 候補を read-only で expansion planner に掛ける。

**DB write 0 / HTTP 0 / LLM 0 / keyword insert 0。** production の keyword・article・active
affiliate catalog を読み (URL は読まない)、C2.2 の制作キューと合わせて純粋ロジック
(:mod:`app.article.keyword_expansion`) に渡すだけ。session が pending 変更を持つと flush 前に
例外で止まる (:func:`app.services.content_queue_service.read_only_session`)。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from sqlalchemy.orm import Session

from app.article.cluster_plan import AffiliateMatch, ClusterConfig, build_content_queue
from app.article.keyword_expansion import (
    ExpansionPlan,
    ExpansionRules,
    IdeaCandidate,
    plan_expansion,
)
from app.keyword.affiliate_matching import match_programs
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

        enriched = [
            replace(
                candidate,
                affiliate_matches=tuple(
                    AffiliateMatch(program_id=m.program_id, name=m.name, provider=m.provider)
                    for m in match_programs(
                        candidate.keyword, catalog, ignore_japanese_spacing=True
                    )
                ),
            )
            for candidate in candidates
        ]
        return plan_expansion(config, rules, enriched, keywords, articles, queue)
