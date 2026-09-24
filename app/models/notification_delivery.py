"""NotificationDelivery -- 送信の試行 1 回分 (C8.7、append-only)。

「送ったつもり」を無くすための最小の履歴。成功も失敗も 1 行として積み、
上書きしない。

**汎用のメッセージング基盤は作らない。** ここにあるのは運用通知の監査に必要な
事実だけである。

保存しないもの (secret を DB に残さないため):

- SMTP の password / URL / 認証情報
- SMTP の生のやり取り
- 例外の本文 (種別と sanitized な要約だけを残す)

宛先は ``recipient_fingerprint`` (sha256 の先頭) と、ドメインを残した
``recipient_hint`` として持つ。運用者が「誰に届いたか」を確認するには十分で、
アドレスをそのまま撒き散らさない。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# -- channels ------------------------------------------------------------------
CHANNEL_EMAIL = "email"
CHANNELS = (CHANNEL_EMAIL,)

# -- notification types --------------------------------------------------------
NOTIFICATION_DAILY_INCIDENT = "daily_incident"
NOTIFICATION_WEEKLY_REPORT = "weekly_report"
NOTIFICATION_ALERT = "alert"
NOTIFICATION_TEST = "test"
#: モバイル承認の依頼 (C8.8)。承認そのものではなく、レビュー依頼を運ぶだけ。
NOTIFICATION_APPROVAL_REQUEST = "approval_request"
#: 複数の承認依頼を 1 通にまとめたもの (T4.2)。決定は提案ごとに独立している。
NOTIFICATION_APPROVAL_DIGEST = "approval_digest"
NOTIFICATION_TYPES = (
    NOTIFICATION_DAILY_INCIDENT,
    NOTIFICATION_WEEKLY_REPORT,
    NOTIFICATION_ALERT,
    NOTIFICATION_TEST,
    NOTIFICATION_APPROVAL_REQUEST,
    NOTIFICATION_APPROVAL_DIGEST,
)

# -- outcomes ------------------------------------------------------------------
DELIVERY_SENT = "sent"
DELIVERY_FAILED = "failed"
#: 設定が無い/無効なので送らなかった (失敗ではない)。
DELIVERY_SKIPPED = "skipped"
#: 同じ dedupe key で既に送ってあるので送らなかった。
DELIVERY_DUPLICATE = "duplicate"
DELIVERY_OUTCOMES = (DELIVERY_SENT, DELIVERY_FAILED, DELIVERY_SKIPPED, DELIVERY_DUPLICATE)


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"

    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_notification_deliveries_dedupe_key"),
        Index("ix_notification_deliveries_run", "operations_run_id"),
        Index("ix_notification_deliveries_type", "notification_type", "outcome"),
        CheckConstraint("channel IN ('email')", name="notification_deliveries_channel"),
        CheckConstraint(
            "outcome IN ('sent', 'failed', 'skipped', 'duplicate')",
            name="notification_deliveries_outcome",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    operations_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("operations_runs.id", ondelete="RESTRICT"), nullable=True
    )
    alert_id: Mapped[int | None] = mapped_column(
        ForeignKey("operations_alerts.id", ondelete="RESTRICT"), nullable=True
    )

    channel: Mapped[str] = mapped_column(String(16), nullable=False, default=CHANNEL_EMAIL)
    notification_type: Mapped[str] = mapped_column(String(32), nullable=False)

    #: 宛先の安全な表現。生アドレスは保存しない。
    recipient_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    #: ``3 recipient(s) @gmail.com`` のような要約。
    recipient_hint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)

    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 同じ通知を二重に送らないための決定的キー (週次ダイジェストなど)。
    dedupe_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: 例外の **種別** のみ。応答本文は載せない。
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: provider が安全に返す識別子があれば。
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: 送信内容そのものではなく、判断の根拠 (件数など)。
    detail_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
