"""remove the 18+ adults-only constraint on user_profiles

Explicit product decision (not a bug fix): the app is now open to any age,
not just adults, primarily to support managed plans
(MANAGED_PLANS_TEACHER_DOCTOR_PARENT_PLAN.md) where a parent/doctor/teacher
creates a real account for a child/patient/student of any age. Confirmed
directly: "remove age ristriction from our plan any one can use ower
application" — this is a deliberate reversal of the original "18+ only... out
of scope [for minors]" rule (app/profile/models.py's prof_adults_only), not
an oversight being corrected.

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-10-14 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, None] = 'd0e1f2a3b4c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint('prof_adults_only', 'user_profiles', type_='check')
    op.create_check_constraint('prof_age_sane', 'user_profiles', 'age >= 1')


def downgrade() -> None:
    op.drop_constraint('prof_age_sane', 'user_profiles', type_='check')
    op.create_check_constraint('prof_adults_only', 'user_profiles', 'age >= 18')
