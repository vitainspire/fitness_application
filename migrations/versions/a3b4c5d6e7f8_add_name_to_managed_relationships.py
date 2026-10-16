"""add name to managed_relationships

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-10-14 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a3b4c5d6e7f8'
down_revision: Union[str, None] = 'f2a3b4c5d6e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable first so any pre-existing rows (created before this feature
    # existed) don't fail the ADD COLUMN — backfilled immediately, then
    # locked to NOT NULL for every row going forward.
    op.add_column('managed_relationships', sa.Column('name', sa.String(length=100), nullable=True))
    op.execute("UPDATE managed_relationships SET name = 'Unnamed' WHERE name IS NULL")
    op.alter_column('managed_relationships', 'name', nullable=False)


def downgrade() -> None:
    op.drop_column('managed_relationships', 'name')
