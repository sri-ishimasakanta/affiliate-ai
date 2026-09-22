"""KeywordExpansionService — keyword idea 候補を read-only で expansion planner に掛ける。

**DB write 0 / HTTP 0 / LLM 0 / keyword insert 0。** production の keyword・article・active
affiliate catalog を読み (URL は読まない)、C2.2 の制作キューと合わせて純粋ロジック
(:mod:`app.article.keyword_expansion`) に渡すだけ。session が pending 変更を持つと flush 前に
例外で止まる (:func:`app.services.content_queue_service.read_only_session`)。

affiliate coverage は scoring / article planning / C2.2 queue と **同じ** tier 付き照合
(:func:`app.keyword.affiliate_tiers.match_catalog`: Google Ads の分かち書き ``議事 録`` を吸収、
strong + weak) で解決する。C2.5.6 で planner と scoring の照合の食い違いは無くなった。判定 (keep /
merge / reject) は affiliate coverage に依存しない。
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
from app.keyword.affiliate_tiers import match_catalog
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
                affiliate_matches=affiliate_matches_from_tiered(
                    match_catalog(candidate.keyword, catalog)
                ),
            )
            for candidate in candidates
        ]
        return plan_expansion(config, rules, enriched, keywords, articles, queue)
