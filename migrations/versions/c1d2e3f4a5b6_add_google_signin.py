"""add google sign-in columns to users

Revision ID: c1d2e3f4a5b6
Revises: b9c0d1e2f3a4
Create Date: 2026-10-27

Both columns are additive and safe to roll back: google_sub is nullable (NULL for every existing user)
and auth_provider defaults to "password", which is what every existing account is.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, None] = 'b9c0d1e2f3a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("google_sub", sa.String(length=64), nullable=True))
    op.add_column("users", sa.Column("auth_provider", sa.String(length=16), nullable=False,
                                     server_default="password"))
    op.create_index("ix_users_google_sub", "users", ["google_sub"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_google_sub", table_name="users")
    op.drop_column("users", "auth_provider")
    op.drop_column("users", "google_sub")
