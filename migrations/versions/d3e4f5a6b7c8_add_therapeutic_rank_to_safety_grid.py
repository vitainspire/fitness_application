"""add therapeutic_rank to safety_grid

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-10-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd3e4f5a6b7c8'
down_revision: Union[str, None] = 'c2d3e4f5a6b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('safety_grid', sa.Column('therapeutic_rank', sa.Integer(), nullable=True))
    op.create_check_constraint(
        'sg_therapeutic_rank_range', 'safety_grid',
        'therapeutic_rank IS NULL OR therapeutic_rank BETWEEN 1 AND 5')


def downgrade() -> None:
    op.drop_constraint('sg_therapeutic_rank_range', 'safety_grid', type_='check')
    op.drop_column('safety_grid', 'therapeutic_rank')
