"""add article_type and article_content_subjects

Revision ID: d41b7c6e2a58
Revises: c58a1f3d9b04
Create Date: 2026-09-22 15:00:00.000000

C3:

- ``articles.article_type`` (nullable): 人が確定した記事タイプ。NULL は C3 より前の legacy 行で、
  読み取り時に keyword から推論する。**backfill しない** / server_default も置かない。
- ``article_content_subjects``: 記事が比較・調査する編集上の対象。affiliate link とは独立で、
  ``affiliate_program_id`` は nullable (catalog に案件が無い対象も持てる)。承認時点の選択を
  行として固定するため、後から config を変えても承認済み記事の比較対象は変わらない。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd41b7c6e2a58'
down_revision: Union[str, Sequence[str], None] = 'c58a1f3d9b04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('articles', schema=None) as batch_op:
        batch_op.add_column(sa.Column('article_type', sa.String(length=40), nullable=True))

    op.create_table(
        'article_content_subjects',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('article_id', sa.Integer(), nullable=False),
        sa.Column('subject_key', sa.String(length=100), nullable=False),
        sa.Column('display_name', sa.String(length=200), nullable=False),
        sa.Column('affiliate_program_id', sa.Integer(), nullable=True),
        sa.Column('subject_source', sa.String(length=32), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['affiliate_program_id'], ['affiliate_programs.id'],
            name=op.f('fk_article_content_subjects_affiliate_program_id_affiliate_programs'),
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['article_id'], ['articles.id'],
            name=op.f('fk_article_content_subjects_article_id_articles'), ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_article_content_subjects')),
        sa.UniqueConstraint(
            'article_id', 'subject_key',
            name='uq_article_content_subjects_article_subject',
        ),
        sa.UniqueConstraint(
            'article_id', 'display_name', name='uq_article_content_subjects_article_name',
        ),
    )
    with op.batch_alter_table('article_content_subjects', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_article_content_subjects_article_id'), ['article_id'], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('article_content_subjects', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_article_content_subjects_article_id'))
    op.drop_table('article_content_subjects')
    with op.batch_alter_table('articles', schema=None) as batch_op:
        batch_op.drop_column('article_type')
