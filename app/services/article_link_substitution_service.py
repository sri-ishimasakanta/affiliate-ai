"""ArticleLinkSubstitutionService — mapping の作成/atomic remap オーケストレーション
(transaction owner)。

gate (create):
  target 存在 / **target.article_id == mapping.article_id** (他 article 所属の
  target は拒否) / occurrence_identity_hash が 64 桁 hex / active-uniqueness
  (article_id, occurrence_identity_hash) / idempotency_key 一致確認。

gate (remap, D-D1.2):
  old mapping が active であること / 新 target が old と同じ article に属すること。
  新しい active row の ``article_id`` / ``occurrence_identity_hash`` /
  ``original_href`` は呼び出し側から受け取らず、old row からそのまま継承する
  (occurrence drift を構造的に防止する)。1 トランザクション内で old を supersede
  してから新 active row を作る (partial unique active index を安全に解放する)。
  途中で失敗した場合は全体を rollback し、old は active のまま残る
  (``supersede_mapping(old_id, preexisting_replacement_id)`` という旧 API は
  任意の無関係な mapping を replacement として指せてしまう危険な形だったため
  D-D1.2 で削除した — 本番 DB に mapping データは一切存在しない)。

D-D1 は foundation のみ — occurrence 抽出・HTML 置換・WordPress 更新は行わない。
``original_href`` / ``occurrence_identity_hash`` は呼び出し側が既に計算・検証した
値をそのまま凍結する。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.article.fact_freshness import to_storage_utc
from app.exceptions import ArticleLinkSubstitutionMappingError, EntityNotFoundError
from app.models import ALSM_ACTIVE, ArticleLinkSubstitutionMapping
from app.repositories.affiliate_link_target_repository import (
    AffiliateLinkTargetRepository,
)
from app.repositories.article_link_substitution_mapping_repository import (
    ArticleLinkSubstitutionMappingRepository,
)
from app.wordpress.publication_artifact import is_hex64

_IDENTITY_FIELDS = (
    "article_id",
    "occurrence_identity_hash",
    "original_href",
    "affiliate_link_target_id",
)


class ArticleLinkSubstitutionService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._mappings = ArticleLinkSubstitutionMappingRepository(session)
        self._targets = AffiliateLinkTargetRepository(session)

    # -- create ------------------------------------------------------
    def create_mapping(
        self,
        *,
        article_id: int,
        occurrence_identity_hash: str,
        original_href: str,
        affiliate_link_target_id: int,
        idempotency_key: str | None = None,
        approved_at: datetime | None = None,
    ) -> ArticleLinkSubstitutionMapping:
        approved_at = approved_at or datetime.now(UTC)

        if not is_hex64(occurrence_identity_hash):
            raise ArticleLinkSubstitutionMappingError(
                "occurrence_identity_hash must be a 64-character hex hash"
            )
        if not original_href:
            raise ArticleLinkSubstitutionMappingError(
                "original_href must be a non-empty exact string"
            )

        target = self._targets.get_by_id(affiliate_link_target_id)
        if target is None:
            raise EntityNotFoundError("AffiliateLinkTarget", affiliate_link_target_id)
        if target.article_id != article_id:
            raise ArticleLinkSubstitutionMappingError(
                "affiliate_link_target belongs to a different article"
            )

        identity = {
            "article_id": article_id,
            "occurrence_identity_hash": occurrence_identity_hash,
            "original_href": original_href,
            "affiliate_link_target_id": affiliate_link_target_id,
        }

        if idempotency_key is not None:
            existing = self._mappings.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                if self._identity_of(existing) == identity:
                    return existing
                raise ArticleLinkSubstitutionMappingError(
                    f"idempotency_key {idempotency_key!r} already used for a "
                    "different mapping identity"
                )

        active = self._mappings.get_active_for_occurrence(
            article_id, occurrence_identity_hash
        )
        if active is not None:
            raise ArticleLinkSubstitutionMappingError(
                "an active mapping already exists for this exact occurrence"
            )

        mapping = self._mappings.add_active(
            article_id=article_id,
            occurrence_identity_hash=occurrence_identity_hash,
            original_href=original_href,
            affiliate_link_target_id=affiliate_link_target_id,
            approved_at=to_storage_utc(approved_at),
            idempotency_key=idempotency_key,
        )
        # D-F0: commit is the LAST local DB operation. No post-commit refresh --
        # ArticleLinkSubstitutionMappingRepository.add_active() already flush()es
        # internally (populating id/created_at via RETURNING), and SessionLocal is
        # built with expire_on_commit=False (app/config/database.py), so
        # `mapping`'s in-memory attributes are already correct and do not go stale
        # on commit. A post-commit refresh() here would be an unnecessary fallible
        # DB round-trip that, if it failed, would raise an unrelated, undocumented
        # exception even though the mapping was already durably created.
        self._session.commit()
        return mapping

    # -- atomic same-occurrence remap (D-D1.2) --------------------------
    def remap_occurrence(
        self,
        old_mapping_id: int,
        *,
        new_affiliate_link_target_id: int,
        idempotency_key: str | None = None,
        approved_at: datetime | None = None,
    ) -> ArticleLinkSubstitutionMapping:
        """old (active) mapping を、同じ occurrence
        (article_id / occurrence_identity_hash / original_href) を保ったまま
        新しい target へ原子的に付け替える。

        old: active -> superseded (``superseded_by_id`` = new.id、ちょうど 1 回)。
        new: old からそのまま継承した同一 occurrence で active。

        呼び出し側は article_id / occurrence_identity_hash / original_href を
        再入力しない — old row から継承することで occurrence drift を構造的に
        防止する。1 トランザクション内で完結し、途中で失敗した場合は old の
        active 状態を含め全体を rollback する (この service がトランザクション
        境界を所有する)。
        """

        approved_at = approved_at or datetime.now(UTC)
        old = self._require_mapping(old_mapping_id)

        # idempotency: 同じ key で既にこの old -> new remap が完了済みなら、その
        # 既存の replacement 行をそのまま返す (retry で二重 remap しない)。
        if idempotency_key is not None:
            existing = self._mappings.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                if (
                    existing.article_id == old.article_id
                    and existing.occurrence_identity_hash
                    == old.occurrence_identity_hash
                    and existing.original_href == old.original_href
                    and existing.affiliate_link_target_id
                    == new_affiliate_link_target_id
                    and old.superseded_by_id == existing.id
                ):
                    return existing
                raise ArticleLinkSubstitutionMappingError(
                    f"idempotency_key {idempotency_key!r} already used for a "
                    "different remap identity"
                )

        if old.status != ALSM_ACTIVE:
            raise ArticleLinkSubstitutionMappingError(
                f"mapping {old.id}: remap requires an active mapping (current "
                f"status '{old.status}')"
            )

        new_target = self._targets.get_by_id(new_affiliate_link_target_id)
        if new_target is None:
            raise EntityNotFoundError(
                "AffiliateLinkTarget", new_affiliate_link_target_id
            )
        if new_target.article_id != old.article_id:
            raise ArticleLinkSubstitutionMappingError(
                "affiliate_link_target belongs to a different article"
            )

        try:
            self._mappings.mark_superseded_for_remap(old)
            new_mapping = self._mappings.add_active(
                article_id=old.article_id,
                occurrence_identity_hash=old.occurrence_identity_hash,
                original_href=old.original_href,
                affiliate_link_target_id=new_affiliate_link_target_id,
                approved_at=to_storage_utc(approved_at),
                idempotency_key=idempotency_key,
            )
            self._mappings.link_superseded_by(old, new_id=new_mapping.id)
        except Exception:
            self._session.rollback()
            raise

        # D-F0: commit is the LAST local DB operation for this atomic remap. No
        # post-commit refresh -- mark_superseded_for_remap()/add_active()/
        # link_superseded_by() all flush() internally (populating id/created_at/
        # superseded_by_id via RETURNING or direct in-memory assignment before
        # flush), and expire_on_commit=False means `old`/`new_mapping`'s in-memory
        # attributes already reflect exactly what was committed. A post-commit
        # refresh() here would be an unnecessary fallible DB round-trip that, if
        # it failed, would raise an unrelated, undocumented exception even though
        # the atomic remap had already durably succeeded.
        self._session.commit()
        return new_mapping

    def revoke_mapping(self, mapping_id: int) -> ArticleLinkSubstitutionMapping:
        mapping = self._require_mapping(mapping_id)
        self._mappings.revoke(mapping)
        # D-F0: commit is the LAST local DB operation. No post-commit refresh --
        # revoke() already flush()es internally, and expire_on_commit=False means
        # `mapping`'s in-memory attributes already reflect exactly what was
        # committed. See create_mapping()'s comment above for the full rationale.
        self._session.commit()
        return mapping

    # -- reads ----------------------------------------------------------
    def get(self, mapping_id: int) -> ArticleLinkSubstitutionMapping:
        return self._require_mapping(mapping_id)

    def list_active_for_article(
        self, article_id: int
    ) -> list[ArticleLinkSubstitutionMapping]:
        return self._mappings.list_active_for_article(article_id)

    # -- helpers ----------------------------------------------------------
    def _require_mapping(self, mapping_id: int) -> ArticleLinkSubstitutionMapping:
        mapping = self._mappings.get_by_id(mapping_id)
        if mapping is None:
            raise EntityNotFoundError("ArticleLinkSubstitutionMapping", mapping_id)
        return mapping

    @staticmethod
    def _identity_of(mapping: ArticleLinkSubstitutionMapping) -> dict:
        return {f: getattr(mapping, f) for f in _IDENTITY_FIELDS}
