"""add pending_custom_exercises table

Revision ID: b1c2d3e4f5a6
Revises: a3b4c5d6e7f8
Create Date: 2026-10-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, None] = 'a3b4c5d6e7f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'pending_custom_exercises',
        sa.Column('id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('relationship_id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('normalized_name', sa.String(length=64), nullable=False),
        sa.Column('measurement_type', sa.String(length=16), nullable=False),
        sa.Column('target_sets', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('target_amount', sa.Numeric(10, 2), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("measurement_type IN ('reps','distance','duration')",
                           name='pending_custom_measurement_known'),
        sa.CheckConstraint('target_amount > 0', name='pending_custom_target_amount_positive'),
        sa.CheckConstraint('target_sets >= 1', name='pending_custom_target_sets_positive'),
        sa.ForeignKeyConstraint(['relationship_id'], ['managed_relationships.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('relationship_id', 'normalized_name', name='uq_pending_custom_name'),
    )


def downgrade() -> None:
    op.drop_table('pending_custom_exercises')
