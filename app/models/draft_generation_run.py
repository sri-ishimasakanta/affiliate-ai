"""DraftGenerationRun — **How generation was executed** を再現可能にする lifecycle record。

- 1 run は必ず 1 :class:`DraftInputSnapshot` へ bind する (``snapshot_id`` NOT NULL,
  ``ON DELETE RESTRICT``)。
- prepare 後は **execution identity** (snapshot / prompt_package / prompt_input_hash /
  rendered_prompt / rendered_prompt_hash / provider / model / editorial_overrides /
  generation_parameters / idempotency_key) が immutable。以降は狭い遷移操作
  (``mark_running`` / ``mark_succeeded`` / ``mark_failed`` / ``mark_cancelled``) だけ。
- DraftInputSnapshot と違い **lifecycle record なので ``updated_at`` を持つ**。
- 生成成功 (``status=succeeded``) は **Article.body 採用ではない**。採用は将来の
  promotion phase で Human action によってのみ行う。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.article import Article
    from app.models.draft_input_snapshot import DraftInputSnapshot

# 表記揺れ防止の定数 (§22)。
PROMPT_PACKAGE_VERSION = "draft_prompt_v1"
# v2 (Phase 3C-4C.1): LLM-visible comparison-axis projection を追加。
# Snapshot plan.comparison_axes から affiliate economics / invoice・Japan business /
# generic Japanese-support 軸を PromptPackage から除外する (Snapshot は不変)。
# PromptPackage schema / renderer template 本文は v1 と同一。
PROMPT_BUILDER_VERSION = "draft_prompt_builder_v2"
PROMPT_TEMPLATE_VERSION = "article_roundup_v1"

# status (§17)。String 保存だが定数で扱う。
RUN_PREPARED = "prepared"
RUN_RUNNING = "running"
RUN_SUCCEEDED = "succeeded"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
RUN_STATUSES = frozenset(
    {RUN_PREPARED, RUN_RUNNING, RUN_SUCCEEDED, RUN_FAILED, RUN_CANCELLED}
)
RUN_TERMINAL_STATUSES = frozenset({RUN_SUCCEEDED, RUN_FAILED, RUN_CANCELLED})

# execution_mode (§40)。manual のみ V1 で実行可、他は interface/stub。
MODE_MANUAL = "manual"
MODE_LOCAL_CLI = "local_cli"
MODE_API = "api"
EXECUTION_MODES = frozenset({MODE_MANUAL, MODE_LOCAL_CLI, MODE_API})


class DraftGenerationRun(Base):
    __tablename__ = "draft_generation_runs"

    __table_args__ = (
        UniqueConstraint(
            "idempotency_key", name="uq_draft_generation_runs_idempotency_key"
        ),
        Index(
            "ix_draft_generation_runs_article_created_id",
            "article_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- binding (prepare 後 immutable) ---
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("draft_input_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    snapshot_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # --- lifecycle ---
    status: Mapped[str] = mapped_column(String(20), nullable=False)

    # --- execution config (prepare 後 immutable) ---
    execution_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)

    prompt_template_version: Mapped[str] = mapped_column(String(40), nullable=False)
    prompt_builder_version: Mapped[str] = mapped_column(String(40), nullable=False)

    # --- frozen prompt artifact (prepare 後 immutable。execute はこれを使う) ---
    prompt_package: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    prompt_input_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    rendered_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    rendered_prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Human 確定の編集判断 (prepare 後 immutable)。
    editorial_overrides: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # 既知の安全キーのみ。secret は入れない (prepare 後 immutable)。
    generation_parameters: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )
    # 課金・副作用のある execute の重複防止 (prepare 後 immutable, nullable, UNIQUE)。
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # --- generation output (lifecycle mutable) ---
    raw_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_meta_description: Mapped[str | None] = mapped_column(
        String(400), nullable=True
    )
    generation_notes: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    validation_report: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )
    token_usage: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # sanitized のみ (Bearer / x-api-key / sk-* を除去済み)。
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # lifecycle record なので updated_at を持つ (Snapshot とは対照)。
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    article: Mapped[Article] = relationship(back_populates="draft_generation_runs")
    snapshot: Mapped[DraftInputSnapshot] = relationship(back_populates="runs")
