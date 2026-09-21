"""AffiliateCommissionFact の永続化アクセス。

``commit`` は行わず ``flush`` のみ。汎用 ``update`` / ``delete`` は持たない。
identity フィールド (:data:`~app.models.affiliate_commission_fact.FROZEN_FIELDS`)
は ``add()`` 時のみ設定し、以降は変更しない。provider 側で変化しうるフィールド
(status / amount / currency / source / payout タイムスタンプ / last_seen_at /
source_import_run_id) だけを :meth:`update_observed_state` で更新する
(current-fact-row + UPSERT 設計。理由は model docstring 参照)。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AffiliateCommissionFact


class AffiliateCommissionFactRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        *,
        affiliate_program_id: int,
        provider: str,
        source_commission_id: str,
        source_organization_id: str | None,
        event_type: str,
        provider_status: str,
        commission_amount: Decimal | None,
        currency: str | None,
        source: str | None,
        occurred_at: datetime,
        payout_requested_at: datetime | None,
        payout_approved_at: datetime | None,
        payout_realized_at: datetime | None,
        first_seen_at: datetime,
        last_seen_at: datetime,
        source_import_run_id: int,
    ) -> AffiliateCommissionFact:
        entity = AffiliateCommissionFact(
            affiliate_program_id=affiliate_program_id,
            provider=provider,
            source_commission_id=source_commission_id,
            source_organization_id=source_organization_id,
            event_type=event_type,
            provider_status=provider_status,
            commission_amount=commission_amount,
            currency=currency,
            source=source,
            occurred_at=occurred_at,
            payout_requested_at=payout_requested_at,
            payout_approved_at=payout_approved_at,
            payout_realized_at=payout_realized_at,
            first_seen_at=first_seen_at,
            last_seen_at=last_seen_at,
            source_import_run_id=source_import_run_id,
        )
        self._session.add(entity)
        self._session.flush()
        return entity

    def get_by_id(self, fact_id: int) -> AffiliateCommissionFact | None:
        return self._session.get(AffiliateCommissionFact, fact_id)

    def get_by_source_id(
        self, *, provider: str, source_commission_id: str
    ) -> AffiliateCommissionFact | None:
        stmt = select(AffiliateCommissionFact).where(
            AffiliateCommissionFact.provider == provider,
            AffiliateCommissionFact.source_commission_id == source_commission_id,
        )
        return self._session.scalars(stmt).first()

    def get_map_by_source_ids(
        self, *, provider: str, source_commission_ids: Iterable[str]
    ) -> dict[str, AffiliateCommissionFact]:
        ids = list(source_commission_ids)
        if not ids:
            return {}
        stmt = select(AffiliateCommissionFact).where(
            AffiliateCommissionFact.provider == provider,
            AffiliateCommissionFact.source_commission_id.in_(ids),
        )
        return {
            row.source_commission_id: row
            for row in self._session.scalars(stmt).all()
        }

    def list_for_program(
        self, affiliate_program_id: int
    ) -> list[AffiliateCommissionFact]:
        stmt = (
            select(AffiliateCommissionFact)
            .where(AffiliateCommissionFact.affiliate_program_id == affiliate_program_id)
            .order_by(AffiliateCommissionFact.occurred_at, AffiliateCommissionFact.id)
        )
        return list(self._session.scalars(stmt).all())

    # -- narrow update: only the provider-mutable observed-state fields ----
    def update_observed_state(
        self,
        entity: AffiliateCommissionFact,
        *,
        provider_status: str,
        commission_amount: Decimal | None,
        currency: str | None,
        source: str | None,
        payout_requested_at: datetime | None,
        payout_approved_at: datetime | None,
        payout_realized_at: datetime | None,
        last_seen_at: datetime,
        source_import_run_id: int,
    ) -> AffiliateCommissionFact:
        entity.provider_status = provider_status
        entity.commission_amount = commission_amount
        entity.currency = currency
        entity.source = source
        entity.payout_requested_at = payout_requested_at
        entity.payout_approved_at = payout_approved_at
        entity.payout_realized_at = payout_realized_at
        entity.last_seen_at = last_seen_at
        entity.source_import_run_id = source_import_run_id
        self._session.flush()
        return entity
