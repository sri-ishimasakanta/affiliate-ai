"""WordPressContentUpdateRun — 既に published 済みの WordPress post の **content を
update する** auditable な実行記録 (append-only)。

D-D5A / D-D5A.1 で確定した設計を反映する:

- **2 つの独立した hash namespace を厳密に分離する** (D-D5A.1 §3):

  * ``request_content_hash`` -- 意図した (submit する) content の identity。
    ``artifact.tracked_html`` の ``compute_text_hash``。
  * ``*_wordpress_raw_content_hash`` (``expected_pre_update_...`` /
    ``observed_pre_update_...`` / ``response_content_raw_hash``) -- WordPress が
    実際に保存している content (``content.raw``, ``context=edit``) の identity。

  WordPress は保存時に inline style 等を正当に sanitize するため、この 2 つは
  同じ入力からでも一致しない (D-D5A で実証済み)。このモデルのどのフィールドも、
  この 2 namespace を等値比較する目的で設計してはならない。

- **status lifecycle に ``prepared`` を持たない** (D-D5A.1 §6 の帰結)。実行前の
  live preflight GET (D-D5A.1 §4-5) が ``CONTENT_NOOP`` か ``UPDATE_REQUIRED`` かを
  決定し、``CONTENT_NOOP`` / remote drift の場合は run 行を一切作らない
  (D-D5A.1 §17-18)。よって行が存在する時点で「実際に 1 回の write request を試みる」
  ことが確定しており、``running`` から直接始まる
  (``AffiliateTargetProjectionPushRun`` と同じ 4-state パターン)。

  D-D5B.1: ``running`` の正確な意味は「preflight 完了後、network-write 境界の
  **直前** に durable に記録された update 試行/意図」であって、「WordPress へ
  request が実際に送信済みであることの証明」では **ない**。Transaction A の
  commit 成功後、実際の HTTP request 送信前にプロセスが crash する window が
  理論上存在するため、``running`` は意図的に blocking/ambiguous なままであり、
  将来の execution/reconciliation が解決するまで自動では進まない (D-D5A.1 §16-17)。

- ``expected_pre_update_wordpress_raw_content_hash`` (実行前にローカルの証跡から
  解決される期待値) と ``observed_pre_update_wordpress_raw_content_hash`` (live
  preflight GET が実際に観測した値) は **両方** 凍結する (D-D5A.1 §18-19) --
  「何を期待したか」と「実際に何を見たか」を後から監査できるようにする。

  D-D5B.1: ``running`` 行は preflight **成功後にのみ** 作られる (D-D5A.1 §21-23:
  prepared 相当の durable な事前状態は存在しない) ため、``running`` として
  永続化された行は必ず両方の値を持つ -- ``observed_pre_update_...`` が
  ``NULL`` のまま ``running`` になっている行は不整合な状態であり、
  ``nullable=False`` で禁止する (D-D5B では ``expected_pre_update_...`` のみ
  NOT NULL だったが、これは D-D5B.1 で修正した誤り)。

- ``response_content_raw_hash`` は **normative** -- succeeded run の観測済み
  post-update raw state であり、次回 operation の ``expected_pre_update_...``
  baseline になる (D-D5A.1 §9-10)。``response_content_rendered_hash`` /
  ``wordpress_modified_at`` / ``wordpress_modified_gmt_raw`` /
  ``observed_pre_update_modified_gmt_raw`` は informational/audit のみで、
  normative な成功判定には使わない (D-D5A.1 §11)。

- ``updated_at`` を持たない。retry は新しい run を append する
  (outcome_unknown row への自動 retry は禁止 -- D-D5A.1 §16-17)。
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

# status lifecycle (D-D5A.1 §6-7 で確定): running から直接始まり、prepared を持たない。
WP_CONTENT_UPDATE_RUNNING = "running"
WP_CONTENT_UPDATE_SUCCEEDED = "succeeded"
WP_CONTENT_UPDATE_FAILED = "failed"
WP_CONTENT_UPDATE_OUTCOME_UNKNOWN = "outcome_unknown"

WP_CONTENT_UPDATE_STATUSES = frozenset(
    {
        WP_CONTENT_UPDATE_RUNNING,
        WP_CONTENT_UPDATE_SUCCEEDED,
        WP_CONTENT_UPDATE_FAILED,
        WP_CONTENT_UPDATE_OUTCOME_UNKNOWN,
    }
)
WP_CONTENT_UPDATE_TERMINAL_STATUSES = frozenset(
    {WP_CONTENT_UPDATE_SUCCEEDED, WP_CONTENT_UPDATE_FAILED, WP_CONTENT_UPDATE_OUTCOME_UNKNOWN}
)

# running からのみ、3 つの terminal 状態へ。terminal 同士の遷移・reset・retry 遷移は無い。
WP_CONTENT_UPDATE_TRANSITIONS: dict[str, frozenset[str]] = {
    WP_CONTENT_UPDATE_RUNNING: frozenset(
        {WP_CONTENT_UPDATE_SUCCEEDED, WP_CONTENT_UPDATE_FAILED, WP_CONTENT_UPDATE_OUTCOME_UNKNOWN}
    ),
    WP_CONTENT_UPDATE_SUCCEEDED: frozenset(),
    WP_CONTENT_UPDATE_FAILED: frozenset(),
    WP_CONTENT_UPDATE_OUTCOME_UNKNOWN: frozenset(),
}


def wp_content_update_run_transition_allowed(current: str, target: str) -> bool:
    """``current`` から ``target`` への status 遷移が許可されているか (同一 status も不可)。"""

    return target in WP_CONTENT_UPDATE_TRANSITIONS.get(current, frozenset())


# running 作成後 immutable な request/preflight フィールド (D-D5A.1 §22)。
FROZEN_FIELDS = (
    "article_id",
    "wordpress_post_id",
    "article_publication_artifact_id",
    "artifact_hash",
    "method",
    "endpoint_path",
    "update_payload_json",
    "update_payload_hash",
    "content_update_request_identity_hash",
    "target_content_update_request_identity_hash",
    "target_base_url",
    "request_content_hash",
    "expected_pre_update_wordpress_raw_content_hash",
    "observed_pre_update_wordpress_raw_content_hash",
    "observed_pre_update_modified_gmt_raw",
    "idempotency_key",
    "created_at",
    "started_at",
)


class WordPressContentUpdateRun(Base):
    __tablename__ = "wordpress_content_update_runs"

    __table_args__ = (
        UniqueConstraint(
            "idempotency_key", name="uq_wordpress_content_update_runs_idempotency_key"
        ),
        Index(
            "ix_wordpress_content_update_runs_article_created_id",
            "article_id",
            "created_at",
            "id",
        ),
        # D-D5B.2: 永続化された running 行は preflight 成功後にのみ作られるため
        # (D-D5A.1 §4-5) expected/observed の pre-update raw baseline は必ず
        # 一致する -- DB 制約としても defense-in-depth で保証する。
        CheckConstraint(
            "expected_pre_update_wordpress_raw_content_hash = "
            "observed_pre_update_wordpress_raw_content_hash",
            name="pre_update_raw_match",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # 凍結した authoritative post id コピー (Article.wordpress_post_id 由来)。
    wordpress_post_id: Mapped[str] = mapped_column(String(64), nullable=False)

    article_publication_artifact_id: Mapped[int] = mapped_column(
        ForeignKey("article_publication_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    artifact_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False)

    method: Mapped[str] = mapped_column(String(10), nullable=False)
    endpoint_path: Mapped[str] = mapped_column(String(255), nullable=False)

    # 送信予定/送信済みの exact な update JSON body ({"content": tracked_html} のみ)。
    update_payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    update_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_update_request_identity_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_content_update_request_identity_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    # 凍結した設置先 (canonical。credential は含まない)。
    target_base_url: Mapped[str] = mapped_column(String(1024), nullable=False)

    # -- 意図した content の identity (WordPress raw とは別 namespace) -----------
    request_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # -- pre-update raw-state baseline (WordPress raw の namespace) --------------
    # 実行前にローカルの証跡 (直近の succeeded update run、無ければ
    # WordPressPublicationRun) から解決した期待値。
    expected_pre_update_wordpress_raw_content_hash: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    # live preflight GET が実際に観測した値。running は preflight 成功後にのみ
    # 作られるため、running として永続化された行は必ずこの値を持つ -- NOT NULL
    # (D-D5B.1: D-D5B では誤って nullable だった。DB 制約としても NULL を禁止する)。
    observed_pre_update_wordpress_raw_content_hash: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    # informational/audit only (D-D5A.1 §11, D-D5B.1 §4) -- raw content の観測とは
    # 異なり、provider のタイムスタンプ形式・精度は normative な gate にできないため
    # nullable のまま維持する。
    observed_pre_update_modified_gmt_raw: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )

    # -- post-update 観測結果 (succeeded のみ populate) --------------------------
    # normative: 次回 operation の expected_pre_update baseline になる (D-D5A.1 §9)。
    response_content_raw_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # informational only (D-D5A.1 §10)。
    response_content_rendered_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # informational/audit only -- normative な成功判定には使わない (D-D5A.1 §11)。
    wordpress_modified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    wordpress_modified_gmt_raw: Mapped[str | None] = mapped_column(String(64), nullable=True)

    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # updated_at は持たない (append-only)。
