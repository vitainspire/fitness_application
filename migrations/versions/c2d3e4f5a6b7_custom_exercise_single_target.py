"""custom exercises: single measurement_type + fixed target, replacing the
multi-type tracking-only design

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-10-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c2d3e4f5a6b7'
down_revision: Union[str, None] = 'b1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('custom_exercises', sa.Column('measurement_type', sa.String(length=16), nullable=True))
    op.add_column('custom_exercises', sa.Column('target_sets', sa.Integer(), nullable=False,
                                                 server_default='1'))
    op.add_column('custom_exercises', sa.Column('target_amount', sa.Numeric(10, 2), nullable=True))

    # Backfill from the old array column: take its first element as the one
    # type kept, with a placeholder target (there's no prior target data to
    # recover — pre-dates this feature) the owner can edit afterward.
    conn = op.get_bind()
    conn.execute(sa.text("""
        UPDATE custom_exercises
        SET measurement_type = measurement_types[1],
            target_amount = CASE measurement_types[1]
                WHEN 'reps' THEN 10
                WHEN 'distance' THEN 1
                WHEN 'duration' THEN 10
                ELSE 10
            END
    """))

    op.alter_column('custom_exercises', 'measurement_type', nullable=False)
    op.alter_column('custom_exercises', 'target_amount', nullable=False)
    op.alter_column('custom_exercises', 'target_sets', server_default=None)

    op.drop_constraint('custom_measurement_known', 'custom_exercises', type_='check')
    op.drop_column('custom_exercises', 'measurement_types')

    op.create_check_constraint(
        'custom_measurement_known', 'custom_exercises',
        "measurement_type IN ('reps','distance','duration')")
    op.create_check_constraint(
        'custom_target_amount_positive', 'custom_exercises', 'target_amount > 0')
    op.create_check_constraint(
        'custom_target_sets_positive', 'custom_exercises', 'target_sets >= 1')


def downgrade() -> None:
    op.drop_constraint('custom_target_sets_positive', 'custom_exercises', type_='check')
    op.drop_constraint('custom_target_amount_positive', 'custom_exercises', type_='check')
    op.drop_constraint('custom_measurement_known', 'custom_exercises', type_='check')

    op.add_column('custom_exercises',
                   sa.Column('measurement_types', sa.ARRAY(sa.String(length=16)), nullable=True))
    conn = op.get_bind()
    conn.execute(sa.text(
        "UPDATE custom_exercises SET measurement_types = ARRAY[measurement_type]"))
    op.alter_column('custom_exercises', 'measurement_types', nullable=False)

    op.drop_column('custom_exercises', 'target_amount')
    op.drop_column('custom_exercises', 'target_sets')
    op.drop_column('custom_exercises', 'measurement_type')

    op.create_check_constraint(
        'custom_measurement_known', 'custom_exercises',
        "measurement_types <@ ARRAY['reps','distance','duration']::varchar[] "
        "AND array_length(measurement_types, 1) BETWEEN 1 AND 3")
