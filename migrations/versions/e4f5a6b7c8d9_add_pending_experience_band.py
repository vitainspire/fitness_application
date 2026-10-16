"""add pending_experience_band to managed_relationships

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-10-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e4f5a6b7c8d9'
down_revision: Union[str, None] = 'd3e4f5a6b7c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('managed_relationships',
                   sa.Column('pending_experience_band', sa.String(length=16), nullable=True))
    op.create_check_constraint(
        'rel_pending_experience_band_known', 'managed_relationships',
        "pending_experience_band IS NULL OR "
        "pending_experience_band IN ('beginner','1m','2m','3m','experienced')")


def downgrade() -> None:
    op.drop_constraint('rel_pending_experience_band_known', 'managed_relationships', type_='check')
    op.drop_column('managed_relationships', 'pending_experience_band')
