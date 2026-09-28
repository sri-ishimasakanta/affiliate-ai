"""allow threads growth posts without a source article

Revision ID: c4d2e8f1a9b3
Revises: afc2f36bb3ca
Create Date: 2026-09-28 11:00:00.000000

T6.3.3: アカウントを育てる投稿 (content_kind=account_growth) は記事から作らない。
``source_article_id`` を NULL にできるようにするだけ (提案と公開の 2 つの表)。
既存の行は変えない (記事から作る提案は、これまで通り記事を持つ)。外部キー・一意制約・
CHECK・索引はそのまま。記事の必須はコードで守る (印の無い NULL の提案は公開しない)。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d2e8f1a9b3'
down_revision: Union[str, Sequence[str], None] = 'afc2f36bb3ca'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ('threads_post_proposals', 'threads_publications')


def upgrade() -> None:
    """Upgrade schema."""
    for table in TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column(
                'source_article_id', existing_type=sa.Integer(), nullable=True
            )


def downgrade() -> None:
    """Downgrade schema.

    記事の無い行 (アカウントを育てる投稿) が 1 つでもあれば戻さない (消さない・作らない)。
    """
    bind = op.get_bind()
    for table in TABLES:
        rows = bind.execute(
            sa.text(f'SELECT COUNT(*) FROM {table} WHERE source_article_id IS NULL')
        ).scalar()
        if rows:
            raise RuntimeError(
                f'{table} has {rows} row(s) without a source article (account growth posts); '
                'refusing to make source_article_id NOT NULL again'
            )
    for table in TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column(
                'source_article_id', existing_type=sa.Integer(), nullable=False
            )
