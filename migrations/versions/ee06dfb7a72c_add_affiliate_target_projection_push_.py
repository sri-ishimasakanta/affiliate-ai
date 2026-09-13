"""add affiliate target projection push runs

Revision ID: ee06dfb7a72c
Revises: a0674bcc7cb2
Create Date: 2026-09-14 00:36:56.324213

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'ee06dfb7a72c'
down_revision: str | Sequence[str] | None = 'a0674bcc7cb2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'affiliate_target_projection_push_runs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('snapshot_scope', sa.String(length=20), nullable=False),
        sa.Column('runtime_origin', sa.String(length=255), nullable=False),
        sa.Column('requested_snapshot_hash', sa.String(length=64), nullable=False),
        sa.Column('requested_target_count', sa.Integer(), nullable=False),
        sa.Column('request_manifest_json', sa.Text(), nullable=False),
        sa.Column('http_status', sa.Integer(), nullable=True),
        sa.Column('server_code', sa.String(length=64), nullable=True),
        sa.Column(
            'response_projection_snapshot_hash', sa.String(length=64), nullable=True
        ),
        sa.Column('received_count', sa.Integer(), nullable=True),
        sa.Column('inserted_count', sa.Integer(), nullable=True),
        sa.Column('updated_count', sa.Integer(), nullable=True),
        sa.Column('unchanged_count', sa.Integer(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint(
            'id', name=op.f('pk_affiliate_target_projection_push_runs')
        ),
    )
    with op.batch_alter_table(
        'affiliate_target_projection_push_runs', schema=None
    ) as batch_op:
        batch_op.create_index(
            'ix_affiliate_target_projection_push_runs_origin_created_id',
            ['runtime_origin', 'created_at', 'id'],
            unique=False,
        )
        batch_op.create_index(
            'ix_affiliate_target_projection_push_runs_status',
            ['status'],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table(
        'affiliate_target_projection_push_runs', schema=None
    ) as batch_op:
        batch_op.drop_index('ix_affiliate_target_projection_push_runs_status')
        batch_op.drop_index(
            'ix_affiliate_target_projection_push_runs_origin_created_id'
        )
    op.drop_table('affiliate_target_projection_push_runs')
