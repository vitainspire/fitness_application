"""add user_program.switch_undo (what an entry was before a chat "Switch" put it in the plan, so Undo can put it back)

Revision ID: a8b9c0d1e2f3
Revises: d7e8f9a0b1c2
Create Date: 2026-11-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a8b9c0d1e2f3'
down_revision: Union[str, None] = 'd7e8f9a0b1c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('user_program', sa.Column('switch_undo', sa.String(length=256), nullable=True))


def downgrade() -> None:
    op.drop_column('user_program', 'switch_undo')
