"""add managed_relationships table and user_program managed-plan columns

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-10-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b8c9d0e1f2a3'
down_revision: Union[str, None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'managed_relationships',
        sa.Column('id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('manager_user_id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('managed_user_id', postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column('relation_type', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('invite_token', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['manager_user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['managed_user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('invite_token'),
        sa.CheckConstraint("relation_type IN ('child','student','patient','other')",
                           name='rel_type_known'),
        sa.CheckConstraint("status IN ('pending','active','revoked')", name='rel_status_known'),
    )
    op.create_index('ix_managed_relationships_manager', 'managed_relationships', ['manager_user_id'])
    # Partial unique index: one manager can only have ONE relationship per
    # managed user once it's claimed (pending invites, with managed_user_id
    # still null, are exempt — a manager can have many simultaneous pending
    # invites, nothing to deduplicate yet).
    op.create_index(
        'ux_managed_relationships_manager_managed', 'managed_relationships',
        ['manager_user_id', 'managed_user_id'], unique=True,
        postgresql_where=sa.text('managed_user_id IS NOT NULL'),
    )

    op.add_column(
        'user_program',
        sa.Column('managed_by_relationship_id', postgresql.UUID(as_uuid=False), nullable=True),
    )
    op.create_foreign_key(
        'fk_user_program_managed_by_relationship', 'user_program',
        'managed_relationships', ['managed_by_relationship_id'], ['id'],
        ondelete='SET NULL',
    )
    op.add_column(
        'user_program',
        sa.Column('auto_progress', sa.Boolean(), nullable=False, server_default='true'),
    )


def downgrade() -> None:
    op.drop_column('user_program', 'auto_progress')
    op.drop_constraint('fk_user_program_managed_by_relationship', 'user_program', type_='foreignkey')
    op.drop_column('user_program', 'managed_by_relationship_id')
    op.drop_index('ux_managed_relationships_manager_managed', table_name='managed_relationships')
    op.drop_index('ix_managed_relationships_manager', table_name='managed_relationships')
    op.drop_table('managed_relationships')
