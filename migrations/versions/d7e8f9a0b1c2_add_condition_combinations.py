"""add condition_combinations table (audited 794-combination safety table)

Revision ID: d7e8f9a0b1c2
Revises: c1d2e3f4a5b6
Create Date: 2026-11-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd7e8f9a0b1c2'
down_revision: Union[str, None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'condition_combinations',
        sa.Column('condition_set', sa.String(length=300), nullable=False),
        sa.Column('activity_type', sa.String(length=64), nullable=False),
        sa.Column('condition_count', sa.Integer(), nullable=False),
        sa.Column('verdict', sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(['activity_type'], ['activity_taxonomy.activity_type'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('condition_set', 'activity_type'),
        sa.CheckConstraint("verdict IN ('safe','modify','avoid')", name='cc_verdict_known'),
    )


def downgrade() -> None:
    op.drop_table('condition_combinations')
