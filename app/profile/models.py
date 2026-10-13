"""Module 3 (profile) + Module 4 (activity) + Module 14 (program) tables."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer,
    Numeric, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class UserProfile(Base):
    """3.1 — age (18+), goal, experience band, unit preference."""
    __tablename__ = "user_profiles"

    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    age: Mapped[int] = mapped_column(Integer, nullable=False)
    goal: Mapped[str] = mapped_column(String(32), nullable=False)
    experience_band: Mapped[str] = mapped_column(String(16), nullable=False)
    unit_preference: Mapped[str] = mapped_column(String(8), nullable=False, default="km")
    onboarded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        # 18+ only. Accepting minors brings parental consent and different
        # guidance entirely — out of scope.
        CheckConstraint("age >= 18", name="prof_adults_only"),
        CheckConstraint(
            "experience_band IN ('beginner','1m','2m','3m','experienced')",
            name="prof_band_known"),
        CheckConstraint("unit_preference IN ('km','mi')", name="prof_unit_known"),
    )


class UserCondition(Base):
    """3.2 — conditions selected from the closed list. Never free text."""
    __tablename__ = "user_conditions"

    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    condition_code: Mapped[str] = mapped_column(
        String(64), ForeignKey("conditions.condition_code"), primary_key=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)


class CustomExercise(Base):
    """4.4 — a user's own free-named exercise.

    Personal (owned by exactly one user), tracking-only: never suggested,
    never programmed (13.1). Exists purely so activity_logs has something
    to point at when a user logs something outside the reviewed taxonomy.
    """
    __tablename__ = "custom_exercises"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # Lowercased/trimmed, so "Swimming" and "swimming " collide as one entry.
    normalized_name: Mapped[str] = mapped_column(String(64), nullable=False)
    # 1 to 3 of 'reps'/'distance'/'duration' — a user can track an exercise by
    # more than one measure at once (e.g. Swimming by both Distance and Time).
    # unit and plausible_max_total aren't stored: both are fixed per type (see
    # CUSTOM_MEASUREMENT_UNITS / CUSTOM_MEASUREMENT_DEFAULT_MAX in service.py),
    # so keeping a copy on the row would just be a second place to drift.
    measurement_types: Mapped[list[str]] = mapped_column(ARRAY(String(16)), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    # Soft delete (mirrors User.deleted_at): the row stays so past logs that
    # reference it via the FK keep displaying correctly (recent_logs joins
    # on this table by name) - it just drops out of list_custom_exercises
    # and can no longer be logged against. This is what actually lets a
    # user delete an exercise with history, instead of refusing outright.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # No DB-level uniqueness here on purpose: a name must stay free to
        # reuse once its old row is soft-deleted, and the service layer
        # already enforces "no two ACTIVE exercises share a name" itself.
        CheckConstraint(
            "measurement_types <@ ARRAY['reps','distance','duration']::varchar[] "
            "AND array_length(measurement_types, 1) BETWEEN 1 AND 3",
            name="custom_measurement_known"),
    )


class ActivityLog(Base):
    """4.1 — one entry point for manual and voice input alike."""
    __tablename__ = "activity_logs"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # Exactly one of these two is set (see log_exactly_one_source below).
    # Official taxonomy activity:
    activity_type: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("activity_taxonomy.activity_type"), nullable=True)
    # 4.4 — or the user's own custom exercise. No ON DELETE CASCADE here on
    # purpose: deleting a custom exercise must never silently erase the
    # history logged against it (see delete_custom_exercise in service.py,
    # which refuses the delete instead while logs still reference it).
    custom_exercise_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("custom_exercises.id"), nullable=True)

    # What was actually performed, in the shape it was prescribed.
    sets_done: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    amount_per_set: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    # sets x amount x sides. Adherence, the implausibility ceiling and the
    # soreness reduction all need ONE comparable number whatever the shape.
    total_volume: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    logged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    local_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")

    # 4.6 — kept, flagged, and excluded from progression. Never silently dropped:
    # rejecting what a user entered is worse than storing it with a marker.
    implausible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Set alongside `implausible` for a distance/duration activity (walk, run,
    # cycle, swim — never a reps/sets exercise) whose size earned it a spot in
    # the user's own top-5 PersonalBest list for that activity (see
    # log_activity). Distinguishes "probably a genuine extraordinary effort"
    # from "probably a typo or sensor glitch": streak() and suggestions_today()
    # count an extraordinary day as a real active day even though the log is
    # still `implausible`; the progression engine still excludes it from
    # weekly target math exactly as it excludes any other implausible log,
    # since one outlier effort should not permanently inflate future targets.
    extraordinary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Idempotency for offline replay (12.5). A retry must not double-log.
    client_entry_id: Mapped[str | None] = mapped_column(String(64))

    # Which guided Workout Player session (if any) this log was made during.
    # Null for every log made outside the player (the manual dashboard flow,
    # unchanged) — this is purely a grouping tag on top of the log that
    # already fully describes what happened, not a second record of it.
    # ON DELETE SET NULL: a session record disappearing (it never actually
    # gets deleted today, but if it ever did) must not take real logged
    # history down with it.
    workout_session_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("workout_sessions.id", ondelete="SET NULL"))

    # Per-set breakdown (e.g. [10, 8, 6] for 3 sets that dropped off with
    # fatigue) — null for any log that didn't provide one, which is every log
    # ever written before this column existed, plus distance/duration logs
    # (already a single effort, nothing to break down) and any client that
    # just submits a uniform amount. When present, sets_done/amount_per_set/
    # total_volume are computed FROM this array (see log_activity) rather
    # than from a client-supplied uniform amount, so those three columns stay
    # meaningful summaries even though the detail here is richer — nothing
    # that reads only those three columns (progression, adherence, streak)
    # needs to know this column exists at all.
    set_amounts: Mapped[list[float] | None] = mapped_column(ARRAY(Numeric(10, 2)))

    __table_args__ = (
        UniqueConstraint("user_id", "client_entry_id", name="uq_log_client_entry"),
        Index("ix_activity_logs_user_date", "user_id", "local_date"),
        Index("ix_activity_logs_workout_session", "workout_session_id"),
        CheckConstraint("source IN ('manual','voice')", name="log_source_known"),
        CheckConstraint("amount_per_set > 0", name="log_amount_positive"),
        CheckConstraint("sets_done >= 1", name="log_sets_positive"),
        CheckConstraint("num_nonnulls(activity_type, custom_exercise_id) = 1",
                        name="log_exactly_one_source"),
        CheckConstraint("NOT extraordinary OR implausible",
                        name="log_extraordinary_implies_implausible"),
    )


class PersonalBest(Base):
    """A user's own top-5 all-time results for a distance/duration activity
    (walk, run, cycle, swim — never a reps/sets exercise). Populated only when
    log_activity() flags a log both `implausible` and `extraordinary` (see
    ActivityLog.extraordinary) — this table is purely a celebratory record,
    never consulted by streak, progression, or adherence math.
    """
    __tablename__ = "personal_bests"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    activity_type: Mapped[str] = mapped_column(
        String(64), ForeignKey("activity_taxonomy.activity_type"), nullable=False)
    value: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    achieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    # SET NULL, not CASCADE: this record of "you once did X" should outlive
    # the source log if the log is ever removed (no delete path exists today,
    # but nothing here should depend on that staying true).
    source_log_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("activity_logs.id", ondelete="SET NULL"))

    __table_args__ = (
        Index("ix_personal_bests_user_activity", "user_id", "activity_type"),
    )


class UserProgram(Base):
    """14.1 — the persistent program.

    One entry per activity per user, enforced by the primary key. Without that a
    logged activity has no unambiguous target to score adherence against.
    """
    __tablename__ = "user_program"

    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    activity_type: Mapped[str] = mapped_column(
        String(64), ForeignKey("activity_taxonomy.activity_type"), primary_key=True)

    target_sets: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    target_amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    # RLM design doc — the weekly curve's real, continuous tracking value,
    # separate from target_amount above (which is always snapped to a real
    # prescribable number — whole reps, whole-ish seconds). Found live: a
    # rep-based exercise's real step (e.g. 2 reps) is often bigger than a
    # single week's growth (~10%), so rounding target_amount itself back down
    # every week erased the small gain permanently and could stall forever at
    # 100% adherence. This column keeps growing by the real percentage even
    # while target_amount stays flat, so partial progress accumulates instead
    # of resetting — see app/progression/service.py's _volume_bounds. NULL
    # until the weekly job or an escalation first touches this entry; falls
    # back to target_amount until then.
    internal_target_amount: Mapped[float | None] = mapped_column(Numeric(12, 6))
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="template")
    suspended_reason: Mapped[str | None] = mapped_column(String(256))
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    # --- Per-set independent progression (PER_SET_PROGRESSION_PLAN.md) -----
    # All four null together means "still the old single-blended-target
    # system" — fully backward compatible, the default for every entry that
    # exists today and every non-reps activity going forward.
    # reps_ceiling IS NULL is the one flag app/progression/service.py checks
    # to decide which system an entry uses; the other three are only ever
    # populated once this is.
    reps_ceiling: Mapped[float | None] = mapped_column(Numeric(6, 2))
    # Current per-set DISPLAYED targets, one entry per active set, in order
    # (e.g. [24, 17, 15] for 3 sets) — target_sets/target_amount stay in sync
    # with this (target_sets = len(set_targets), target_amount = its average)
    # so any code that only knows the old columns still sees a sane value.
    set_targets: Mapped[list[float] | None] = mapped_column(ARRAY(Numeric(10, 2)))
    # Full-precision per-set tracking values — the per-set equivalent of
    # internal_target_amount above, same reason: small weekly gains must
    # accumulate against the real value, not the rounded/displayed one.
    internal_set_targets: Mapped[list[float] | None] = mapped_column(ARRAY(Numeric(12, 6)))
    # Set the moment the last set reaches reps_ceiling at REPS_STANDARD_CEILING
    # and the ceiling is bumped to REPS_HARD_CEILING — purely informational,
    # for the frontend's one-time "you've reached Pro level" message. Nothing
    # in the calculation itself branches on this; the very next week's normal
    # run already reflects real performance against the new ceiling, which is
    # what makes that week the "test week" without needing a special formula.
    pro_leveled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- Managed plans (MANAGED_PLANS_TEACHER_DOCTOR_PARENT_PLAN.md) -------
    # Null for every entry today and for anything a user adds to their own
    # plan themselves — set only when a manager (teacher/doctor/parent)
    # created or is responsible for this specific entry. This is both what a
    # manager's roster view filters on and what authorizes them to edit it.
    managed_by_relationship_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("managed_relationships.id", ondelete="SET NULL"))
    # Independent of `source` on purpose — `source='user_override'` already
    # means something different and unrelated (a safety-grid avoid-verdict
    # override, see add_program_entry). This is the new, clean "does the
    # weekly engine touch this" flag; defaults true so every existing row and
    # every self-built entry keeps today's behavior unchanged.
    auto_progress: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        CheckConstraint("status IN ('active','suspended')", name="prog_status_known"),
        CheckConstraint("source IN ('template','agent','manual','user_override')",
                        name="prog_source_known"),
        CheckConstraint("target_amount > 0", name="prog_amount_positive"),
        CheckConstraint("target_sets >= 1", name="prog_sets_positive"),
        CheckConstraint("reps_ceiling IS NULL OR reps_ceiling > 0",
                        name="prog_reps_ceiling_positive"),
    )


class OverrideConsent(Base):
    """14.7 — the record of every 'my doctor advised this' override.

    Answers "what was this user told, and when?" after the fact. Not the app's
    protection — the reviewed safety grid is that. This is supporting evidence
    that the warning was actually shown, worded exactly as shown, at the time
    of the decision.

    On account deletion (1.10), `user_id` is nulled and the row otherwise kept:
    proof the warning system worked, without holding health data tied to an
    identifiable person once they're gone.
    """
    __tablename__ = "override_consents"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"))
    condition_code: Mapped[str] = mapped_column(
        String(64), ForeignKey("conditions.condition_code"), nullable=False)
    # Set only when this override was triggered by a condition PAIR (the
    # combined-conditions interaction check, MULTI_CONDITION_INTERACTION_SAFETY_PLAN.md)
    # rather than a single condition — NULL keeps the ordinary single-condition
    # meaning unchanged. condition_code holds condition_a and this holds
    # condition_b in that case (both names end up in warning_text either way).
    condition_code_2: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("conditions.condition_code"))
    activity_type: Mapped[str] = mapped_column(
        String(64), ForeignKey("activity_taxonomy.activity_type"), nullable=False)
    grid_verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    # No global grid version counter exists yet (14.8 isn't built either) — the
    # safety cell's own reviewed_at is the closest honest stand-in for "which
    # version of the guidance this was."
    grid_reviewed_at: Mapped[date | None] = mapped_column(Date)
    warning_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("grid_verdict IN ('safe','modify','avoid')",
                        name="oc_verdict_known"),
    )


class WorkoutSession(Base):
    """One guided pass through the Workout Player, start to finish.

    Separate from ActivityLog (which records each individual exercise): this
    is what actually answers "how long did Tuesday's session take" and "how
    many of the planned exercises did they finish" — neither is reliably
    reconstructable from log timestamps alone, since a user can pause,
    background the tab, or run two short sessions in the same day.

    started_at/ended_at are the server's own clock, set in application code
    (app.profile.service), never trusted from a client device — the whole
    reason this table exists instead of a client-reported duration field.
    """
    __tablename__ = "workout_sessions"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # Snapshot of how many exercises the player was showing when the session
    # started — frozen at start time, not recomputed live, so a program
    # change mid-session (e.g. a chat-confirmed condition update) can't
    # retroactively change what this session was "for".
    planned_activity_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # Incremented in the same transaction as each real ActivityLog insert
    # tagged with this session (see log_activity) — never computed by
    # counting logs separately, so it can't drift from what was actually
    # logged.
    completed_activity_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        CheckConstraint("status IN ('active','completed','abandoned')",
                        name="ws_status_known"),
        CheckConstraint("planned_activity_count >= 0", name="ws_planned_nonneg"),
        CheckConstraint("completed_activity_count >= 0", name="ws_completed_nonneg"),
        Index("ix_workout_sessions_user_started", "user_id", "started_at"),
        # At most one 'active' session per user, enforced by the database
        # itself rather than a check-then-insert in application code — a
        # double-tapped "Start Workout" or two open tabs must never be able
        # to create two concurrently active sessions for the same user.
        Index("ix_workout_sessions_one_active", "user_id", unique=True,
              postgresql_where=text("status = 'active'")),
    )
