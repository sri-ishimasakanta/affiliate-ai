"""add wordpress content update runs

Revision ID: 3d98dc94680c
Revises: 45ed2fbd00c6
Create Date: 2026-09-16 11:33:44.747571

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3d98dc94680c"
down_revision: str | Sequence[str] | None = "45ed2fbd00c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "wordpress_content_update_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("article_id", sa.Integer(), nullable=False),
        sa.Column("wordpress_post_id", sa.String(length=64), nullable=False),
        sa.Column("article_publication_artifact_id", sa.Integer(), nullable=False),
        sa.Column("artifact_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("endpoint_path", sa.String(length=255), nullable=False),
        sa.Column("update_payload_json", sa.Text(), nullable=False),
        sa.Column("update_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("content_update_request_identity_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "target_content_update_request_identity_hash", sa.String(length=64), nullable=False
        ),
        sa.Column("target_base_url", sa.String(length=1024), nullable=False),
        sa.Column("request_content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "expected_pre_update_wordpress_raw_content_hash", sa.String(length=64), nullable=False
        ),
        sa.Column(
            "observed_pre_update_wordpress_raw_content_hash", sa.String(length=64), nullable=False
        ),
        sa.Column("observed_pre_update_modified_gmt_raw", sa.String(length=64), nullable=True),
        sa.Column("response_content_raw_hash", sa.String(length=64), nullable=True),
        sa.Column("response_content_rendered_hash", sa.String(length=64), nullable=True),
        sa.Column("wordpress_modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("wordpress_modified_gmt_raw", sa.String(length=64), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("provider_error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("response_snapshot", sa.JSON(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "expected_pre_update_wordpress_raw_content_hash = "
            "observed_pre_update_wordpress_raw_content_hash",
            name=op.f("ck_wordpress_content_update_runs_pre_update_raw_match"),
        ),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.id"],
            name=op.f("fk_wordpress_content_update_runs_article_id_articles"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["article_publication_artifact_id"],
            ["article_publication_artifacts.id"],
            name=op.f(
                "fk_wordpress_content_update_runs_article_publication_artifact_id_article_publication_artifacts"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wordpress_content_update_runs")),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_wordpress_content_update_runs_idempotency_key"
        ),
    )
    with op.batch_alter_table("wordpress_content_update_runs", schema=None) as batch_op:
        batch_op.create_index(
            "ix_wordpress_content_update_runs_article_created_id",
            ["article_id", "created_at", "id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_wordpress_content_update_runs_article_id"), ["article_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_wordpress_content_update_runs_article_publication_artifact_id"),
            ["article_publication_artifact_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f(
                "ix_wordpress_content_update_runs_target_content_update_request_identity_hash"
            ),
            ["target_content_update_request_identity_hash"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("wordpress_content_update_runs", schema=None) as batch_op:
        batch_op.drop_index(
            batch_op.f(
                "ix_wordpress_content_update_runs_target_content_update_request_identity_hash"
            )
        )
        batch_op.drop_index(
            batch_op.f("ix_wordpress_content_update_runs_article_publication_artifact_id")
        )
        batch_op.drop_index(batch_op.f("ix_wordpress_content_update_runs_article_id"))
        batch_op.drop_index("ix_wordpress_content_update_runs_article_created_id")

    op.drop_table("wordpress_content_update_runs")
