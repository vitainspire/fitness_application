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
from sqlalchemy.dialects.postgresql import ARRAY, UUID
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
    # The managed person's name, given by the manager when creating this —
    # purely a display label so a manager with more than one child/student/
    # patient can tell them apart in their own roster. Never the managed
    # account's own data (they have no profile "name" field at all; this
    # lives only on the relationship, set by someone else).
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    relation_type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # Opaque, unguessable — issued with secrets.token_urlsafe, never a
    # sequential/derivable id (see app/relationships/service.py).
    invite_token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # The manager's own answers to the onboarding questions (age, conditions),
    # given on the managed person's behalf WHILE building the plan — never
    # asked of the managed person themselves (confirmed in conversation: they
    # answer this during plan setup, not at link-open time). Null until set;
    # null forever if nothing in the plan actually needs it (see
    # app/relationships/service.py's open_invite — a plan where every entry
    # is fixed never asks for this at all). Consumed exactly once, at
    # open_invite's first-ever account creation, same lifecycle as
    # PendingManagedEntry above.
    pending_age: Mapped[int | None] = mapped_column(Integer)
    pending_condition_codes: Mapped[list[str] | None] = mapped_column(ARRAY(String(64)))
    # Same lifecycle as pending_age/pending_condition_codes above — drives
    # how big a number the suggested-exercises list proposes (see
    # app/relationships/service.py's suggested_exercises), and becomes the
    # real UserProfile.experience_band at claim time instead of always
    # defaulting to beginner.
    pending_experience_band: Mapped[str | None] = mapped_column(String(16))

    __table_args__ = (
        CheckConstraint("relation_type IN ('child','student','patient','other')",
                        name="rel_type_known"),
        CheckConstraint("status IN ('pending','active','revoked')", name="rel_status_known"),
        CheckConstraint(
            "pending_experience_band IS NULL OR "
            "pending_experience_band IN ('beginner','1m','2m','3m','experienced')",
            name="rel_pending_experience_band_known"),
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

    Built-in taxonomy activities only, staged with a target and an
    auto_progress choice — see PendingCustomExercise below for the separate,
    simpler staging table a manager's free-named exercises use instead.
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
    # Set when the manager explicitly confirmed an avoid-verdict warning at
    # staging time (app/relationships/service.py's add_pending_entry) —
    # carried through to the real add_program_entry call at materialization
    # (open_invite), so a decision already made in front of the manager isn't
    # silently re-blocked (and the entry dropped with no one told) later.
    override_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("relationship_id", "activity_type", name="uq_pending_entry_activity"),
    )


class PendingCustomExercise(Base):
    """A manager's free-named exercise, staged before the invite is claimed.

    Deliberately separate from PendingManagedEntry: this is the one path with
    no safety-grid check at all — by design, it's how a manager can still
    give the managed person an exercise the grid would otherwise block for
    their staged conditions, the same way a real account's own custom
    exercises are never run through the grid either (4.4). Same shape as the
    real CustomExercise it eventually becomes (one measurement type, a fixed
    target) — see that model's own docstring for why.
    """
    __tablename__ = "pending_custom_exercises"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    relationship_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("managed_relationships.id", ondelete="CASCADE"),
        nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(64), nullable=False)
    measurement_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_sets: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    target_amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("relationship_id", "normalized_name", name="uq_pending_custom_name"),
    )
