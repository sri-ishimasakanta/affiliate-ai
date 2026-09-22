"""add article monetization_mode

Revision ID: c58a1f3d9b04
Revises: 7cb3df2e7348
Create Date: 2026-09-22 10:00:00.000000

C2.5.8: content monetization mode を Article に **明示** で保存する。

- nullable (後方互換)。既存行は **backfill しない** -- NULL は「明示されていない legacy 行」で、
  読み取り時に app.article.monetization.resolve_effective_mode が primary link から導出する。
- server_default は置かない (既存行を書き換えないため / 新規行は application が必ず明示する)。
- 値は "affiliate" / "supporting" の 2 つ。ArticleStatus 等と同じく DB 側の ENUM は使わない。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c58a1f3d9b04'
down_revision: Union[str, Sequence[str], None] = '7cb3df2e7348'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('articles', schema=None) as batch_op:
        batch_op.add_column(sa.Column('monetization_mode', sa.String(length=20), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('articles', schema=None) as batch_op:
        batch_op.drop_column('monetization_mode')
