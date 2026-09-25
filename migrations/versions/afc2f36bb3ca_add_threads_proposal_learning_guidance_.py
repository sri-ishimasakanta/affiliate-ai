"""add threads proposal learning guidance provenance

Revision ID: afc2f36bb3ca
Revises: 33d93394f342
Create Date: 2026-09-25 14:10:38.717146

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'afc2f36bb3ca'
down_revision: Union[str, Sequence[str], None] = '33d93394f342'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1 列だけ。nullable で既定値なし: 既存の行は NULL (= T5.5 より前の提案) のまま。
    with op.batch_alter_table('threads_post_proposals', schema=None) as batch_op:
        batch_op.add_column(sa.Column('learning_guidance_json', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('threads_post_proposals', schema=None) as batch_op:
        batch_op.drop_column('learning_guidance_json')
