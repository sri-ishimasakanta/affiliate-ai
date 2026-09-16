"""AffiliateLinkTargetService — control-plane の Human 主導オペレーション (transaction owner)。

``create_target`` / ``disable_target`` / ``supersede_target`` のみを公開する。
LLM 呼び出し・外部 API 呼び出しは一切しない。``destination_url`` は Human 入力
(:attr:`AffiliateProgram.tracking_url`) の exact 文字列を **無改変** で凍結する。

gate (create / supersede 共通):
  article 存在 / program 存在 / ArticleAffiliateProgram 関係あり /
  program.status が active / program.tracking_url あり / destination 検証合格 /
  正規化 destination host が provider に対して independently 承認済み。
いずれか失敗なら row は作らない。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.affiliate.destination_policy import is_host_approved
from app.affiliate.destination_safety import (
    AffiliateDestinationError,
    DestinationFacts,
    validate_destination_url,
)
from app.affiliate.link_identity import compute_link_identity_hash
from app.affiliate.token import generate_token
from app.article.fact_freshness import to_storage_utc
from app.exceptions import AffiliateLinkTargetError, EntityNotFoundError
from app.models import AffiliateLinkTarget, AffiliateProgram, Article
from app.models.affiliate_link_target import (
    ALT_ACTIVE,
    ALT_DISABLED,
    ALT_SUPERSEDED,
    alt_transition_allowed,
)
from app.models.enums import AffiliateProgramStatus
from app.repositories.affiliate_link_target_repository import (
    AffiliateLinkTargetRepository,
)
from app.repositories.article_affiliate_program_repository import (
    ArticleAffiliateProgramRepository,
)

_TOKEN_MINT_ATTEMPTS = 5
# affiliate 用途で "有効" とみなす program status。
_USABLE_PROGRAM_STATUSES = frozenset({AffiliateProgramStatus.ACTIVE.value})


class AffiliateLinkTargetService:
    def __init__(
        self,
        session: Session,
        *,
        host_policy: Mapping[str, frozenset[str]] | None = None,
    ) -> None:
        self._session = session
        self._repo = AffiliateLinkTargetRepository(session)
        self._relations = ArticleAffiliateProgramRepository(session)
        # None -> production の DEFAULT policy (fail closed)。tests は明示注入する。
        self._host_policy = host_policy

    # -- create ------------------------------------------------------
    def create_target(
        self,
        *,
        article_id: int,
        affiliate_program_id: int,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> AffiliateLinkTarget:
        now = now or datetime.now(UTC)
        program = self._require_program(affiliate_program_id)
        self._require_article(article_id)
        self._require_relation(article_id, affiliate_program_id)
        facts = self._validate_program_destination(program)

        link_hash = compute_link_identity_hash(
            article_id=article_id,
            affiliate_program_id=affiliate_program_id,
            destination_url=facts.destination_url,
        )

        if idempotency_key is not None:
            existing = self._repo.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                if (
                    existing.article_id == article_id
                    and existing.affiliate_program_id == affiliate_program_id
                    and existing.destination_url == facts.destination_url
                    and existing.link_identity_hash == link_hash
                ):
                    return existing
                raise AffiliateLinkTargetError(
                    f"idempotency_key {idempotency_key!r} already used for a different "
                    "target identity"
                )

        if (
            self._repo.get_active_for_article_program(
                article_id, affiliate_program_id
            )
            is not None
        ):
            raise AffiliateLinkTargetError(
                "an active affiliate link target already exists for this article and "
                "program"
            )

        token = self._mint_token()
        try:
            target = self._repo.add(
                token=token,
                article_id=article_id,
                affiliate_program_id=affiliate_program_id,
                destination_url=facts.destination_url,
                destination_host=facts.destination_host,
                status=ALT_ACTIVE,
                link_identity_hash=link_hash,
                idempotency_key=idempotency_key,
                created_at=to_storage_utc(now),
            )
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise AffiliateLinkTargetError(
                "could not create affiliate link target (uniqueness conflict)"
            ) from exc
        # D-F2B: commit is the LAST local DB operation. No post-commit refresh --
        # AffiliateLinkTargetRepository.add() already flush()es internally
        # (populating id/created_at via RETURNING), and SessionLocal is built with
        # expire_on_commit=False (app/config/database.py), so `target`'s in-memory
        # attributes are already correct and do not go stale on commit. A
        # post-commit refresh() here would be an unnecessary fallible DB
        # round-trip that, if it failed, would raise an unrelated, undocumented
        # exception even though the target was already durably created.
        return target

    # -- disable ---------------------------------------------------
    def disable_target(
        self, target_id: int, *, now: datetime | None = None
    ) -> AffiliateLinkTarget:
        now = now or datetime.now(UTC)
        target = self._repo.get_by_id(target_id)
        if target is None:
            raise EntityNotFoundError("AffiliateLinkTarget", target_id)
        if not alt_transition_allowed(target.status, ALT_DISABLED):
            raise AffiliateLinkTargetError(
                f"target {target_id} status {target.status!r} cannot be disabled"
            )
        self._repo.mark_disabled(target, disabled_at=to_storage_utc(now))
        # D-F2B: commit is the LAST local DB operation. No post-commit refresh --
        # mark_disabled() already flush()es internally, and expire_on_commit=False
        # means `target`'s in-memory attributes already reflect exactly what was
        # committed. See create_target()'s comment above for the full rationale.
        self._session.commit()
        return target

    # -- supersede -----------------------------------------------
    def supersede_target(
        self,
        target_id: int,
        *,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> tuple[AffiliateLinkTarget, AffiliateLinkTarget]:
        """old active target を、現在の ``program.tracking_url`` に基づく新 target で置換する。

        新 destination をまず検証し、old を active から外し、新 target を作り、
        supersede pointer を張る — すべて 1 transaction。途中で失敗したら rollback で
        old は active のまま。戻り値 ``(old_superseded, new_active)``。

        ``idempotency_key`` が既存行に解決する場合、その判定は通常の
        transition guard より **先** に行う (D-F2B.1)。そうしないと「既に
        成功した supersede の再試行」は old が既に superseded であるが故に
        常に "cannot be superseded" で弾かれ、replay 判定へ到達できない
        (transition guard が先に落とす)。genuine replay と認めるのは、
        identity (article_id / affiliate_program_id / destination の
        link_identity_hash) が要求内容と完全一致し、かつ
        ``old.status == superseded`` かつ ``old.superseded_by_id ==
        existing.id`` という historical linkage が実在する場合のみ。
        いずれか一つでも欠ける場合は replay とはみなさず fail closed で
        conflict を報告する (無関係な行を誤って返さない -- 同じ
        idempotency_key を指す既存行があっても、それが「この old を実際に
        supersede して作られたものだ」という証拠がなければ信用しない)。
        """

        now = now or datetime.now(UTC)
        old = self._repo.get_by_id(target_id)
        if old is None:
            raise EntityNotFoundError("AffiliateLinkTarget", target_id)

        if idempotency_key is not None:
            existing = self._repo.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                # D-F2B.1: idempotency 解決は transition guard より先。
                # read-only -- flush/commit/token mint は一切行わない。
                program = self._require_program(old.affiliate_program_id)
                facts = self._validate_program_destination(program)
                link_hash = compute_link_identity_hash(
                    article_id=old.article_id,
                    affiliate_program_id=old.affiliate_program_id,
                    destination_url=facts.destination_url,
                )
                if (
                    existing.article_id == old.article_id
                    and existing.affiliate_program_id == old.affiliate_program_id
                    and existing.link_identity_hash == link_hash
                    and old.status == ALT_SUPERSEDED
                    and old.superseded_by_id == existing.id
                ):
                    # Genuine, already-committed replay of THIS exact
                    # supersede call. `existing` の**現在の** status は見ない
                    # (このリクエストが成功した後、別の操作で existing 自身が
                    # disabled/superseded になっていても、この呼び出しが既に
                    # 成功したという historical fact は変わらない -- D-F2B の
                    # refresh 除去方針を踏襲し refresh もしない)。
                    return old, existing
                raise AffiliateLinkTargetError(
                    f"idempotency_key {idempotency_key!r} already used for a "
                    "different target identity"
                )

        if not alt_transition_allowed(old.status, ALT_SUPERSEDED):
            raise AffiliateLinkTargetError(
                f"target {target_id} status {old.status!r} cannot be superseded"
            )

        program = self._require_program(old.affiliate_program_id)
        facts = self._validate_program_destination(program)
        if facts.destination_url == old.destination_url:
            raise AffiliateLinkTargetError(
                "new destination is identical to the current target; nothing to "
                "supersede"
            )

        link_hash = compute_link_identity_hash(
            article_id=old.article_id,
            affiliate_program_id=old.affiliate_program_id,
            destination_url=facts.destination_url,
        )

        token = self._mint_token()
        try:
            # 1. old を active から外す (partial unique index を空ける)。
            self._repo.begin_supersede(old, disabled_at=to_storage_utc(now))
            # 2. 新 active target。
            new = self._repo.add(
                token=token,
                article_id=old.article_id,
                affiliate_program_id=old.affiliate_program_id,
                destination_url=facts.destination_url,
                destination_host=facts.destination_host,
                status=ALT_ACTIVE,
                link_identity_hash=link_hash,
                idempotency_key=idempotency_key,
                created_at=to_storage_utc(now),
            )
            # 3. supersede pointer。
            self._repo.link_supersede(old, superseded_by_id=new.id)
            # D-F2B: commit remains the last statement inside this try block,
            # exactly as before -- only IntegrityError is translated to
            # AffiliateLinkTargetError here (unchanged rollback/exception
            # contract). No post-commit refresh -- begin_supersede()/add()/
            # link_supersede() all flush() internally (populating id/created_at/
            # superseded_by_id via RETURNING or direct in-memory assignment
            # before flush), and expire_on_commit=False means `old`/`new`'s
            # in-memory attributes already reflect exactly what was committed.
            # A post-commit refresh() here would be an unnecessary fallible DB
            # round-trip that, if it failed, would raise an unrelated,
            # undocumented exception even though the atomic supersede had
            # already durably succeeded.
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise AffiliateLinkTargetError(
                "could not supersede affiliate link target (uniqueness conflict)"
            ) from exc
        return old, new

    # -- reads -----------------------------------------------------
    def get(self, target_id: int) -> AffiliateLinkTarget:
        target = self._repo.get_by_id(target_id)
        if target is None:
            raise EntityNotFoundError("AffiliateLinkTarget", target_id)
        return target

    def list_active_for_article(
        self, article_id: int
    ) -> list[AffiliateLinkTarget]:
        return self._repo.list_active_for_article(article_id)

    # -- internal ------------------------------------------------
    def _require_article(self, article_id: int) -> Article:
        article = self._session.get(Article, article_id)
        if article is None:
            raise EntityNotFoundError("Article", article_id)
        return article

    def _require_program(self, affiliate_program_id: int) -> AffiliateProgram:
        program = self._session.get(AffiliateProgram, affiliate_program_id)
        if program is None:
            raise EntityNotFoundError("AffiliateProgram", affiliate_program_id)
        return program

    def _require_relation(self, article_id: int, affiliate_program_id: int) -> None:
        if (
            self._relations.get_by_article_and_program(
                article_id, affiliate_program_id
            )
            is None
        ):
            raise AffiliateLinkTargetError(
                "no ArticleAffiliateProgram relationship exists for this article and "
                "program"
            )

    def _validate_program_destination(
        self, program: AffiliateProgram
    ) -> DestinationFacts:
        if str(program.status) not in _USABLE_PROGRAM_STATUSES:
            raise AffiliateLinkTargetError(
                f"affiliate program status {program.status!r} does not permit active "
                "affiliate use"
            )
        raw = program.tracking_url
        if not raw:
            raise AffiliateLinkTargetError("affiliate program has no tracking_url")
        try:
            facts = validate_destination_url(raw)
        except AffiliateDestinationError as exc:
            raise AffiliateLinkTargetError(
                f"destination validation failed: {exc}"
            ) from exc
        if not is_host_approved(
            provider=program.provider,
            destination_host=facts.destination_host,
            policy=self._host_policy,
        ):
            raise AffiliateLinkTargetError(
                "destination host is not independently approved for this provider"
            )
        return facts

    def _mint_token(self) -> str:
        for _ in range(_TOKEN_MINT_ATTEMPTS):
            candidate = generate_token()
            if not self._repo.token_exists(candidate):
                return candidate
        raise AffiliateLinkTargetError("could not allocate a unique token")
