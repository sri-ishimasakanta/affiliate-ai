"""add article_editorial_revisions

Revision ID: bb5385457b29
Revises: d41b7c6e2a58
Create Date: 2026-09-22 16:22:52.756052

C4.5:

- ``article_editorial_revisions``: promote 済み Article 本文の改訂履歴 (append-only)。
  ``ArticleDraftPromotion`` の first-promotion semantics は変更しない。改訂は常に
  この table へ append され、``previous_*_hash`` で改訂直前の canonical 値を残すため
  promotion から現在までの chain を後から辿れる。
- ``base_promotion_id`` は FK RESTRICT。改訂が参照している限り promotion 行は消えず、
  Article 削除時は Article 側の cascade で revision -> promotion の順に消える。
- ``(article_id, revision_content_hash)`` の UNIQUE が同一内容の二重適用を DB 層で防ぐ。
- 既存行の backfill はしない (改訂されていない記事は revision を 1 行も持たない)。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bb5385457b29'
down_revision: Union[str, Sequence[str], None] = 'd41b7c6e2a58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('article_editorial_revisions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('article_id', sa.Integer(), nullable=False),
    sa.Column('base_promotion_id', sa.Integer(), nullable=False),
    sa.Column('revision_reason', sa.Text(), nullable=False),
    sa.Column('article_status_at_revision', sa.String(length=20), nullable=False),
    sa.Column('published_update_intent', sa.Text(), nullable=True),
    sa.Column('previous_body_hash', sa.String(length=64), nullable=False),
    sa.Column('previous_meta_hash', sa.String(length=64), nullable=False),
    sa.Column('body_markdown', sa.Text(), nullable=False),
    sa.Column('meta_description', sa.Text(), nullable=False),
    sa.Column('body_hash', sa.String(length=64), nullable=False),
    sa.Column('meta_hash', sa.String(length=64), nullable=False),
    sa.Column('revision_content_hash', sa.String(length=64), nullable=False),
    sa.Column('validation_report', sa.JSON(), nullable=False),
    sa.Column('editor_notes', sa.JSON(), nullable=True),
    sa.Column('idempotency_key', sa.String(length=128), nullable=True),
    sa.Column('revised_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['article_id'], ['articles.id'], name=op.f('fk_article_editorial_revisions_article_id_articles'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['base_promotion_id'], ['article_draft_promotions.id'], name=op.f('fk_article_editorial_revisions_base_promotion_id_article_draft_promotions'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_article_editorial_revisions')),
    sa.UniqueConstraint('article_id', 'revision_content_hash', name='uq_article_editorial_revisions_article_content'),
    sa.UniqueConstraint('idempotency_key', name='uq_article_editorial_revisions_idempotency_key')
    )
    with op.batch_alter_table('article_editorial_revisions', schema=None) as batch_op:
        batch_op.create_index('ix_article_editorial_revisions_article_created_id', ['article_id', 'created_at', 'id'], unique=False)
        batch_op.create_index(batch_op.f('ix_article_editorial_revisions_article_id'), ['article_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_article_editorial_revisions_base_promotion_id'), ['base_promotion_id'], unique=False)



def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('article_editorial_revisions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_article_editorial_revisions_base_promotion_id'))
        batch_op.drop_index(batch_op.f('ix_article_editorial_revisions_article_id'))
        batch_op.drop_index('ix_article_editorial_revisions_article_created_id')

    op.drop_table('article_editorial_revisions')
