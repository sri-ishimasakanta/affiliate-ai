"""add affiliate click import runs

Revision ID: 4cb3c9ea6507
Revises: 821856f0c58d
Create Date: 2026-09-09 01:28:35.400938

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '4cb3c9ea6507'
down_revision: str | Sequence[str] | None = '821856f0c58d'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'affiliate_click_import_runs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('requested_since_id', sa.BigInteger(), nullable=False),
        sa.Column('requested_limit', sa.Integer(), nullable=False),
        sa.Column('http_status', sa.Integer(), nullable=True),
        sa.Column('server_code', sa.String(length=64), nullable=True),
        sa.Column('response_count', sa.Integer(), nullable=True),
        sa.Column('response_next_since_id', sa.BigInteger(), nullable=True),
        sa.Column('inserted_count', sa.Integer(), nullable=True),
        sa.Column('duplicate_count', sa.Integer(), nullable=True),
        sa.Column('unresolved_token_count', sa.Integer(), nullable=True),
        sa.Column('first_source_click_id', sa.BigInteger(), nullable=True),
        sa.Column('last_source_click_id', sa.BigInteger(), nullable=True),
        sa.Column('has_more', sa.Boolean(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('response_snapshot', sa.JSON(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_affiliate_click_import_runs')),
    )
    with op.batch_alter_table('affiliate_click_import_runs', schema=None) as batch_op:
        batch_op.create_index(
            'ix_affiliate_click_import_runs_status', ['status'], unique=False
        )
        batch_op.create_index(
            'ix_affiliate_click_import_runs_created_id',
            ['created_at', 'id'],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('affiliate_click_import_runs', schema=None) as batch_op:
        batch_op.drop_index('ix_affiliate_click_import_runs_created_id')
        batch_op.drop_index('ix_affiliate_click_import_runs_status')
    op.drop_table('affiliate_click_import_runs')
