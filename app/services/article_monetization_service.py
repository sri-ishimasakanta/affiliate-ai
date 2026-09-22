"""Article の content monetization mode (C2.5.8) の読み取りと **明示変更**。

mode は編集上の意図であって link の状態ではない。したがって:

- 読み取りは :func:`app.article.monetization.resolve_effective_mode`。
  ``articles.monetization_mode`` に明示値があればそれ、NULL の legacy 行だけ primary link から
  導出する (``legacy_derived``)。
- 変更は :meth:`ArticleMonetizationService.set_mode` **だけ**。affiliate link を足す / 外す /
  primary を切り替えるといった link 操作では mode は変わらない。
  ``affiliate`` の article から primary を外しても ``supporting`` にはならず、
  readiness / freeze gate が「primary が無い」と報告する。

遷移の条件 (C2.5.7 / C2.5.8 の規則をそのまま使う):

``published`` の article
    mode を変更できない (同じ値の明示・legacy NULL の昇格も含めて一切拒否)。公開済みの記事の
    収益化方針を変えることは revision の判断であり、その workflow は後続 phase の責務。
    拒否時は DB へ一切書かない (published の article #1 は NULL のまま)。
``* -> affiliate``
    primary の link がちょうど 1 件あり、それが現在の catalog で ``primary_eligible``
    (strong、または weak かつ core) であること。
``* -> supporting``
    primary の link が 1 件も残っていないこと (supporting に primary は無い)。

1 transaction で検証 → 書き込み → commit。失敗は full rollback (partial state を作らない)。
DB write はこの service の ``set_mode`` のみ。LLM / 外部 API は呼ばない。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.article import monetization
from app.article.schemas import (
    ArticleMonetizationModeRead,
    ArticleMonetizationModeUpdate,
)
from app.exceptions import EntityNotFoundError, MonetizationModeTransitionError
from app.models import Article, ArticleAffiliateProgram
from app.models.enums import ArticleStatus
from app.repositories.article_affiliate_program_repository import (
    ArticleAffiliateProgramRepository,
)
from app.repositories.article_repository import ArticleRepository

_ARTICLE = "Article"


class ArticleMonetizationService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._articles = ArticleRepository(session)
        self._links = ArticleAffiliateProgramRepository(session)

    # -- read ---------------------------------------------------------
    def get_mode(self, article_id: int) -> ArticleMonetizationModeRead:
        article = self._require_article(article_id)
        return self._read(article, self._links.list_by_article(article_id))

    # -- write (transaction owner) ------------------------------------
    def set_mode(
        self, article_id: int, payload: ArticleMonetizationModeUpdate
    ) -> ArticleMonetizationModeRead:
        article = self._require_article(article_id)
        links = self._links.list_by_article(article_id)
        target = monetization.validated_mode(payload.monetization_mode)

        # 検証は書き込み前に全て済ませる (同じ mode を明示し直す場合も条件を満たす必要がある)。
        self._assert_not_published(article)
        self._assert_transition_allowed(article, links, target)

        if article.monetization_mode == target:
            # 既に同じ明示値: 書き込まない (idempotent)。
            return self._read(article, links)

        try:
            self._articles.update(article, {"monetization_mode": target})
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        self._session.refresh(article)
        return self._read(article, self._links.list_by_article(article_id))

    # -- rules --------------------------------------------------------
    @staticmethod
    def _assert_not_published(article: Article) -> None:
        """published の article は mode を変更できない (書き込みは一切行わない)。"""

        if ArticleStatus(article.status) is not ArticleStatus.PUBLISHED:
            return
        raise MonetizationModeTransitionError(
            f"Article {article.id} is published; its content monetization mode is frozen. "
            "Changing the monetization approach of a live article is a revision decision and "
            "needs the publication/revision workflow (not implemented yet). Nothing was written"
        )

    def _assert_transition_allowed(
        self, article: Article, links: list[ArticleAffiliateProgram], target: str
    ) -> None:
        primary_links = [link for link in links if link.is_primary]
        if target == monetization.MODE_SUPPORTING:
            if primary_links:
                raise MonetizationModeTransitionError(
                    f"Article {article.id} still has a primary affiliate "
                    f"(affiliate_program_id {primary_links[0].affiliate_program_id}); "
                    "supporting content has no affiliate primary. Remove the primary link "
                    "first, then change the mode"
                )
            return

        if len(primary_links) != 1:
            raise MonetizationModeTransitionError(
                f"Article {article.id} has {len(primary_links)} primary affiliate links; "
                "monetization_mode=affiliate requires exactly one primary. Link a "
                "primary-eligible program as the primary first, then change the mode"
            )
        self._assert_primary_eligible(article, primary_links[0].affiliate_program_id)

    def _assert_primary_eligible(self, article: Article, program_id: int) -> None:
        """primary が現在の catalog で primary_eligible (strong / weak+core) か (C2.5.7)。"""

        # 循環 import を避ける (article_plan_service -> repositories のみ)
        from app.services.article_plan_service import ArticlePlanService

        if article.keyword_id is None:
            raise MonetizationModeTransitionError(
                f"Article {article.id} has no keyword; the primary affiliate's eligibility "
                "(strong, or weak with core fit) cannot be verified"
            )
        plan = ArticlePlanService(self._session).plan_for_keyword(article.keyword_id)
        chosen = next(
            (c for c in plan.affiliate_candidates if c.program_id == program_id), None
        )
        if chosen is None:
            raise MonetizationModeTransitionError(
                f"primary affiliate_program_id {program_id} is not an active matched "
                f"candidate for keyword {plan.keyword!r}"
            )
        if not chosen.primary_eligible:
            raise MonetizationModeTransitionError(
                f"primary affiliate_program_id {program_id} ({chosen.name}) is only a weak "
                f"({chosen.fit or 'unreviewed'} fit) match for keyword {plan.keyword!r}; a "
                "primary affiliate must be a strong match or a weak match whose fit is core"
            )

    # -- helpers ------------------------------------------------------
    def _require_article(self, article_id: int) -> Article:
        article = self._articles.get_by_id(article_id)
        if article is None:
            raise EntityNotFoundError(_ARTICLE, article_id)
        return article

    @staticmethod
    def _read(
        article: Article, links: list[ArticleAffiliateProgram]
    ) -> ArticleMonetizationModeRead:
        effective = monetization.resolve_effective_mode(article.monetization_mode, links)
        primary_links = [link for link in links if link.is_primary]
        violations: list[str] = []
        if effective.is_affiliate:
            if not links:
                violations.append("no_comparison_links")
            if len(primary_links) != 1:
                violations.append("primary_not_exactly_one")
        elif primary_links:
            violations.append("supporting_with_primary")
        return ArticleMonetizationModeRead(
            article_id=article.id,
            monetization_mode=effective.stored,
            effective_monetization_mode=effective.mode,
            monetization_mode_source=effective.source,
            primary_affiliate_program_id=(
                primary_links[0].affiliate_program_id if len(primary_links) == 1 else None
            ),
            affiliate_program_link_count=len(links),
            mode_requirement_violations=violations,
        )
