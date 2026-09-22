"""add article_reference_facts

Revision ID: 27655047edee
Revises: bb5385457b29
Create Date: 2026-09-22 17:54:23.661140



C4.6:

- ``article_reference_facts``: 記事レベルの参照文献エビデンス (append-only)。
  ガイドライン・標準・官公庁の公表資料のように、**製品ではない一次情報** を根拠に
  書く記事のための土台。比較対象 (ArticleContentSubject) にも affiliate 案件にも
  依存しない。
- ``source_id`` は NOT NULL / FK RESTRICT。出典の無いエビデンスは保存できず、
  参照されている限り Source は削除できない。取得日時は Source.checked_at が持つ。
- ``(article_id, reference_key, statement_hash)`` の UNIQUE が同一内容の二重登録を
  DB 層で防ぐ。同じ key の内容変更は新しい行の append で表し、読み出しは key ごとの
  最新行を採用する (ArticleFact と同じ "latest wins")。
- 既存行の backfill はしない。製品 fact を持つ既存記事の fact pack / snapshot は
  この table が空のままで従来どおり動く。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '27655047edee'
down_revision: Union[str, Sequence[str], None] = 'bb5385457b29'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('article_reference_facts',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('article_id', sa.Integer(), nullable=False),
    sa.Column('source_id', sa.Integer(), nullable=False),
    sa.Column('reference_key', sa.String(length=80), nullable=False),
    sa.Column('statement', sa.Text(), nullable=False),
    sa.Column('section_label', sa.String(length=200), nullable=True),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('statement_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['article_id'], ['articles.id'], name=op.f('fk_article_reference_facts_article_id_articles'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], name=op.f('fk_article_reference_facts_source_id_sources'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_article_reference_facts')),
    sa.UniqueConstraint('article_id', 'reference_key', 'statement_hash', name='uq_article_reference_facts_article_key_statement')
    )
    with op.batch_alter_table('article_reference_facts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_article_reference_facts_article_id'), ['article_id'], unique=False)
        batch_op.create_index('ix_article_reference_facts_article_position_id', ['article_id', 'position', 'id'], unique=False)
        batch_op.create_index(batch_op.f('ix_article_reference_facts_source_id'), ['source_id'], unique=False)



def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('article_reference_facts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_article_reference_facts_source_id'))
        batch_op.drop_index('ix_article_reference_facts_article_position_id')
        batch_op.drop_index(batch_op.f('ix_article_reference_facts_article_id'))

    op.drop_table('article_reference_facts')
