"""Article の Human publication approval (transaction owner)。

このフェーズは **review -> approved のみ**。WordPress へは一切通信しない
(post の公開・更新・作成は行わない)。

Human が WordPress draft を目視レビューした後、対応する WordPressDraftRun が
succeeded かつ draft のままであること・prepare 後に Article 本文/meta が
drift していないこと・呼び出し側が承認した target_request_identity_hash が
現在の run と一致することを全て確認してから、宣言的な
:data:`app.services.status_transitions.ARTICLE_TRANSITIONS` に従って
review -> approved のみを許可する。汎用の任意 status 変更はしない
(それは既存の ``ArticleService.change_status`` の責務)。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.article.draft_promotion_canonical import compute_text_hash
from app.article.schemas import ArticleRead
from app.exceptions import ArticlePublicationApprovalError, EntityNotFoundError
from app.models.enums import ArticleStatus
from app.models.wordpress_draft_run import WP_RUN_SUCCEEDED
from app.repositories.article_repository import ArticleRepository
from app.repositories.wordpress_draft_run_repository import (
    WordPressDraftRunRepository,
)
from app.services.article_service import ArticleService
from app.services.status_transitions import ARTICLE_TRANSITIONS, ensure_transition_allowed

_ARTICLE = "Article"


class ArticlePublicationApprovalService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._articles = ArticleRepository(session)
        self._runs = WordPressDraftRunRepository(session)

    def approve(
        self,
        article_id: int,
        *,
        expected_wordpress_post_id: int,
        expected_target_request_identity_hash: str,
    ) -> ArticleRead:
        article = self._articles.get_by_id(article_id)
        if article is None:
            raise EntityNotFoundError(_ARTICLE, article_id)

        run = self._matching_succeeded_run(article)

        self._assert_gates(
            article=article,
            run=run,
            expected_wordpress_post_id=expected_wordpress_post_id,
            expected_target_request_identity_hash=expected_target_request_identity_hash,
        )

        current = ArticleStatus(article.status)
        target = ArticleStatus.APPROVED
        ensure_transition_allowed(_ARTICLE, current, target, ARTICLE_TRANSITIONS)

        if target != current:
            self._articles.update(article, {"status": target})
            self._session.commit()

        return ArticleService._to_read(article)

    # -- internals ----------------------------------------------------------
    def _matching_succeeded_run(self, article):
        if article.wordpress_post_id is None:
            return None
        wp_id = str(article.wordpress_post_id)
        for run in self._runs.list_by_article(article.id):
            if run.wordpress_post_id == wp_id and run.status == WP_RUN_SUCCEEDED:
                return run
        return None

    @staticmethod
    def _assert_gates(
        *,
        article,
        run,
        expected_wordpress_post_id: int,
        expected_target_request_identity_hash: str,
    ) -> None:
        fails: list[str] = []

        if str(article.status) != "review":
            fails.append(f"Article.status={article.status!r} (expected review)")
        if article.wordpress_post_id is None:
            fails.append("Article.wordpress_post_id is null")
        elif article.wordpress_post_id != expected_wordpress_post_id:
            fails.append(
                f"Article.wordpress_post_id={article.wordpress_post_id!r} != "
                f"expected_wordpress_post_id={expected_wordpress_post_id!r}"
            )
        if article.published_url is not None:
            fails.append("Article.published_url is not null")
        if article.published_at is not None:
            fails.append("Article.published_at is not null")

        if run is None:
            fails.append(
                "no succeeded WordPressDraftRun found matching Article.wordpress_post_id"
            )
        else:
            if run.article_id != article.id:
                fails.append("matched run.article_id does not match Article.id")
            if run.status != WP_RUN_SUCCEEDED:
                fails.append(f"run.status={run.status!r} (expected succeeded)")
            if run.wordpress_post_status != "draft":
                fails.append(
                    f"run.wordpress_post_status={run.wordpress_post_status!r} "
                    "(expected draft)"
                )
            if run.error_message is not None:
                fails.append("run.error_message is not null")
            if run.target_request_identity_hash != expected_target_request_identity_hash:
                fails.append(
                    "target_request_identity_hash does not match the caller-approved value"
                )
            body_hash = compute_text_hash(article.body or "")
            if body_hash != run.canonical_body_hash:
                fails.append(
                    "current Article body hash has drifted from the approved run"
                )
            meta_hash = compute_text_hash(article.meta_description or "")
            if meta_hash != run.canonical_meta_hash:
                fails.append(
                    "current Article meta hash has drifted from the approved run"
                )

        if fails:
            raise ArticlePublicationApprovalError("; ".join(fails))
