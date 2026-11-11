"""drop the old safety_grid and condition_interactions tables (owner rule: the audited 794-row table is the only safety source)

Revision ID: 60b1c04d7ad3
Revises: a8b9c0d1e2f3
Create Date: 2026-11-11 00:00:00.000000

The per-condition grid (safety_grid) and the pair table (condition_interactions) are no longer read anywhere. Every safety decision comes from
condition_combinations, loaded from data/condition_combinations_794_AUDITED.csv. downgrade() re-creates the two tables EMPTY (the data files
are deleted from the repo; they remain in git history).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '60b1c04d7ad3'
down_revision: Union[str, None] = 'a8b9c0d1e2f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS condition_interactions")
    op.execute("DROP TABLE IF EXISTS safety_grid")


def downgrade() -> None:
    op.create_table(
        'safety_grid',
        sa.Column('condition_code', sa.String(64), sa.ForeignKey('conditions.condition_code', ondelete='CASCADE'), primary_key=True),
        sa.Column('activity_type', sa.String(64), sa.ForeignKey('activity_taxonomy.activity_type', ondelete='CASCADE'), primary_key=True),
        sa.Column('verdict', sa.String(16), nullable=False),
        sa.Column('note', sa.Text()),
        sa.Column('reviewer', sa.String(128), nullable=False),
        sa.Column('reviewed_at', sa.Date()),
        sa.Column('usefulness_rank', sa.Integer()),
        sa.Column('therapeutic_rank', sa.Integer()),
        sa.CheckConstraint("verdict IN ('safe','modify','avoid')", name='sg_verdict_known'),
        sa.CheckConstraint('therapeutic_rank IS NULL OR therapeutic_rank BETWEEN 1 AND 5', name='sg_therapeutic_rank_range'),
    )
    op.create_index('ix_safety_grid_condition', 'safety_grid', ['condition_code'])
    op.create_table(
        'condition_interactions',
        sa.Column('condition_a', sa.String(64), sa.ForeignKey('conditions.condition_code', ondelete='CASCADE'), primary_key=True),
        sa.Column('condition_b', sa.String(64), sa.ForeignKey('conditions.condition_code', ondelete='CASCADE'), primary_key=True),
        sa.Column('activity_type', sa.String(64), sa.ForeignKey('activity_taxonomy.activity_type', ondelete='CASCADE'), primary_key=True),
        sa.Column('verdict', sa.String(16), nullable=False),
        sa.Column('note', sa.Text()),
        sa.Column('reviewer', sa.String(128), nullable=False),
        sa.Column('reviewed_at', sa.Date()),
        sa.CheckConstraint("verdict IN ('modify','avoid')", name='ci_verdict_known'),
        sa.CheckConstraint('condition_a < condition_b', name='ci_pair_canonical_order'),
    )
    op.create_index('ix_condition_interactions_a', 'condition_interactions', ['condition_a'])
    op.create_index('ix_condition_interactions_b', 'condition_interactions', ['condition_b'])
