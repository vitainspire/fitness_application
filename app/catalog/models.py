"""Reviewed reference data: taxonomy, conditions, safety grid, videos.

These tables are loaded from CSV in `data/` and are the only place exercise and
safety decisions live. Nothing in the application invents an exercise or a verdict.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer,
    Numeric, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base

PLACEHOLDER_REVIEWER = "UNREVIEWED-PLACEHOLDER"


class ActivityTaxonomy(Base):
    """The closed list of activities, with a full prescription (REQUIREMENTS 4.0).

    Four prescription shapes, because "sets x reps" does not describe everything:

        reps      sets x repetitions        2 x 5 glute bridge
        hold      sets x seconds held       2 x 20s calf stretch (isometric/stretch)
        distance  one distance              1.5 km walk
        duration  one continuous block      15 min session

    `hold` is the shape that a simple reps/duration split gets wrong: a stretch is
    not one long duration, it is several held rounds — which is a set in every way
    that matters for prescription and rest.
    """
    __tablename__ = "activity_taxonomy"

    activity_type: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)

    prescription_type: Mapped[str] = mapped_column(String(16), nullable=False)

    # --- beginner prescription -------------------------------------------
    default_sets: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    default_amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    amount_unit: Mapped[str] = mapped_column(String(16), nullable=False)
    rest_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Unilateral work is prescribed per side. The source audit flagged this
    # repeatedly ("confirm whether the baseline is per side or total") — leaving it
    # implicit is how a 2x15s stretch silently becomes 2x30s of work.
    per_side: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # --- progression ------------------------------------------------------
    # Double progression: raise `amount` until it reaches max_amount, then add a
    # set and reset amount to the starting value. Standard for beginners, and it
    # keeps a single rule for all four shapes.
    progression_axis: Mapped[str] = mapped_column(String(16), nullable=False, default="amount")
    progression_step: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    max_sets: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)

    # Outlier ceiling on TOTAL volume for one entry (sets x amount x sides).
    plausible_max_total: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)

    # Module 15 diminishing-curve tuning constant (RLM design doc §4.1). Optional
    # per-activity override — null means "use the app-wide default" (see
    # app.progression.service.DEFAULT_CURVE_K). Not populated from CSV yet: this
    # reviewed table is physiotherapist-owned, and per-activity tuning is a later
    # decision for them to make, not one engineering makes by editing the CSV.
    curve_k: Mapped[float | None] = mapped_column(Numeric(6, 3))

    # Lets a distance activity show an honest time estimate without storing a
    # second target. Only meaningful for `distance`.
    est_pace_min_per_km: Mapped[float | None] = mapped_column(Numeric(6, 2))

    synonyms: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    source_exercise_id: Mapped[str | None] = mapped_column(String(128))

    # Which of the beginner-tier slices (REQUIREMENTS 4.0 experience bands) an
    # activity fills first. Lower = shown sooner. Not a difficulty ranking —
    # it is "how essential is this before someone has a habit yet", which is
    # why Walking/Glute Bridge/Dead Bug/Shoulder Circles rank above stretches
    # that matter but are more specific.
    priority_order: Mapped[int] = mapped_column(Integer, nullable=False)

    # Public-domain demonstration frames + steps from free-exercise-db. Two frames
    # (start/end) alternated give a usable demonstration with no video hosting and
    # no per-exercise human choice — every user sees them immediately.
    demo_images: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    instructions: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)

    # Self-hosted GIF, when a confirmed one exists (scripts/fetch_gifs.py). Served
    # from our own static/ dir, never hotlinked -- see that script's docstring for
    # why. Optional: activities without one fall back to demo_images alternation.
    demo_gif: Mapped[str | None] = mapped_column(String(256))
    demo_gif_source: Mapped[str | None] = mapped_column(String(256))

    youtube_url: Mapped[str | None] = mapped_column(String(512))

    reviewer: Mapped[str] = mapped_column(String(128), nullable=False)
    reviewed_at: Mapped[date | None] = mapped_column(Date)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint(
            "prescription_type IN ('reps','hold','distance','duration')",
            name="at_prescription_known"),
        CheckConstraint("default_sets >= 1", name="at_sets_positive"),
        CheckConstraint("default_amount > 0", name="at_amount_positive"),
        CheckConstraint("max_amount >= default_amount", name="at_max_amount_sane"),
        CheckConstraint("max_sets >= default_sets", name="at_max_sets_sane"),
        CheckConstraint("progression_step > 0", name="at_step_positive"),
        CheckConstraint("rest_seconds >= 0", name="at_rest_nonneg"),
        CheckConstraint("progression_axis IN ('amount','sets')", name="at_axis_known"),
        # The ceiling must exceed a fully progressed prescription, or a user who
        # completes the programme as designed gets flagged as implausible.
        CheckConstraint("plausible_max_total >= max_sets * max_amount",
                        name="at_ceiling_above_full_progression"),
        # Sets and rest are meaningless for a single continuous effort.
        CheckConstraint(
            "prescription_type NOT IN ('distance','duration') "
            "OR (default_sets = 1 AND max_sets = 1)",
            name="at_single_effort_has_one_set"),
        CheckConstraint("activity_type ~ '^[a-z][a-z0-9_]*$'", name="at_code_is_snake"),
        # Ties would make the tier slice (4/6/8/…) ambiguous about which
        # activity is "in" vs "the next one out".
        UniqueConstraint("priority_order", name="uq_at_priority_order"),
    )

    # --- derived helpers --------------------------------------------------

    @property
    def sides(self) -> int:
        return 2 if self.per_side else 1

    def total_volume(self, sets: int, amount: float) -> float:
        """One comparable number for progression and adherence.

        Everything downstream — adherence, the implausibility ceiling, the
        soreness reduction — needs a single figure, whatever the shape.
        """
        return float(sets) * float(amount) * self.sides

    def describe(self, sets: int, amount: float) -> str:
        """Human prescription, e.g. '2 x 15s each side' or '1.5 km'."""
        unit = self.amount_unit
        side = " each side" if self.per_side else ""
        amt = f"{amount:g}"
        if self.prescription_type == "reps":
            return f"{sets} x {amt}{side}"
        if self.prescription_type == "hold":
            return f"{sets} x {amt}s{side}"
        if self.prescription_type == "distance":
            return f"{amt} {unit}"
        return f"{amt} {unit}"


class Condition(Base):
    """The closed condition list with its buckets (REQUIREMENTS 3.2).

    `other_unlisted` is structural: the fail-closed default for anything a user
    cannot find. Removing it reopens the hole the closed list exists to close.
    """
    __tablename__ = "conditions"

    condition_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    bucket: Mapped[str] = mapped_column(String(16), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("bucket IN ('supported','refer')", name="cond_bucket_known"),
    )


class SafetyGridCell(Base):
    """One verdict per condition x activity (REQUIREMENTS 13.3)."""
    __tablename__ = "safety_grid"

    condition_code: Mapped[str] = mapped_column(
        String(64), ForeignKey("conditions.condition_code", ondelete="CASCADE"),
        primary_key=True)
    activity_type: Mapped[str] = mapped_column(
        String(64), ForeignKey("activity_taxonomy.activity_type", ondelete="CASCADE"),
        primary_key=True)

    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    reviewer: Mapped[str] = mapped_column(String(128), nullable=False)
    reviewed_at: Mapped[date | None] = mapped_column(Date)

    # Personalized "most useful first" ordering (1 = most useful) for this one
    # condition, distinct from `verdict` (safety). Only meaningful for a cell
    # a user will ever actually be shown (`safe`/`modify`) — null for `avoid`,
    # since a banned activity is never ranked for anything. A user with
    # several conditions gets the best (lowest) rank across all of them per
    # activity — see app.profile.service._usefulness_ranks.
    #
    # "avoid implies null, shown implies present" is enforced in
    # app.catalog.loader (load_safety_grid), the same way overall grid
    # coverage is (assert_full_coverage) — not a DB CheckConstraint. The
    # deploy sequence runs migrations before the loader (Procfile: `alembic
    # upgrade head && python -m app.catalog.loader`), so a DB-level
    # NOT-NULL-when-shown constraint would reject its own migration the
    # moment this column was added, before the loader ever got a chance to
    # populate real ranks — found by actually running this locally.
    usefulness_rank: Mapped[int | None] = mapped_column(Integer)

    # Separate from usefulness_rank above: "most useful to show" (relevance,
    # covers every safe/modify cell) vs. this one, "actually helps reduce the
    # condition itself" (efficacy, sparse — most activities don't meaningfully
    # treat most conditions, so most cells leave this null on purpose; e.g.
    # Wrist Circles is the ONLY ranked entry for carpal_tunnel). 1-5, 1 =
    # most effective; null for `avoid` (same reasoning as usefulness_rank)
    # and null for anything not genuinely therapeutic for this condition.
    therapeutic_rank: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        CheckConstraint("verdict IN ('safe','modify','avoid')", name="sg_verdict_known"),
        CheckConstraint("therapeutic_rank IS NULL OR therapeutic_rank BETWEEN 1 AND 5",
                        name="sg_therapeutic_rank_range"),
        Index("ix_safety_grid_condition", "condition_code"),
    )


class ConditionInteractionCell(Base):
    """One verdict per (condition pair) x activity — the combined-conditions
    safety check from APP_FEATURES_OVERVIEW.md's "designed conceptually but
    not yet built" gap. See MULTI_CONDITION_INTERACTION_SAFETY_PLAN.md.

    Deliberately NOT full-coverage like SafetyGridCell: this table only ever
    ESCALATES a verdict on top of the single-condition grid, which is still
    the fail-closed baseline underneath it. A missing row means "no extra
    risk known for this combination," never "assumed safe" — the grid's own
    per-condition check still applies regardless. That's why `verdict` here
    can never be 'safe': a 'safe' row would mean nothing (absence already
    means no extra restriction) and is rejected at load time.

    condition_a is always the lexically-smaller code (enforced by the
    CheckConstraint below AND by the loader, which swaps a reversed CSV row
    before insert) so a lookup only ever has to check sorted(pair), never
    both orders, and a reversed-order duplicate is a real constraint
    violation instead of a silent second row.
    """
    __tablename__ = "condition_interactions"

    condition_a: Mapped[str] = mapped_column(
        String(64), ForeignKey("conditions.condition_code", ondelete="CASCADE"),
        primary_key=True)
    condition_b: Mapped[str] = mapped_column(
        String(64), ForeignKey("conditions.condition_code", ondelete="CASCADE"),
        primary_key=True)
    activity_type: Mapped[str] = mapped_column(
        String(64), ForeignKey("activity_taxonomy.activity_type", ondelete="CASCADE"),
        primary_key=True)

    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    reviewer: Mapped[str] = mapped_column(String(128), nullable=False)
    reviewed_at: Mapped[date | None] = mapped_column(Date)

    __table_args__ = (
        CheckConstraint("verdict IN ('modify','avoid')", name="ci_verdict_known"),
        CheckConstraint("condition_a < condition_b", name="ci_pair_canonical_order"),
        Index("ix_condition_interactions_a", "condition_a"),
        Index("ix_condition_interactions_b", "condition_b"),
    )


class VideoCatalog(Base):
    """Curated YouTube links (Module 6). Link-only: no embeds, no live API call."""
    __tablename__ = "video_catalog"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    activity_type: Mapped[str] = mapped_column(
        String(64), ForeignKey("activity_taxonomy.activity_type", ondelete="CASCADE"),
        nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    youtube_video_id: Mapped[str] = mapped_column(String(32), nullable=False)
    watch_url: Mapped[str] = mapped_column(String(512), nullable=False)
    thumbnail_url: Mapped[str | None] = mapped_column(String(512))
    duration_label: Mapped[str | None] = mapped_column(String(16))
    curated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
