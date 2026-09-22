"""WordPressContentUpdateReconciliation -- ``outcome_unknown`` で終わった
content-update run の結果を、事後の live 観測から確定させる append-only な記録。

``outcome_unknown`` は設計上 terminal であり、``succeeded`` へ遷移する経路は
存在しない (D-D5A.1 §16-17 / :data:`WP_CONTENT_UPDATE_TRANSITIONS`)。run 行を
後から書き換えると「実行時に何を観測したか」という監査事実が失われるため、
**run の status は一切変更せず**、別テーブルに「後から人間の判断で照合した
結果」を独立した事実として append する。

verdict は 2 つだけ:

- ``reconciled_succeeded`` -- live な post が run の意図した payload と
  content-equivalent であることを確認した。この run はもう未解決ではなく、
  新しい content update を blocking しない。
- ``unresolved`` -- 照合できなかった。run は blocking のまま残り、次の操作は
  人間が介入するまで進めない (fail closed)。

照合は **読み取りのみ** で行う -- reconciliation が WordPress へ write を
発行することは絶対にない。``succeeded`` / ``failed`` の run を reconcile する
ことも禁止する (既に確定した結果を後から塗り替えないため)。
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

WP_CU_RECONCILED_SUCCEEDED = "reconciled_succeeded"
WP_CU_RECONCILED_UNRESOLVED = "unresolved"

WP_CU_RECONCILIATION_VERDICTS = frozenset({WP_CU_RECONCILED_SUCCEEDED, WP_CU_RECONCILED_UNRESOLVED})


class WordPressContentUpdateReconciliation(Base):
    __tablename__ = "wordpress_content_update_reconciliations"

    __table_args__ = (
        # 1 つの run につき verdict は 1 つだけ。2 回目の reconcile は既存行を
        # そのまま返す no-op になり、重複記録も 2 回目の GET も発生しない。
        UniqueConstraint(
            "wordpress_content_update_run_id",
            name="uq_wp_content_update_reconciliations_run",
        ),
        UniqueConstraint(
            "idempotency_key", name="uq_wp_content_update_reconciliations_idempotency_key"
        ),
        Index(
            "ix_wp_content_update_reconciliations_article_created_id",
            "article_id",
            "created_at",
            "id",
        ),
        CheckConstraint(
            "verdict IN ('reconciled_succeeded', 'unresolved')",
            name="wp_content_update_reconciliation_verdict",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    wordpress_content_update_run_id: Mapped[int] = mapped_column(
        ForeignKey("wordpress_content_update_runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # run から凍結してコピーした所有権。run との不一致は service が拒否する。
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    wordpress_post_id: Mapped[str] = mapped_column(String(64), nullable=False)

    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    # unresolved のときだけ設定される機械可読な理由コード。
    unresolved_reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # なぜ今この run を reconcile したのかという人間の記録 (必須)。
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    # -- 照合時に観測した live state -------------------------------------------
    observed_wordpress_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    observed_wordpress_raw_content_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    observed_modified_gmt_raw: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # -- run から凍結した参照値 (raw namespace / 意図 namespace を混ぜない) -----
    pre_update_wordpress_raw_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # visible text / href 列 / heading 列の一致など、照合の内訳 (本文は含まない)。
    comparison_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
