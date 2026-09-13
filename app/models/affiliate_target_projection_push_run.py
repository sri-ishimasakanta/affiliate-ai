"""AffiliateTargetProjectionPushRun — WordPress runtime への affiliate target projection
POST の **auditable な実行記録** (append-only)。

D-D0.2/D-D0.3 で判明した重要な事実を反映する:

- WordPress の projection endpoint は **request 単位で完全 atomic** (§D-D0.2/D-D0.3)。
  レスポンスは per-target のデータを一切返さない — batch レベルの集約 (count 群と
  echo された snapshot hash) のみ。よって本 run は:

  * **request 側の事実** (``request_manifest_json`` — 何を送ろうとしたか、送信直前に
    凍結) と
  * **response 側の事実** (``http_status`` / ``server_code`` / 4 つの count /
    echo された snapshot hash — サーバが実際に返した batch レベルの証跡)

  を明確に分離して保持する。per-target の decision (insert/update/noop) や
  per-target のレスポンス token/entry hash は **サーバから返らないため保存しない**
  (捏造しない)。

- ``updated_at`` を持たない。再送は新しい run を append する。
- secret / signature / raw request body / raw response body / HTTP headers /
  full token / destination_url / PII は一切保存しない。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# status lifecycle (D-D0.3 §10 で確定した 4 状態)。
ATPP_RUNNING = "running"
ATPP_SUCCEEDED = "succeeded"
ATPP_FAILED = "failed"
ATPP_OUTCOME_UNKNOWN = "outcome_unknown"

ATPP_STATUSES = frozenset(
    {ATPP_RUNNING, ATPP_SUCCEEDED, ATPP_FAILED, ATPP_OUTCOME_UNKNOWN}
)
ATPP_TERMINAL_STATUSES = frozenset(
    {ATPP_SUCCEEDED, ATPP_FAILED, ATPP_OUTCOME_UNKNOWN}
)

# running からのみ、3 つの terminal 状態へ。terminal 同士の遷移は無い
# (succeeded/failed/outcome_unknown は書き換えない — 新しい run を append する)。
ATPP_TRANSITIONS: dict[str, frozenset[str]] = {
    ATPP_RUNNING: frozenset({ATPP_SUCCEEDED, ATPP_FAILED, ATPP_OUTCOME_UNKNOWN}),
    ATPP_SUCCEEDED: frozenset(),
    ATPP_FAILED: frozenset(),
    ATPP_OUTCOME_UNKNOWN: frozenset(),
}


def atpp_transition_allowed(current: str, target: str) -> bool:
    """``current`` から ``target`` への status 遷移が許可されているか (同一 status は不可)。"""

    return target in ATPP_TRANSITIONS.get(current, frozenset())


# run 作成後 immutable な request identity フィールド。
FROZEN_FIELDS = (
    "snapshot_scope",
    "runtime_origin",
    "requested_snapshot_hash",
    "requested_target_count",
    "request_manifest_json",
)


class AffiliateTargetProjectionPushRun(Base):
    __tablename__ = "affiliate_target_projection_push_runs"

    __table_args__ = (
        # acknowledgement resolver の主要な lookup: 1 runtime_origin の中で新しい順。
        Index(
            "ix_affiliate_target_projection_push_runs_origin_created_id",
            "runtime_origin",
            "created_at",
            "id",
        ),
        Index("ix_affiliate_target_projection_push_runs_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False)

    # --- request 側 (POST 前に凍結。immutable) ---------------------------
    # 現状は常に "full" (ローカルの全 AffiliateLinkTarget を送る)。将来 partial
    # モードが追加されても、このフィールドが明示的に区別する — "full" を暗黙に
    # 仮定しない。
    snapshot_scope: Mapped[str] = mapped_column(String(20), nullable=False)
    # 正規化済み HTTPS origin (credential/path を含まない)。例: https://bizfluxlab.com
    runtime_origin: Mapped[str] = mapped_column(String(255), nullable=False)
    requested_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_target_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # canonical JSON (Text)。1 target あたり: affiliate_link_target_id /
    # token_fingerprint / link_identity_hash / status / projection_version /
    # entry_hash のみ。full token も destination_url も持たない。
    request_manifest_json: Mapped[str] = mapped_column(Text, nullable=False)

    # --- response 側 (POST 後、batch レベルの証跡のみ。per-target データは無い) ---
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    server_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_projection_snapshot_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    received_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    inserted_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unchanged_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # updated_at は持たない (append-only)。
