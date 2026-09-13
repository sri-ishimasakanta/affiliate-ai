"""add article publication artifacts

Revision ID: 45ed2fbd00c6
Revises: 942d7351ca05
Create Date: 2026-09-14 01:16:27.675332

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '45ed2fbd00c6'
down_revision: str | Sequence[str] | None = '942d7351ca05'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'article_publication_artifacts',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('article_id', sa.Integer(), nullable=False),
        sa.Column('canonical_body_hash', sa.String(length=64), nullable=False),
        sa.Column('renderer_version', sa.String(length=40), nullable=False),
        sa.Column('artifact_schema_version', sa.Integer(), nullable=False),
        sa.Column('substitution_manifest_json', sa.Text(), nullable=False),
        sa.Column('artifact_hash', sa.String(length=64), nullable=False),
        sa.Column('tracked_html', sa.Text(), nullable=False),
        sa.Column('tracked_html_hash', sa.String(length=64), nullable=False),
        sa.Column('substitution_count', sa.Integer(), nullable=False),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('approved_artifact_hash', sa.String(length=64), nullable=True),
        sa.Column('generated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['article_id'],
            ['articles.id'],
            name=op.f('fk_article_publication_artifacts_article_id_articles'),
            ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_article_publication_artifacts')),
        sa.UniqueConstraint(
            'artifact_hash', name='uq_article_publication_artifacts_hash'
        ),
    )
    with op.batch_alter_table('article_publication_artifacts', schema=None) as batch_op:
        batch_op.create_index(
            'ix_article_publication_artifacts_article_id',
            ['article_id'],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('article_publication_artifacts', schema=None) as batch_op:
        batch_op.drop_index('ix_article_publication_artifacts_article_id')
    op.drop_table('article_publication_artifacts')
