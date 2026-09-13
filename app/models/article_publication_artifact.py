"""ArticlePublicationArtifact — canonical Article body から決定的に導出された、
凍結・再現可能な「tracked (affiliate-substituted) publication HTML」の 1 バージョン。

D-D0/D-D0.1 で確定した設計:

- :attr:`~app.models.article.Article.body` (canonical, immutable) とは完全に別物。
  このテーブルへの書き込みは canonical body・``DraftGenerationRun``・
  ``ArticleDraftPromotion``・``WordPressDraftRun``・``WordPressPublicationRun`` の
  いずれも一切変更しない (additive のみ)。
- **immutable-content + set-once-approval**: 生成後、``approved_at`` /
  ``approved_artifact_hash`` の組だけが ``NULL -> (値)`` に **ちょうど 1 回** 遷移
  できる。厳密な append-only ではない (その 1 点においてのみ)。他の全フィールドは
  行の生成時から不変。
- ``artifact_hash`` は ``(artifact_schema_version, article_id, canonical_body_hash,
  renderer_version, substitution_manifest)`` のみの純関数。**現在の**
  mapping/target/projection acknowledgement 状態を一切参照しない — 過去の artifact
  は、それらが後でどう変化しても常に同じように再現・検証できる。
- ``substitution_manifest_json`` は凍結された request 側の事実 (D-D1A の
  projection acknowledgement run と同じ精神): このテーブルに限り、token は
  ``https://bizfluxlab.com/go/{token}`` として公開される予定の値なので manifest に
  含めてよい (destination_url / credential / HMAC 値 / PII は一切含めない)。
- ``updated_at`` を持たない。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# 生成後 (承認フィールドを除き) 不変なフィールド。
FROZEN_FIELDS = (
    "article_id",
    "canonical_body_hash",
    "renderer_version",
    "artifact_schema_version",
    "substitution_manifest_json",
    "artifact_hash",
    "tracked_html",
    "tracked_html_hash",
    "substitution_count",
    "generated_at",
)


class ArticlePublicationArtifact(Base):
    __tablename__ = "article_publication_artifacts"

    __table_args__ = (
        UniqueConstraint("artifact_hash", name="uq_article_publication_artifacts_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    canonical_body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    renderer_version: Mapped[str] = mapped_column(String(40), nullable=False)
    artifact_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)

    # canonical JSON (Text)。1 出現あたり: occurrence_ordinal / occurrence_identity_hash /
    # mapping_id / affiliate_link_target_id / token / target_projection_version /
    # original_href / replacement_href / rel_before / rel_after のみ。
    substitution_manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    # 上記 + article_id/canonical_body_hash/renderer_version/artifact_schema_version
    # の純関数。UNIQUE — 凍結された artifact identity そのもの。
    artifact_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    tracked_html: Mapped[str] = mapped_column(Text, nullable=False)
    tracked_html_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    substitution_count: Mapped[int] = mapped_column(Integer, nullable=False)

    # --- 承認 (set-once。NULL -> 値、ちょうど 1 回だけ) ---------------------
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approved_artifact_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )

    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # updated_at は持たない。
