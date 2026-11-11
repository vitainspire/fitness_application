"""remove the 7 conditions the audited 794-row table does not cover (owner decision: the app offers only conditions that have audited exercises)

Revision ID: 5ea1d0c63f23
Revises: 60b1c04d7ad3
Create Date: 2026-11-11 00:00:00.000000

Removed: spinal_stenosis, spondylosis, bursitis, carpal_tunnel (Wrist pain), sprain_strain, gout, pcos. Anyone who had selected one loses that
selection (their other conditions stay); override-consent rows that name a removed condition are deleted (the column is NOT NULL, so they
cannot be kept). Testing phase: no real users. downgrade() re-creates only the 7 condition rows.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '5ea1d0c63f23'
down_revision: Union[str, None] = '60b1c04d7ad3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CODES = ['spinal_stenosis', 'spondylosis', 'bursitis', 'carpal_tunnel', 'sprain_strain', 'gout', 'pcos']


def upgrade() -> None:
    conn = op.get_bind()
    params = {"codes": CODES}
    conn.execute(sa.text("DELETE FROM override_consents WHERE condition_code = ANY(:codes) OR condition_code_2 = ANY(:codes)"), params)
    conn.execute(sa.text("DELETE FROM user_conditions WHERE condition_code = ANY(:codes)"), params)
    conn.execute(sa.text(
        "UPDATE managed_relationships SET pending_condition_codes = ARRAY(SELECT c FROM unnest(pending_condition_codes) AS c WHERE NOT (c = ANY(:codes))) "
        "WHERE pending_condition_codes && CAST(:codes AS varchar[])"), params)
    conn.execute(sa.text("DELETE FROM conditions WHERE condition_code = ANY(:codes)"), params)


def downgrade() -> None:
    conn = op.get_bind()
    rows = [
        ("spinal_stenosis", "Nerve pinching in the spine", 5), ("spondylosis", "Neck or back stiffness", 6),
        ("bursitis", "Joint swelling", 8), ("carpal_tunnel", "Wrist pain", 9), ("sprain_strain", "Sprain / pulled muscle", 10),
        ("gout", "Uric acid problem", 11), ("pcos", "PCOD", 16),
    ]
    for code, name, order in rows:
        conn.execute(sa.text(
            "INSERT INTO conditions (condition_code, display_name, bucket, display_order) VALUES (:c, :n, 'supported', :o) ON CONFLICT DO NOTHING"),
            {"c": code, "n": name, "o": order})
