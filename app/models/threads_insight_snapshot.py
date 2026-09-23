"""ThreadsInsightSnapshot (T4、append-only)。

公開済み投稿の指標を、観測した時点ごとに 1 行として積む。上書きしない。

**「観測できなかった」と「0 だった」を混同しない。**

- Meta が明示的に 0 を返したら 0 を保存する。
- 指標が応答に無い / 未対応 / 読めなかった場合は **NULL** のままにする。
- 欠測を 0 で埋める処理はどこにも置かない。

公式に存在しない指標 (reach / impressions / CTR / engagement rate) は列として
持たない。持てば、いつか誰かが埋めたくなるからである。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
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

#: 取得の結果。
SNAPSHOT_OBSERVED = "observed"
#: 応答は返ったが、指標が 1 つも含まれていなかった (REPOST_FACADE など)。
SNAPSHOT_EMPTY = "empty"
SNAPSHOT_FAILED = "failed"
SNAPSHOT_OUTCOMES = (SNAPSHOT_OBSERVED, SNAPSHOT_EMPTY, SNAPSHOT_FAILED)


class ThreadsInsightSnapshot(Base):
    __tablename__ = "threads_insight_snapshots"

    __table_args__ = (
        # 同じ観測時刻の取り込みを二重に積まない。
        UniqueConstraint(
            "threads_publication_id", "observed_at", name="uq_threads_insight_observation"
        ),
        Index("ix_threads_insight_snapshots_pub", "threads_publication_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    threads_publication_id: Mapped[int] = mapped_column(
        ForeignKey("threads_publications.id", ondelete="RESTRICT"), nullable=False
    )
    threads_media_id: Mapped[str] = mapped_column(String(64), nullable=False)

    #: 観測した時刻 (UTC)。Meta 側の集計時刻ではなく、こちらが読んだ時刻。
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: 公開から観測までの経過時間。成熟度の判定に使う。
    age_hours: Mapped[float | None] = mapped_column(nullable=True)

    # -- 公式に存在する指標だけ。すべて nullable (未観測は NULL のまま)。
    views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    likes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    replies: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reposts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quotes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shares: Mapped[int | None] = mapped_column(Integer, nullable=True)

    outcome: Mapped[str] = mapped_column(String(16), nullable=False, default=SNAPSHOT_OBSERVED)
    #: 応答に含まれなかった指標の名前 (0 で埋めた印ではない)。
    missing_json: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
