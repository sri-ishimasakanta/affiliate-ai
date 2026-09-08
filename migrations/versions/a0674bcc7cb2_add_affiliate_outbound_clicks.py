"""add affiliate outbound clicks

Revision ID: a0674bcc7cb2
Revises: 4cb3c9ea6507
Create Date: 2026-09-09 01:28:36.209405

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a0674bcc7cb2'
down_revision: str | Sequence[str] | None = '4cb3c9ea6507'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'affiliate_outbound_clicks',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('source_click_id', sa.BigInteger(), nullable=False),
        sa.Column('token', sa.String(length=64), nullable=False),
        sa.Column('clicked_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('source_import_run_id', sa.Integer(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['source_import_run_id'],
            ['affiliate_click_import_runs.id'],
            name=op.f(
                'fk_affiliate_outbound_clicks_source_import_run_id_affiliate_click_import_runs'
            ),
            ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_affiliate_outbound_clicks')),
        sa.UniqueConstraint(
            'source_click_id',
            name='uq_affiliate_outbound_clicks_source_click_id',
        ),
    )
    with op.batch_alter_table('affiliate_outbound_clicks', schema=None) as batch_op:
        batch_op.create_index(
            'ix_affiliate_outbound_clicks_source_click_id',
            ['source_click_id'],
            unique=False,
        )
        batch_op.create_index(
            'ix_affiliate_outbound_clicks_token', ['token'], unique=False
        )
        batch_op.create_index(
            'ix_affiliate_outbound_clicks_source_import_run_id',
            ['source_import_run_id'],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('affiliate_outbound_clicks', schema=None) as batch_op:
        batch_op.drop_index('ix_affiliate_outbound_clicks_source_import_run_id')
        batch_op.drop_index('ix_affiliate_outbound_clicks_token')
        batch_op.drop_index('ix_affiliate_outbound_clicks_source_click_id')
    op.drop_table('affiliate_outbound_clicks')
