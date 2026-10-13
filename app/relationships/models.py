"""See MANAGED_PLANS_TEACHER_DOCTOR_PARENT_PLAN.md for the full design.

No account-level "teacher/doctor/parent" role exists anywhere — this table is
the entire feature's permission boundary. Every check elsewhere is "does an
active row here link this manager to that specific managed user," never
anything about who a user generally is.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class ManagedRelationship(Base):
    """One link between a manager (whoever created it) and a managed person.

    `managed_user_id` is null until the invite link is claimed — see the
    plan doc §3.4. `relation_type` describes THIS relationship, chosen by the
    manager when they create it; it is not a property of either account and
    carries no extra privilege by itself.
    """
    __tablename__ = "managed_relationships"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    manager_user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    managed_user_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    relation_type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # Opaque, unguessable — issued with secrets.token_urlsafe, never a
    # sequential/derivable id (see app/relationships/service.py).
    invite_token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("relation_type IN ('child','student','patient','other')",
                        name="rel_type_known"),
        CheckConstraint("status IN ('pending','active','revoked')", name="rel_status_known"),
        # Partial unique index (managed_user_id IS NOT NULL) added in the
        # migration directly — SQLAlchemy's UniqueConstraint can't express a
        # WHERE clause; see the migration's op.create_index(unique=True, ...).
    )


class PendingManagedEntry(Base):
    """What a manager configures for the invite link BEFORE it's claimed —
    there's no real `user_id` to attach a UserProgram row to yet. Claiming
    the invite (ManagedRelationship.managed_user_id going from null to set,
    see app/relationships/service.py's claim_invite) materializes each of
    these into a real program entry for the newly-linked person, then
    deletes the staging rows; nothing here is ever read again afterward.

    Built-in taxonomy activities only — a custom exercise needs a real
    existing user_id to attach to and has no auto_progress concept at all,
    so pre-provisioning one here doesn't carry over the same way; a manager
    adds those after the invite is claimed instead.
    """
    __tablename__ = "pending_managed_entries"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    relationship_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("managed_relationships.id", ondelete="CASCADE"),
        nullable=False)
    activity_type: Mapped[str] = mapped_column(
        String(64), ForeignKey("activity_taxonomy.activity_type"), nullable=False)
    target_sets: Mapped[int] = mapped_column(Integer, nullable=False)
    target_amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    auto_progress: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("relationship_id", "activity_type", name="uq_pending_entry_activity"),
    )
