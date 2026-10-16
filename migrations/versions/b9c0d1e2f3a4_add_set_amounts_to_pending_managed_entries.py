"""add set_amounts to pending_managed_entries

Revision ID: b9c0d1e2f3a4
Revises: a6b7c8d9e0f1
Create Date: 2026-10-17 00:00:00.000000

Additive and nullable: existing staged rows keep working unchanged (null means
"use the uniform target_sets x target_amount", exactly as before).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b9c0d1e2f3a4'
down_revision: Union[str, None] = 'a6b7c8d9e0f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'pending_managed_entries',
        sa.Column('set_amounts', postgresql.ARRAY(sa.Numeric(10, 2)), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('pending_managed_entries', 'set_amounts')
