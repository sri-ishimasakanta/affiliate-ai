"""add article link substitution mappings

Revision ID: 942d7351ca05
Revises: ee06dfb7a72c
Create Date: 2026-09-14 01:15:50.531231

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '942d7351ca05'
down_revision: str | Sequence[str] | None = 'ee06dfb7a72c'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'article_link_substitution_mappings',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('article_id', sa.Integer(), nullable=False),
        sa.Column('occurrence_identity_hash', sa.String(length=64), nullable=False),
        sa.Column('original_href', sa.Text(), nullable=False),
        sa.Column('affiliate_link_target_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('superseded_by_id', sa.Integer(), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('idempotency_key', sa.String(length=128), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['article_id'],
            ['articles.id'],
            name=op.f('fk_article_link_substitution_mappings_article_id_articles'),
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['affiliate_link_target_id'],
            ['affiliate_link_targets.id'],
            name=op.f(
                'fk_article_link_substitution_mappings_affiliate_link_target_id_affiliate_link_targets'
            ),
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['superseded_by_id'],
            ['article_link_substitution_mappings.id'],
            name=op.f(
                'fk_article_link_substitution_mappings_superseded_by_id_article_link_substitution_mappings'
            ),
            ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint(
            'id', name=op.f('pk_article_link_substitution_mappings')
        ),
        sa.UniqueConstraint(
            'idempotency_key',
            name=op.f('uq_article_link_substitution_mappings_idempotency_key'),
        ),
    )
    with op.batch_alter_table(
        'article_link_substitution_mappings', schema=None
    ) as batch_op:
        batch_op.create_index(
            'ix_article_link_substitution_mappings_article_id',
            ['article_id'],
            unique=False,
        )
        batch_op.create_index(
            'ix_article_link_substitution_mappings_occurrence_identity_hash',
            ['occurrence_identity_hash'],
            unique=False,
        )
        batch_op.create_index(
            'ix_article_link_substitution_mappings_affiliate_link_target_id',
            ['affiliate_link_target_id'],
            unique=False,
        )
        batch_op.create_index(
            'uq_article_link_substitution_mappings_active_occurrence',
            ['article_id', 'occurrence_identity_hash'],
            unique=True,
            sqlite_where=sa.text("status = 'active'"),
            postgresql_where=sa.text("status = 'active'"),
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table(
        'article_link_substitution_mappings', schema=None
    ) as batch_op:
        batch_op.drop_index(
            'uq_article_link_substitution_mappings_active_occurrence',
            sqlite_where=sa.text("status = 'active'"),
            postgresql_where=sa.text("status = 'active'"),
        )
        batch_op.drop_index(
            'ix_article_link_substitution_mappings_affiliate_link_target_id'
        )
        batch_op.drop_index(
            'ix_article_link_substitution_mappings_occurrence_identity_hash'
        )
        batch_op.drop_index('ix_article_link_substitution_mappings_article_id')
    op.drop_table('article_link_substitution_mappings')
