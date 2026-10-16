"""add soreness reports, user_program soreness reduction, and water intake logs

Revision ID: a6b7c8d9e0f1
Revises: f5a6b7c8d9e0
Create Date: 2026-10-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a6b7c8d9e0f1'
down_revision: Union[str, None] = 'f5a6b7c8d9e0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Soreness reports table & index
    op.create_table(
        'soreness_reports',
        sa.Column('id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('activity_type', sa.String(length=64), nullable=False),
        sa.Column('reported_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['activity_type'], ['activity_taxonomy.activity_type'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_soreness_reports_user_activity', 'soreness_reports', ['user_id', 'activity_type', 'reported_at'],
    )

    # 2. User program soreness columns
    op.add_column(
        'user_program',
        sa.Column('soreness_reduced_since', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'user_program',
        sa.Column('pre_soreness_target_amount', sa.Numeric(precision=10, scale=2), nullable=True),
    )
    op.add_column(
        'user_program',
        sa.Column('pre_soreness_internal_target_amount', sa.Numeric(precision=12, scale=6), nullable=True),
    )

    # 3. Progression history check constraint
    op.drop_constraint('ph_reason_known', 'progression_history', type_='check')
    op.create_check_constraint(
        'ph_reason_known',
        'progression_history',
        "reason IN ('curve','hold','reduce','floor','escalation','double_progression',"
        "'set_created','set_boost','pro_level','soreness_reduce','soreness_restore')",
    )

    # 4. Water intake logs table
    op.create_table(
        'water_intake_logs',
        sa.Column('user_id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('local_date', sa.Date(), nullable=False),
        sa.Column('glasses_count', sa.Integer(), server_default='0', nullable=False),
        sa.CheckConstraint('glasses_count >= 0', name='water_glasses_nonneg'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id', 'local_date'),
    )


def downgrade() -> None:
    # 4. Water intake logs table
    op.drop_table('water_intake_logs')

    # 3. Progression history check constraint
    op.drop_constraint('ph_reason_known', 'progression_history', type_='check')
    op.create_check_constraint(
        'ph_reason_known',
        'progression_history',
        "reason IN ('curve','hold','reduce','floor','escalation','double_progression',"
        "'set_created','set_boost','pro_level')",
    )

    # 2. User program soreness columns
    op.drop_column('user_program', 'pre_soreness_internal_target_amount')
    op.drop_column('user_program', 'pre_soreness_target_amount')
    op.drop_column('user_program', 'soreness_reduced_since')

    # 1. Soreness reports table & index
    op.drop_index('ix_soreness_reports_user_activity', table_name='soreness_reports')
    op.drop_table('soreness_reports')
