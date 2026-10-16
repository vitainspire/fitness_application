"""add pending_age and pending_condition_codes to managed_relationships

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-10-14 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd0e1f2a3b4c5'
down_revision: Union[str, None] = 'c9d0e1f2a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('managed_relationships', sa.Column('pending_age', sa.Integer(), nullable=True))
    op.add_column(
        'managed_relationships',
        sa.Column('pending_condition_codes', postgresql.ARRAY(sa.String(length=64)), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('managed_relationships', 'pending_condition_codes')
    op.drop_column('managed_relationships', 'pending_age')
