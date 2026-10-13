"""add pending_managed_entries table

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-10-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c9d0e1f2a3b4'
down_revision: Union[str, None] = 'b8c9d0e1f2a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'pending_managed_entries',
        sa.Column('id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('relationship_id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('activity_type', sa.String(length=64), nullable=False),
        sa.Column('target_sets', sa.Integer(), nullable=False),
        sa.Column('target_amount', sa.Numeric(10, 2), nullable=False),
        sa.Column('auto_progress', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['relationship_id'], ['managed_relationships.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['activity_type'], ['activity_taxonomy.activity_type'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('relationship_id', 'activity_type', name='uq_pending_entry_activity'),
    )


def downgrade() -> None:
    op.drop_table('pending_managed_entries')
