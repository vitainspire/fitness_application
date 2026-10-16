"""add gps as an allowed activity_logs source

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-10-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f5a6b7c8d9e0'
down_revision: Union[str, None] = 'e4f5a6b7c8d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint('log_source_known', 'activity_logs', type_='check')
    op.create_check_constraint(
        'log_source_known', 'activity_logs', "source IN ('manual','voice','gps')")


def downgrade() -> None:
    op.drop_constraint('log_source_known', 'activity_logs', type_='check')
    op.create_check_constraint(
        'log_source_known', 'activity_logs', "source IN ('manual','voice')")
