"""ArticleLinkSubstitutionMapping の永続化アクセス。

``commit`` は行わず ``flush`` のみ。汎用 ``update`` / ``delete`` は持たない。作成後の
変更は狭い lifecycle 遷移メソッド (``mark_superseded_for_remap`` /
``link_superseded_by`` / ``revoke``) のみ。前者 2 つは
:class:`~app.services.article_link_substitution_service.ArticleLinkSubstitutionService`
の ``remap_occurrence`` が 1 トランザクション内で組み合わせて使う (D-D1.2)。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.exceptions import ArticleLinkSubstitutionMappingError
from app.models import ArticleLinkSubstitutionMapping
from app.models.article_link_substitution_mapping import (
    ALSM_ACTIVE,
    ALSM_REVOKED,
    ALSM_SUPERSEDED,
    alsm_transition_allowed,
)


class ArticleLinkSubstitutionMappingRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_active(self, **fields) -> ArticleLinkSubstitutionMapping:
        entity = ArticleLinkSubstitutionMapping(status=ALSM_ACTIVE, **fields)
        self._session.add(entity)
        self._session.flush()
        return entity

    def get_by_id(self, mapping_id: int) -> ArticleLinkSubstitutionMapping | None:
        return self._session.get(ArticleLinkSubstitutionMapping, mapping_id)

    def get_active_for_occurrence(
        self, article_id: int, occurrence_identity_hash: str
    ) -> ArticleLinkSubstitutionMapping | None:
        return self._session.scalars(
            select(ArticleLinkSubstitutionMapping).where(
                ArticleLinkSubstitutionMapping.article_id == article_id,
                ArticleLinkSubstitutionMapping.occurrence_identity_hash
                == occurrence_identity_hash,
                ArticleLinkSubstitutionMapping.status == ALSM_ACTIVE,
            )
        ).first()

    def list_active_for_article(
        self, article_id: int
    ) -> list[ArticleLinkSubstitutionMapping]:
        return list(
            self._session.scalars(
                select(ArticleLinkSubstitutionMapping)
                .where(
                    ArticleLinkSubstitutionMapping.article_id == article_id,
                    ArticleLinkSubstitutionMapping.status == ALSM_ACTIVE,
                )
                .order_by(ArticleLinkSubstitutionMapping.id)
            ).all()
        )

    def get_by_idempotency_key(
        self, key: str
    ) -> ArticleLinkSubstitutionMapping | None:
        return self._session.scalars(
            select(ArticleLinkSubstitutionMapping).where(
                ArticleLinkSubstitutionMapping.idempotency_key == key
            )
        ).first()

    # -- narrow atomic-remap primitives (D-D1.2) -----------------------
    # ``supersede(entity, superseded_by_id=<preexisting id>)`` は D-D1.2 で削除した
    # — 呼び出し側が任意の無関係な既存 mapping (別 occurrence) を replacement として
    # 指せてしまう危険な API だったため。代わりに
    # :class:`~app.services.article_link_substitution_service.ArticleLinkSubstitutionService`
    # の ``remap_occurrence`` が、この 2 つの narrow primitive を 1 トランザクション
    # 内で組み合わせて使う。
    def mark_superseded_for_remap(
        self, entity: ArticleLinkSubstitutionMapping
    ) -> ArticleLinkSubstitutionMapping:
        """entity を active -> superseded に遷移させるだけ (superseded_by_id は
        まだ設定しない)。partial unique active index を解放し、同一 occurrence の
        新しい active row を同一トランザクション内で作れるようにするための、remap
        専用の中間状態。呼び出し側 (service) がこの後 :meth:`link_superseded_by`
        を呼ぶまでは、``superseded_by_id`` は NULL のままで良い — 失敗時に
        rollback すれば元の active 状態に戻る。"""

        self._require_transition(entity, ALSM_SUPERSEDED)
        entity.status = ALSM_SUPERSEDED
        self._session.flush()
        return entity

    def link_superseded_by(
        self, entity: ArticleLinkSubstitutionMapping, *, new_id: int
    ) -> ArticleLinkSubstitutionMapping:
        """既に superseded 済みの entity に ``superseded_by_id`` をちょうど 1 回だけ
        設定する。"""

        if entity.status != ALSM_SUPERSEDED:
            raise ArticleLinkSubstitutionMappingError(
                f"mapping {entity.id} must be superseded before linking "
                "superseded_by_id"
            )
        if entity.superseded_by_id is not None:
            raise ArticleLinkSubstitutionMappingError(
                f"mapping {entity.id} already has superseded_by_id set"
            )
        if new_id == entity.id:
            raise ArticleLinkSubstitutionMappingError(
                "a mapping cannot supersede itself"
            )
        entity.superseded_by_id = new_id
        self._session.flush()
        return entity

    def revoke(
        self, entity: ArticleLinkSubstitutionMapping
    ) -> ArticleLinkSubstitutionMapping:
        self._require_transition(entity, ALSM_REVOKED)
        entity.status = ALSM_REVOKED
        # superseded_by_id は revoked では絶対に設定しない (§6)。
        self._session.flush()
        return entity

    @staticmethod
    def _require_transition(
        entity: ArticleLinkSubstitutionMapping, target: str
    ) -> None:
        if not alsm_transition_allowed(entity.status, target):
            raise ArticleLinkSubstitutionMappingError(
                f"mapping {entity.id}: '{entity.status}' -> '{target}' is not allowed"
            )
