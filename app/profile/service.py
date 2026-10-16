"""Onboarding, activity logging, suggestions.

The rule that shapes all of this: the safety grid decides what a user is shown.
Nothing here invents an exercise, and a condition the grid cannot cover produces
no suggestions rather than unfiltered ones.
"""
from __future__ import annotations

import itertools
import json
import math
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.models import User
from app.catalog.models import (
    ActivityTaxonomy, Condition, ConditionInteractionCell, SafetyGridCell, VideoCatalog,
)
from app.platform.errors import Conflict, Forbidden, NotFound, ValidationError
from app.profile.models import (
    ActivityLog, CustomExercise, OverrideConsent, PersonalBest, SorenessReport,
    UserCondition, UserProfile, UserProgram, WaterIntakeLog, WorkoutSession,
)
from app.progression.models import ProgressionHistory
from app.relationships.models import ManagedRelationship
from app.relationships.service import is_managed_account, require_active_relationship

IST = timezone(timedelta(hours=5, minutes=30))  # single market (3.3)

REFERRAL_MESSAGE = (
    "Based on what you've told us, please speak to a doctor before starting an "
    "exercise programme. You can still log activities here, but we won't suggest "
    "exercises."
)

# Found live (code audit): only a floor (>=18) was ever checked, no ceiling.
# `age` is a 4-byte Postgres Integer (models.py); an extreme Python int
# (arbitrary precision) passed the app-level check and only failed at flush
# with an uncaught DB error instead of a clean 422. 120 is generous — no
# real user of this app is older than that — while still comfortably inside
# the Integer column's range.
MAX_AGE = 120

GOALS = {"health", "fitness", "flexibility", "habit"}
BANDS = {"beginner", "1m", "2m", "3m", "experienced"}

# Found live (code audit): log_activity only ever checked amount/sets were
# POSITIVE, with no upper bound. amount_per_set/total_volume are
# Numeric(10,2) (max ~99,999,999.99) and sets_done is a 32-bit Integer — an
# extreme client-supplied value overflowed the column at INSERT time,
# raising an uncaught database error instead of a clean 422. These are hard
# safety backstops against overflow, not a plausibility judgment — the
# "implausible" flag (compared against each activity's own reviewed
# plausible_max_total, or CUSTOM_MEASUREMENT_DEFAULT_MAX below) already
# handles "is this a believable rehab log" at a much tighter threshold; this
# just guarantees the column can physically store the value at all.
#
# Found live (second-pass code audit): bounding amount and sets separately
# was NOT enough — their PRODUCT (further doubled for per_side activities)
# can still overflow the column even when both factors are individually
# under these caps: amount=100000, sets=1000 already gives total=1e8, over
# the ~99,999,999.99 ceiling. MAX_TOTAL_VOLUME checks the actual computed
# total directly, which is the only way to guarantee the INSERT that's
# about to happen can't overflow regardless of how the two factors combine.
MAX_LOGGABLE_AMOUNT = 100_000
MAX_LOGGABLE_SETS = 10_000
MAX_TOTAL_VOLUME = 99_999_999

# Mirrors ActivityLog's own CheckConstraint("source IN ('manual','voice')").
# Kept here so an unknown value is a clean 422 at the service boundary rather
# than an IntegrityError from the database.
LOG_SOURCES = {"manual", "voice", "gps"}

# A flagged-implausible log for a distance/duration activity (never a
# reps/sets exercise — see log_activity) that ranks in the user's own top 5
# for that activity is treated as a genuine extraordinary effort rather than
# a likely typo/glitch: it's kept here for display and, unlike an ordinary
# implausible log, counts as a real day for streak()/suggestions_today().
PERSONAL_BEST_LIMIT = 5

# How many activities a tier shows, sliced from the full priority-ordered,
# already-condition-filtered list. "experienced" is None: no cap, show
# everything that's safe. Counts are shown, not "default"; the default/optional
# split is just where within that count the line falls (see _tier_slice).
TIER_ORDER = ["beginner", "1m", "2m", "3m", "experienced"]
TIER_SHOWN = {"beginner": 4, "1m": 5, "2m": 7, "3m": 9, "experienced": None}


def today_ist() -> date:
    return datetime.now(IST).date()


# --- Reference data --------------------------------------------------------

def list_conditions(session: Session) -> list[dict]:
    rows = session.scalars(
        select(Condition).order_by(Condition.display_order)
    ).all()
    return [{"code": r.condition_code, "name": r.display_name,
             "bucket": r.bucket, "notes": r.notes} for r in rows]


def list_activities(session: Session) -> list[dict]:
    rows = session.scalars(
        select(ActivityTaxonomy).order_by(ActivityTaxonomy.display_name)
    ).all()
    return [{
        "activity_type": r.activity_type, "display_name": r.display_name,
        "prescription_type": r.prescription_type,
        "default_sets": r.default_sets, "default_amount": float(r.default_amount),
        "amount_unit": r.amount_unit, "per_side": r.per_side,
        "rest_seconds": r.rest_seconds, "synonyms": r.synonyms,
    } for r in rows]


# --- Onboarding ------------------------------------------------------------

def is_referred(session: Session, user_id: str) -> bool:
    """True if any selected condition is in the refer bucket (13.3b).

    Takes precedence over everything: no suggestions, no program, no videos.
    """
    n = session.scalar(
        select(func.count())
        .select_from(UserCondition)
        .join(Condition, Condition.condition_code == UserCondition.condition_code)
        .where(UserCondition.user_id == user_id, Condition.bucket == "refer")
    )
    return bool(n)


def complete_onboarding(session: Session, user_id: str, *, age: int, goal: str,
                        experience_band: str, condition_codes: list[str],
                        unit_preference: str = "km",
                        build_default_program: bool = True,
                        min_age: int = 1) -> dict:
    """build_default_program=False: managed plans
    (MANAGED_PLANS_TEACHER_DOCTOR_PARENT_PLAN.md) — a manager-curated plan
    must consist of exactly what the manager staged, never also the generic
    tier-based default program every other new account gets. Every existing
    caller (the real /v1/profile/onboarding route, and the RAG chat's
    condition-confirmation path) keeps today's behavior unchanged.

    No 18+ floor: explicit product decision, confirmed directly ("remove age
    ristriction from our plan any one can use ower application"), reversing
    the original adults-only rule — see UserProfile's own docstring and
    migration e1f2a3b4c5d6 for what that rule was and why it existed.
    min_age is kept as a parameter (defaulting to a bare sanity floor, not a
    legal gate) in case a future caller ever needs a different one.
    """
    if not isinstance(age, int) or age < min_age or age > MAX_AGE:
        raise ValidationError(f"Age must be a whole number between {min_age} and {MAX_AGE}.")
    if goal not in GOALS:
        raise ValidationError(f"Goal must be one of: {', '.join(sorted(GOALS))}")
    if experience_band not in BANDS:
        raise ValidationError(f"Experience band must be one of: {', '.join(sorted(BANDS))}")
    # Found live (code audit): unlike update_profile, this never validated
    # unit_preference at all — an invalid value only got caught by the DB's
    # own CheckConstraint at flush, an uncaught IntegrityError (500) instead
    # of a clean 422.
    if unit_preference not in UNIT_PREFERENCES:
        raise ValidationError(f"Unit preference must be one of: {', '.join(sorted(UNIT_PREFERENCES))}")

    known = {c.condition_code for c in session.scalars(select(Condition)).all()}
    # Found live (code audit): duplicate codes (e.g. the same code sent
    # twice) were never de-duplicated, unlike create_custom_exercise's own
    # measurement_types handling — two rows with the same composite PK
    # (user_id, condition_code) raised an uncaught IntegrityError.
    codes = list(dict.fromkeys(c for c in (condition_codes or []) if c))
    unknown = [c for c in codes if c not in known]
    if unknown:
        # Free text can never reach here — the client picks from /v1/conditions.
        raise ValidationError(f"Unknown condition(s): {', '.join(unknown)}")
    if not codes:
        codes = ["none"]

    session.merge(UserProfile(
        user_id=user_id, age=age, goal=goal,
        experience_band=experience_band, unit_preference=unit_preference,
    ))
    session.execute(delete(UserCondition).where(UserCondition.user_id == user_id))
    for code in codes:
        session.add(UserCondition(user_id=user_id, condition_code=code))
    session.flush()

    if is_referred(session, user_id):
        # Deliberately no program shown while referred. Soft-suspend, never
        # delete — this is reachable mid-chat (the RAG condition-confirmation
        # flow calls complete_onboarding too), so a hard delete here meant
        # confirming a refer-bucket condition permanently destroyed every
        # program row and all earned progression, with no way back even if
        # the condition was later removed. Every other suspension path in
        # this file (condition_safety, user_removed, sport_focus_change) is
        # reversible; this one was the sole exception.
        session.execute(
            update(UserProgram)
            .where(UserProgram.user_id == user_id, UserProgram.status == "active")
            .values(status="suspended", suspended_reason="referred")
        )
        return {"referred": True, "message": REFERRAL_MESSAGE, "program": []}

    # No longer referred (or never was) — release anything held by the
    # "referred" suspension above, so it's back to a normal active row for
    # build_program's own is_blocked logic to evaluate on its own merits
    # (which may re-suspend it for a real, current safety reason — that's
    # condition_safety, a separate and already-reversible path).
    session.execute(
        update(UserProgram)
        .where(UserProgram.user_id == user_id, UserProgram.suspended_reason == "referred")
        .values(status="active", suspended_reason=None)
    )
    program = build_program(session, user_id) if build_default_program else []
    return {"referred": False, "message": None, "program": program}


UNIT_PREFERENCES = {"km", "mi"}


def update_profile(session: Session, user_id: str, *, fields: dict) -> dict:
    """3.4 — edit one or more simple fields without touching conditions or the
    program. Only age / goal / experience_band / unit_preference are settable
    here; anything else in `fields` is ignored rather than silently accepted.
    """
    prof = session.get(UserProfile, user_id)
    if prof is None:
        raise NotFound("Complete onboarding before editing your profile.")

    if "age" in fields:
        age = fields["age"]
        # No 18+ floor — see UserProfile's own docstring and
        # complete_onboarding's: this was a deliberate product reversal, not
        # a bug fix.
        if not isinstance(age, int) or age < 1 or age > MAX_AGE:
            raise ValidationError(f"Age must be a whole number between 1 and {MAX_AGE}.")
        prof.age = age

    if "goal" in fields:
        goal = fields["goal"]
        if goal not in GOALS:
            raise ValidationError(f"Goal must be one of: {', '.join(sorted(GOALS))}")
        prof.goal = goal

    if "experience_band" in fields:
        band = fields["experience_band"]
        if band not in BANDS:
            raise ValidationError(f"Experience band must be one of: {', '.join(sorted(BANDS))}")
        prof.experience_band = band

    if "unit_preference" in fields:
        unit = fields["unit_preference"]
        if unit not in UNIT_PREFERENCES:
            raise ValidationError(f"Unit preference must be one of: {', '.join(sorted(UNIT_PREFERENCES))}")
        prof.unit_preference = unit

    session.flush()
    return profile_summary(session, user_id)


# --- Program ---------------------------------------------------------------

_VERDICT_RANK = {"safe": 0, "modify": 1, "avoid": 2}


def _interaction_verdicts(session: Session, user_id: str) -> dict[str, dict]:
    """Combined-conditions safety check (MULTI_CONDITION_INTERACTION_SAFETY_PLAN.md):
    activity_type -> the worst interaction verdict across every PAIR of this
    user's own conditions, for activities where some reviewed pair+activity
    row exists. Empty for a user with 0 or 1 conditions (no pair possible).

    Deliberately separate from _blocked_activities/_modified_activities'
    single-condition query — this table is sparse (no coverage requirement,
    see ConditionInteractionCell's docstring), so it can never be the sole
    source of a verdict, only an escalation folded into those two functions
    and into _worst_verdict below.
    """
    codes = sorted(session.scalars(
        select(UserCondition.condition_code).where(UserCondition.user_id == user_id)
    ).all())
    if len(codes) < 2:
        return {}
    pairs = set(itertools.combinations(codes, 2))  # already sorted -> canonical order
    # Both columns individually IN the user's codes is a correct, sufficient
    # filter: since condition_a < condition_b is enforced at write time, a row
    # passing this is guaranteed to BE one of `pairs`, not just touch one.
    rows = session.execute(
        select(ConditionInteractionCell)
        .where(ConditionInteractionCell.condition_a.in_(codes),
               ConditionInteractionCell.condition_b.in_(codes))
    ).scalars().all()
    if not rows:
        return {}
    names = dict(session.execute(
        select(Condition.condition_code, Condition.display_name)
        .where(Condition.condition_code.in_(codes))
    ).all())

    out: dict[str, dict] = {}
    for cell in rows:
        assert (cell.condition_a, cell.condition_b) in pairs  # see filter comment above
        current = out.get(cell.activity_type)
        if current is not None and _VERDICT_RANK[current["verdict"]] >= _VERDICT_RANK[cell.verdict]:
            continue
        out[cell.activity_type] = {
            "verdict": cell.verdict,
            "note": cell.note,
            "reviewed_at": cell.reviewed_at,
            "condition_a_code": cell.condition_a,
            "condition_a_name": names.get(cell.condition_a, cell.condition_a),
            "condition_b_code": cell.condition_b,
            "condition_b_name": names.get(cell.condition_b, cell.condition_b),
        }
    return out


def _blocked_activities(session: Session, user_id: str) -> dict[str, str]:
    """activity_type -> note, for anything the grid marks `avoid` — either a
    single condition on its own, or two of the user's conditions together
    (see _interaction_verdicts)."""
    rows = session.execute(
        select(SafetyGridCell.activity_type, SafetyGridCell.note)
        .join(UserCondition,
              UserCondition.condition_code == SafetyGridCell.condition_code)
        .where(UserCondition.user_id == user_id, SafetyGridCell.verdict == "avoid")
    ).all()
    out = {a: n for a, n in rows}
    for activity_type, interaction in _interaction_verdicts(session, user_id).items():
        if interaction["verdict"] == "avoid":
            out.setdefault(activity_type, interaction["note"])
    return out


def _modified_activities(session: Session, user_id: str) -> dict[str, str]:
    rows = session.execute(
        select(SafetyGridCell.activity_type, SafetyGridCell.note)
        .join(UserCondition,
              UserCondition.condition_code == SafetyGridCell.condition_code)
        .where(UserCondition.user_id == user_id, SafetyGridCell.verdict == "modify")
    ).all()
    out = {a: n for a, n in rows}
    blocked = _blocked_activities(session, user_id)
    for activity_type, interaction in _interaction_verdicts(session, user_id).items():
        # avoid still wins over modify — same precedence _VERDICT_RANK already
        # gives these two within a single condition's own cells.
        if interaction["verdict"] == "modify" and activity_type not in blocked:
            out[activity_type] = interaction["note"]
    return out


def _usefulness_ranks(session: Session, user_id: str) -> dict[str, int]:
    """activity_type -> best (lowest/most useful) rank across the user's own
    conditions, from the reviewed safety_grid.usefulness_rank column.

    A user can have several conditions at once, each with its own "most
    useful first" ordering for the SAME activity — e.g. Glute Bridge might be
    rank 2 for gout but rank 6 for arthritis_knee. Taking the MIN means an
    activity that's a top pick for ANY of the user's conditions surfaces
    early, rather than getting buried by a condition it's merely fine for.
    `avoid` cells never carry a rank (enforced at load time) and are excluded
    here — an activity a user cannot be shown at all must never win a "most
    useful" comparison against one they can.
    """
    rows = session.execute(
        select(SafetyGridCell.activity_type, func.min(SafetyGridCell.usefulness_rank))
        .join(UserCondition,
              UserCondition.condition_code == SafetyGridCell.condition_code)
        .where(UserCondition.user_id == user_id,
               SafetyGridCell.verdict != "avoid",
               SafetyGridCell.usefulness_rank.is_not(None))
        .group_by(SafetyGridCell.activity_type)
    ).all()
    return dict(rows)


def build_program(session: Session, user_id: str) -> list[dict]:
    """Seed the program from the taxonomy baselines, minus anything `avoid` —
    and, for a user who already has a program, RECONCILE against the current
    condition set rather than blindly wiping it.

    Found live (this session, traced from a user question about chat-confirmed
    conditions): the old version unconditionally deleted every UserProgram row
    and recreated all of them from taxonomy DEFAULTS, every single time this
    ran — including every re-run from complete_onboarding, which fires not
    just at first signup but also from the profile editor and from
    app.rag.service._add_condition (a user casually confirming an unrelated
    symptom mid-conversation). That meant weeks of real, earned progression on
    every OTHER exercise silently reset to day-one numbers any time a
    condition changed, even one wholly unrelated to that exercise. Reproduced
    live: glute_bridge progressed from a 5.00 baseline to 6.00 via
    apply_escalation, then confirming an unrelated condition (sprain_strain)
    reset it straight back to 5.00.

    New behavior: an activity with NO existing row is seeded fresh from the
    taxonomy baseline exactly as before (this is what makes first-time
    onboarding — where nothing exists yet — behave identically to the old
    code). An activity that already has a row is left completely untouched —
    same target_sets/target_amount, same status — UNLESS the current
    condition set just made it unsafe (`avoid`), in which case it's
    soft-suspended (the same convention remove_program_entry already uses),
    never deleted, so its history and progress are preserved rather than lost
    outright. A `user_override` entry (the user explicitly confirmed they
    want it despite a KNOWN risk, see add_program_entry) is left active even
    if a newly added condition also flags it — that's a deliberate, already-
    informed choice, not something a background condition change should
    silently reverse.

    Found live (fourth-pass audit): the block direction alone left a real
    gap — nothing ever reversed it. Remove the condition that made an entry
    unsafe, and it stayed suspended forever with no automatic path back,
    even though the reason it was suspended no longer applies. Now also
    resurrects: an entry suspended_reason="condition_safety" (a SYSTEM
    suspension, never a user's own remove_program_entry call — that reason
    is "user_removed" and must stay untouched here, see get_program's own
    comment on why a manual removal must never be silently backfilled) whose
    activity is no longer in the current blocked set goes back to active,
    with whatever target_sets/target_amount it already had — so progress
    earned before the suspension comes back too, not reset to baseline.
    """
    blocked = _blocked_activities(session, user_id)
    existing = {
        row.activity_type: row
        for row in session.scalars(
            select(UserProgram).where(UserProgram.user_id == user_id)
        ).all()
    }

    for row in session.scalars(select(ActivityTaxonomy)).all():
        entry = existing.get(row.activity_type)
        is_blocked = row.activity_type in blocked

        if entry is None:
            if not is_blocked:
                session.add(UserProgram(
                    user_id=user_id,
                    activity_type=row.activity_type,
                    target_sets=row.default_sets,
                    target_amount=row.default_amount,
                    unit=row.amount_unit,
                    source="template",
                ))
            continue

        if is_blocked and entry.status == "active" and entry.source != "user_override":
            entry.status = "suspended"
            entry.suspended_reason = "condition_safety"
        elif (
            not is_blocked
            and entry.status == "suspended"
            and entry.suspended_reason == "condition_safety"
        ):
            entry.status = "active"
            entry.suspended_reason = None

    session.flush()
    return get_program(session, user_id)


def get_program(session: Session, user_id: str, *, include_suspended: bool = False) -> list[dict]:
    if is_referred(session, user_id):
        return []
    modified = _modified_activities(session, user_id)
    ranks = _usefulness_ranks(session, user_id)
    query = (
        select(UserProgram, ActivityTaxonomy)
        .join(ActivityTaxonomy,
              ActivityTaxonomy.activity_type == UserProgram.activity_type)
        .where(UserProgram.user_id == user_id)
    )
    if not include_suspended:
        # A manually-removed entry (remove_program_entry) is soft-deleted via
        # status="suspended" rather than an actual row delete, so it must be
        # filtered out here — otherwise every caller that returns this straight
        # to the client (GET /v1/program, and the response after add/remove)
        # would show a "removed" exercise forever. _dashboard_slice is the one
        # deliberate exception: it needs the suspended rows too, to cancel out
        # the tier backfill for a removed entry (see its own comment).
        query = query.where(UserProgram.status == "active")
    rows = session.execute(query.order_by(ActivityTaxonomy.display_name)).all()
    out = []
    for p, t in rows:
        amount = float(p.target_amount)
        est = None
        if t.prescription_type == "distance" and t.est_pace_min_per_km:
            # An honest time estimate without storing a second target.
            est = round(amount * float(t.est_pace_min_per_km))
        # Per-set breakdown (PER_SET_PROGRESSION_PLAN.md): the real diverging
        # values when this activity has adopted per-set tracking, or a
        # uniform list derived from the blended amount otherwise — the
        # frontend always gets a real array of length `sets` to render a
        # per-set table from, regardless of which system this entry is on.
        set_targets = ([float(v) for v in p.set_targets] if p.set_targets is not None
                       else [amount] * p.target_sets)
        out.append({
            "activity_type": p.activity_type,
            "display_name": t.display_name,
            "prescription_type": t.prescription_type,
            "sets": p.target_sets,
            "amount": amount,
            "set_targets": set_targets,
            "unit": p.unit,
            "per_side": t.per_side,
            "rest_seconds": t.rest_seconds,
            "prescription": t.describe(p.target_sets, amount),
            "total_volume": t.total_volume(p.target_sets, amount),
            "est_minutes": est,
            "status": p.status,
            "caution": modified.get(p.activity_type),
            "demo_gif": t.demo_gif,
            "demo_images": t.demo_images,
            "instructions": t.instructions,
            "priority_order": t.priority_order,
            # Condition-personalized "most useful first" rank (see
            # _usefulness_ranks). Falls back to the generic taxonomy order
            # only if this user somehow has no rank for it at all (can't
            # happen for a real condition set — every supported condition's
            # safe/modify cells are required to carry a rank — but a fallback
            # keeps a data gap from ever hiding an activity outright).
            "usefulness_rank": ranks.get(p.activity_type, t.priority_order),
            "source": p.source,
            "auto_progress": p.auto_progress,
        })
    return out


# --- Manual program control (14.7) ------------------------------------------

def _worst_verdict(session: Session, user_id: str, activity_type: str) -> dict | None:
    """The most restrictive verdict for one activity, across all of a user's
    conditions AND every pair of them (combined-conditions check, see
    _interaction_verdicts) — the one shown in the override dialog. None if
    neither source has anything for this activity (as safe as `none`).

    Return shape carries a `kind`: "single" (unchanged shape from before this
    feature — one condition_code/condition_name) or "pair" (two conditions,
    condition_a_code/name + condition_b_code/name instead). Callers branch on
    `kind` to pick the right warning wording (see _override_warning vs.
    _override_warning_pair) and to record the right OverrideConsent columns.
    """
    rows = session.execute(
        select(SafetyGridCell.verdict, SafetyGridCell.note, SafetyGridCell.reviewed_at,
               Condition.condition_code, Condition.display_name)
        .join(UserCondition, UserCondition.condition_code == SafetyGridCell.condition_code)
        .join(Condition, Condition.condition_code == SafetyGridCell.condition_code)
        .where(UserCondition.user_id == user_id,
               SafetyGridCell.activity_type == activity_type)
    ).all()
    single = None
    if rows:
        worst = max(rows, key=lambda r: _VERDICT_RANK[r.verdict])
        single = {"kind": "single", "condition_code": worst.condition_code,
                  "condition_name": worst.display_name, "verdict": worst.verdict,
                  "note": worst.note, "reviewed_at": worst.reviewed_at}

    interaction = _interaction_verdicts(session, user_id).get(activity_type)
    pair = None
    if interaction is not None:
        pair = {"kind": "pair", "verdict": interaction["verdict"], "note": interaction["note"],
                "reviewed_at": interaction["reviewed_at"],
                "condition_a_code": interaction["condition_a_code"],
                "condition_a_name": interaction["condition_a_name"],
                "condition_b_code": interaction["condition_b_code"],
                "condition_b_name": interaction["condition_b_name"]}

    if pair is None:
        return single
    if single is None:
        return pair
    # A tie prefers the pair result — it's the more specific, more useful
    # reason to show the user (names both conditions, not just one).
    if _VERDICT_RANK[pair["verdict"]] >= _VERDICT_RANK[single["verdict"]]:
        return pair
    return single


def _override_warning(condition_name: str) -> str:
    """Identical wording on every entry point that can write a user_override —
    manual add here, and any future one (16.1) — per 14.7.

    Names the condition explicitly so the reason is concrete, not generic. The
    bar stays doctor sign-off, not self-assessment: "I feel fine now" is
    exactly the judgement a reviewed grid exists to not depend on.
    """
    return (f"You've told us you have {condition_name}. This exercise isn't recommended "
            "for that condition, so it's blocked by default. Only continue if a doctor "
            "has examined you and specifically advised you to do it.")


def _override_warning_pair(condition_a_name: str, condition_b_name: str) -> str:
    """Combined-conditions variant of _override_warning — used when the
    `avoid` verdict came from a condition PAIR (_worst_verdict kind="pair"),
    not a single condition. Names both, and says explicitly that either one
    alone would be fine, so the warning doesn't read as a generic duplicate
    of the single-condition message."""
    return (f"You've told us you have both {condition_a_name} and {condition_b_name}. "
            "Together, this combination isn't recommended for this exercise, even though "
            "either one alone might be fine. Only continue if a doctor has examined you "
            "and specifically advised you to do it.")


def list_catalogue(session: Session, user_id: str) -> list[dict]:
    """14.7 / 13.3b — everything NOT already on the dashboard: tier-hidden
    activities the user hasn't grown into yet, and anything their condition
    blocks. Deliberately excludes by dashboard VISIBILITY, not raw program
    membership — most of the taxonomy is already silently in `user_program`
    from onboarding (13.3/14.2), so filtering on that would leave almost
    nothing to offer. Refer-bucket users cannot open this at all, the same
    hard gate as suggestions and videos.
    """
    if is_referred(session, user_id):
        raise Forbidden("Speak to a doctor before adding exercises to your program.")
    _program, visible, _band, _default, _optional = _dashboard_slice(session, user_id)
    ranks = _usefulness_ranks(session, user_id)
    taxonomy = session.scalars(
        select(ActivityTaxonomy).order_by(ActivityTaxonomy.priority_order)).all()
    taxonomy = sorted(taxonomy, key=lambda t: (ranks.get(t.activity_type, t.priority_order), t.priority_order))

    from app.progression.service import _round_reps
    from app.relationships.service import _EXPERIENCE_MULTIPLIER
    prof = session.get(UserProfile, user_id)
    multiplier = _EXPERIENCE_MULTIPLIER.get((prof.experience_band if prof else None) or "beginner", 1.0)

    out = []
    for t in taxonomy:
        if t.activity_type in visible:
            continue
        v = _worst_verdict(session, user_id, t.activity_type)
        scaled = float(t.default_amount) * multiplier
        scaled_amount = (_round_reps(scaled) if t.prescription_type in ("reps", "hold")
                         else round(scaled, 1))
        out.append({
            "activity_type": t.activity_type,
            "display_name": t.display_name,
            "prescription_type": t.prescription_type,
            "default_sets": 1 if t.prescription_type in ("distance", "duration") else t.default_sets,
            "default_amount": scaled_amount,
            "amount_unit": t.amount_unit,
            "per_side": t.per_side,
            "verdict": v["verdict"] if v else "safe",
            "caution": v["note"] if v and v["verdict"] != "safe" else None,
        })
    return out


def add_program_entry(session: Session, user_id: str, activity_type: str,
                      override_confirmed: bool = False, *,
                      acting_manager_id: str | None = None,
                      auto_progress: bool = True) -> dict:
    """14.7 — user-driven add, curated taxonomy only, never free text.

    An `avoid`-verdict activity is permitted, but only past a blocking
    confirmation naming the condition and verdict — a doctor who examined the
    user outranks the generic table, but the app does not adopt the choice as
    its own advice, and never progresses what it does not consider safe.

    Managed plans (MANAGED_PLANS_TEACHER_DOCTOR_PARENT_PLAN.md): `user_id` is
    always the program's OWNER (the managed person), never the caller when a
    manager is acting on someone else's behalf — that caller is
    `acting_manager_id`, required to hold an active relationship with
    `user_id`, checked here so nothing downstream has to re-derive
    authorization. `auto_progress` is only meaningful for a manager-created
    entry; an ordinary self-add always keeps the column's default (True).
    """
    if is_referred(session, user_id):
        raise Forbidden("Speak to a doctor before adding exercises to your program.")
    t = session.get(ActivityTaxonomy, activity_type)
    if t is None:
        raise NotFound(f"Unknown activity: {activity_type}")

    relationship = None
    if acting_manager_id is not None:
        relationship = require_active_relationship(session, acting_manager_id, user_id)
        # Two managers on the same activity: blocked for v1, never a silent
        # takeover — see the plan doc §3.3. A normal self-added entry (no
        # managed_by_relationship_id) is always fine to convert into a
        # managed one; only a DIFFERENT, still-active manager is blocked.
        existing_for_block_check = session.get(UserProgram, (user_id, activity_type))
        if (existing_for_block_check is not None
                and existing_for_block_check.managed_by_relationship_id is not None
                and existing_for_block_check.managed_by_relationship_id != relationship.id):
            other = session.get(
                ManagedRelationship, existing_for_block_check.managed_by_relationship_id)
            if other is not None and other.status == "active":
                raise Conflict(
                    f"{t.display_name} is already managed by another "
                    f"{other.relation_type} relationship for this user."
                )

    worst = _worst_verdict(session, user_id, activity_type)
    if worst and worst["verdict"] == "avoid":
        is_pair = worst["kind"] == "pair"
        warning_text = (
            _override_warning_pair(worst["condition_a_name"], worst["condition_b_name"])
            if is_pair else _override_warning(worst["condition_name"])
        )
        if not override_confirmed:
            response = {
                "added": False, "needs_confirmation": True,
                "verdict": "avoid",
                "warning_text": warning_text,
            }
            if is_pair:
                response["condition_code"] = worst["condition_a_code"]
                response["condition_name"] = worst["condition_a_name"]
                response["condition_code_2"] = worst["condition_b_code"]
                response["condition_name_2"] = worst["condition_b_name"]
            else:
                response["condition_code"] = worst["condition_code"]
                response["condition_name"] = worst["condition_name"]
            return response
        session.add(OverrideConsent(
            user_id=user_id,
            condition_code=worst["condition_a_code"] if is_pair else worst["condition_code"],
            condition_code_2=worst["condition_b_code"] if is_pair else None,
            activity_type=activity_type, grid_verdict="avoid",
            grid_reviewed_at=worst["reviewed_at"],
            warning_text=warning_text,
        ))
        source = "user_override"
    else:
        source = "manual"

    existing = session.get(UserProgram, (user_id, activity_type))
    if existing is not None:
        # Re-adding restores rather than resets (14.9's principle) — whatever
        # target it already had stands, it just becomes active again. Source
        # updates to manual/user_override even if it was `template` before:
        # this was a deliberate catalogue pick, so it stays visible on the
        # dashboard regardless of tier from now on (_dashboard_slice).
        existing.status = "active"
        existing.suspended_reason = None
        existing.source = source
        # A re-add is a fresh start for "logged today" purposes (matches how
        # deleting+recreating a custom exercise gets a fresh id): any log from
        # before this moment must not count toward today's completion below.
        existing.added_at = datetime.now(timezone.utc)
        if relationship is not None:
            existing.managed_by_relationship_id = relationship.id
            existing.auto_progress = auto_progress
    else:
        session.add(UserProgram(
            user_id=user_id, activity_type=activity_type,
            target_sets=t.default_sets, target_amount=t.default_amount,
            unit=t.amount_unit, source=source,
            managed_by_relationship_id=relationship.id if relationship else None,
            auto_progress=auto_progress if relationship else True,
        ))
    session.flush()
    return {"added": True, "needs_confirmation": False, "program": get_program(session, user_id)}


def set_managed_target(session: Session, manager_user_id: str, managed_user_id: str,
                       activity_type: str, target_sets: int, target_amount: float) -> dict:
    """The first-ever direct "set this exact number" write in the app — see
    MANAGED_PLANS_TEACHER_DOCTOR_PARENT_PLAN.md §4. Every other target change
    in this codebase only ever happens through the automated weekly job or a
    confirmed chat escalation; this exists specifically so a manager can
    override whatever the app proposes (or set a fixed one from scratch),
    which neither of those paths allows for anyone.

    Only usable on an entry THIS manager's own active relationship is
    responsible for — not just any entry belonging to a managed user, in
    case a future multi-manager scenario ever has more than one manager on
    the same account (see the plan doc §3.3's "blocked, not merged" note).
    """
    relationship = require_active_relationship(session, manager_user_id, managed_user_id)
    entry = session.get(UserProgram, (managed_user_id, activity_type))
    if entry is None or entry.managed_by_relationship_id != relationship.id:
        raise NotFound(f"No entry for {activity_type} managed by this relationship.")

    t = session.get(ActivityTaxonomy, activity_type)
    if round(target_amount, 2) <= 0:
        raise ValidationError("Amount must be greater than zero.")
    if target_sets < 1:
        raise ValidationError("Sets must be at least 1.")
    if t.prescription_type in ("distance", "duration") and target_sets != 1:
        raise ValidationError(f"{t.display_name} is a single effort, not sets.")

    if t.prescription_type == "reps":
        # Same half-up whole-number rule as every other reps write in this
        # codebase (app/progression/service.py's _round_reps) — a manager
        # typing "12.5" must not silently save a target nobody can perform.
        from app.progression.service import _round_reps  # local: avoids a
        # module-level import cycle (progression/service.py already imports
        # from this module).
        target_amount = _round_reps(target_amount)

    entry.target_sets = target_sets
    entry.target_amount = Decimal(str(target_amount))
    entry.internal_target_amount = Decimal(str(target_amount))
    session.flush()
    return {
        "activity_type": activity_type, "target_sets": entry.target_sets,
        "target_amount": float(entry.target_amount), "auto_progress": entry.auto_progress,
    }


def set_own_target(session: Session, user_id: str, activity_type: str,
                   target_sets: int, target_amount: float,
                   set_amounts: list[float] | None = None,
                   auto_progress: bool | None = None) -> dict:
    """Self-service equivalent of set_managed_target above — a user fixing
    their own mistake (wrong default, or they just want a different number),
    not a manager overriding someone else's. Same validation, no relationship
    check since there's no manager/managed split here.

    set_amounts (optional): a different target per individual set (e.g.
    [12, 10, 8] for a 3-set pyramid), reps or hold activities only — this is
    the same set_targets column app/progression/service.py's
    start_per_set_tracking_if_needed seeds lazily from real logged
    performance, just populated directly from what the user typed up front
    instead of waiting for organic logging to diverge it from a uniform
    start. When given, it replaces target_sets/target_amount entirely
    (target_sets becomes its length, target_amount its average, matching
    the column's own documented invariant). For reps, this also activates
    the weekly per-set auto-progression engine (reps_ceiling). That engine's
    growth math (ceilings, trailing averages) is reps-specific, so a hold
    activity's breakdown stays fixed instead — auto_progress is forced off
    rather than handed to an engine tuned for rep counts, not seconds.

    auto_progress (optional): None leaves the entry's existing flag
    untouched; applied before the set_amounts branch above, which can still
    force it False afterward for a hold-type per-set breakdown."""
    entry = session.get(UserProgram, (user_id, activity_type))
    if entry is None:
        raise NotFound(f"{activity_type} is not in your plan.")

    t = session.get(ActivityTaxonomy, activity_type)
    from app.progression.service import REPS_STANDARD_CEILING, _round_reps

    if auto_progress is not None:
        entry.auto_progress = auto_progress

    if set_amounts is not None:
        if t.prescription_type not in ("reps", "hold"):
            raise ValidationError(f"{t.display_name} doesn't support a different amount per set.")
        if len(set_amounts) < 1:
            raise ValidationError("Provide at least one set.")
        if any(round(v, 2) <= 0 for v in set_amounts):
            raise ValidationError("Each set's amount must be greater than zero.")
        is_reps = t.prescription_type == "reps"
        rounded = [_round_reps(v) for v in set_amounts] if is_reps else [round(v, 2) for v in set_amounts]
        entry.set_targets = [Decimal(str(v)) for v in rounded]
        entry.internal_set_targets = [Decimal(str(v)) for v in rounded]
        if is_reps:
            entry.reps_ceiling = Decimal(str(REPS_STANDARD_CEILING))
        else:
            entry.auto_progress = False
        entry.target_sets = len(rounded)
        avg = _round_reps(sum(rounded) / len(rounded)) if is_reps else round(sum(rounded) / len(rounded), 2)
        entry.target_amount = Decimal(str(avg))
        entry.internal_target_amount = Decimal(str(avg))
        session.flush()
        return {
            "activity_type": activity_type, "target_sets": entry.target_sets,
            "target_amount": float(entry.target_amount),
            "set_targets": [float(v) for v in entry.set_targets],
            "auto_progress": entry.auto_progress,
        }

    if round(target_amount, 2) <= 0:
        raise ValidationError("Amount must be greater than zero.")
    if target_sets < 1:
        raise ValidationError("Sets must be at least 1.")
    if t.prescription_type in ("distance", "duration") and target_sets != 1:
        raise ValidationError(f"{t.display_name} is a single effort, not sets.")
    if t.prescription_type == "reps":
        target_amount = _round_reps(target_amount)

    entry.target_sets = target_sets
    entry.target_amount = Decimal(str(target_amount))
    entry.internal_target_amount = Decimal(str(target_amount))
    # A fresh uniform target overrides any earlier per-set divergence -- the
    # old per-set breakdown would otherwise keep showing stale numbers next
    # to a brand-new blended target that no longer agrees with them.
    entry.set_targets = None
    entry.internal_set_targets = None
    session.flush()
    return {
        "activity_type": activity_type, "target_sets": entry.target_sets,
        "target_amount": float(entry.target_amount), "auto_progress": entry.auto_progress,
    }


def remove_program_entry(session: Session, user_id: str, activity_type: str) -> dict:
    """14.7 — always permitted, for any entry, even for a referred user.

    Marks the entry removed rather than deleting it, and _dashboard_slice
    trims the tier's visible count to match - a manual removal must stay
    removed, never silently backfilled by the next-priority template
    activity. Bringing it back is only ever a deliberate catalogue add.
    """
    row = session.get(UserProgram, (user_id, activity_type))
    if row is not None:
        row.status = "suspended"
        row.suspended_reason = "user_removed"
    session.flush()
    return {"removed": True, "program": get_program(session, user_id)}


def set_program_to_activities(session: Session, user_id: str, activity_types: list[str]) -> dict:
    """Drives the chat's "add these suggested exercises to your program?"
    confirmation (found live: a user asked the rehab chatbot for
    sport-specific exercises and wanted a one-tap way to actually put them in
    their program). Ensures exactly this set of activities ends up active —
    adds whichever aren't already there (or restores one that was previously
    hidden), and soft-suspends (never deletes) any currently-active entry NOT
    in this set, so switching focus to a new sport can't destroy progress on
    an exercise the user might want back later — same soft-suspend convention
    build_program already uses for condition-based safety changes, just a
    different reason so the two are never confused with each other (a
    condition being removed later must not silently resurrect something the
    user deliberately swapped out for a sport focus, and vice versa).

    Every activity_type passed in is trusted to already be safety-approved for
    this user — the caller (the chat's suggestion-confirmation flow) only
    ever offers activity_types that came from _approved_activities in the
    first place, so this function doesn't re-check the safety grid itself.
    A `user_override` entry (the user explicitly confirmed a KNOWN risk via
    add_program_entry) is left untouched even if this set doesn't include it —
    that's the same deliberate-choice protection build_program already gives
    it, not something a sport-focus change should silently reverse either.
    """
    if is_referred(session, user_id):
        raise Forbidden("Speak to a doctor before changing your program.")

    wanted = set(activity_types)
    existing = {
        row.activity_type: row
        for row in session.scalars(
            select(UserProgram).where(UserProgram.user_id == user_id)
        ).all()
    }
    taxonomy_by_type = {
        t.activity_type: t
        for t in session.scalars(
            select(ActivityTaxonomy).where(ActivityTaxonomy.activity_type.in_(wanted))
        ).all()
    }

    added, already_active, hidden = [], [], []
    for activity_type in wanted:
        entry = existing.get(activity_type)
        if entry is None:
            taxonomy = taxonomy_by_type.get(activity_type)
            if taxonomy is None:
                continue  # not a real activity_type — ignore rather than error
            session.add(UserProgram(
                user_id=user_id, activity_type=activity_type,
                target_sets=taxonomy.default_sets, target_amount=taxonomy.default_amount,
                unit=taxonomy.amount_unit, source="template",
            ))
            added.append(activity_type)
        elif entry.status == "active":
            already_active.append(activity_type)
        else:
            # Was hidden (any prior reason) — the user just explicitly asked
            # for it back, so restore it with whatever it already had rather
            # than resetting to a fresh baseline.
            entry.status = "active"
            entry.suspended_reason = None
            already_active.append(activity_type)

    for activity_type, entry in existing.items():
        if (
            activity_type not in wanted
            and entry.status == "active"
            and entry.source != "user_override"
        ):
            entry.status = "suspended"
            entry.suspended_reason = "sport_focus_change"
            hidden.append(activity_type)

    session.flush()
    return {
        "added": added, "already_active": already_active, "hidden": hidden,
        "program": get_program(session, user_id),
    }


def restore_program_activities(session: Session, user_id: str, activity_types: list[str]) -> dict:
    """Restores specific hidden program entries back to active, with
    whatever target they already had — the same "restore rather than reset"
    principle add_program_entry already uses. Purely additive: unlike
    set_program_to_activities, this never hides anything else. Drives the
    chat's "bring these back?" revert confirmation (found live: a user
    wanted a way to undo a previous sport-focus program change by asking the
    chatbot directly, not by manually re-adding each exercise one by one on
    the dashboard)."""
    if is_referred(session, user_id):
        raise Forbidden("Speak to a doctor before changing your program.")
    restored = []
    for activity_type in activity_types:
        entry = session.get(UserProgram, (user_id, activity_type))
        if entry is not None and entry.status != "active":
            entry.status = "active"
            entry.suspended_reason = None
            restored.append(activity_type)
    session.flush()
    return {"restored": restored, "program": get_program(session, user_id)}


def _tier_slice(items_by_priority: list[dict], band: str) -> tuple[set[str], set[str]]:
    """Split activities into (default, optional) for a dashboard experience tier.

    `items_by_priority` only ever contains activities already cleared by the
    safety grid for this user (anything `avoid` was removed before this runs) —
    so backfilling here can only ever promote an already-verified-safe activity,
    never a risky one. If a condition removes one, the next-most-important safe
    activity fills its spot, keeping the tier's usual count intact. Only when
    the safe pool itself runs out short does the count actually drop — never
    padded with anything unvetted.

    Ordered by `usefulness_rank` (condition-personalized "most useful first",
    see _usefulness_ranks) rather than the old flat `priority_order` — a
    beginner with knee pain now sees Walking/Glute Bridge before a wrist
    stretch that happens to rank higher generically. `priority_order` is kept
    as the tiebreak for equal usefulness ranks, so ordering stays deterministic.
    """
    shown = TIER_SHOWN.get(band)
    if shown is None:
        return {i["activity_type"] for i in items_by_priority}, set()
    idx = TIER_ORDER.index(band)
    prev_shown = TIER_SHOWN[TIER_ORDER[idx - 1]] if idx > 0 else shown
    ordered = sorted(items_by_priority, key=lambda i: (i["usefulness_rank"], i["priority_order"]))
    default_n = min(prev_shown, len(ordered))
    optional_n = min(shown, len(ordered))
    default = {i["activity_type"] for i in ordered[:default_n]}
    optional = {i["activity_type"] for i in ordered[default_n:optional_n]}
    return default, optional


# --- Logging ---------------------------------------------------------------

def _personal_best_qualifies(session: Session, user_id: str, activity_type: str, value: float) -> bool:
    """True if `value` would earn a spot in this user's top-PERSONAL_BEST_LIMIT
    for `activity_type` — i.e. there's room, or it beats the current lowest
    entry kept."""
    current = session.scalars(
        select(PersonalBest.value)
        .where(PersonalBest.user_id == user_id, PersonalBest.activity_type == activity_type)
        .order_by(PersonalBest.value.desc())
    ).all()
    if len(current) < PERSONAL_BEST_LIMIT:
        return True
    return Decimal(str(value)) > current[-1]


def _save_personal_best(session: Session, user_id: str, activity_type: str, value: float,
                        unit: str, log_id: str) -> None:
    session.add(PersonalBest(
        user_id=user_id, activity_type=activity_type, value=value, unit=unit,
        source_log_id=log_id,
    ))
    session.flush()
    rows = session.scalars(
        select(PersonalBest)
        .where(PersonalBest.user_id == user_id, PersonalBest.activity_type == activity_type)
        .order_by(PersonalBest.value.desc())
    ).all()
    for extra in rows[PERSONAL_BEST_LIMIT:]:
        session.delete(extra)


def list_personal_bests(session: Session, user_id: str) -> dict:
    """Grouped by activity_type, each list ordered best-first. Returns the user's
    top records (up to 5) for each activity. Purely a celebratory display list —
    never consulted by streak, progression, or adherence math."""
    tax_names = {t.activity_type: t.display_name for t in session.scalars(select(ActivityTaxonomy)).all()}
    custom_names = {c.id: c.name for c in session.scalars(select(CustomExercise).where(CustomExercise.user_id == user_id)).all()}

    pb_rows = session.scalars(
        select(PersonalBest)
        .where(PersonalBest.user_id == user_id)
        .order_by(PersonalBest.activity_type, PersonalBest.value.desc())
    ).all()

    out: dict[str, list[dict]] = {}
    for r in pb_rows:
        out.setdefault(r.activity_type, []).append({
            "value": float(r.value),
            "sets": 1,
            "unit": r.unit,
            "total_volume": float(r.value),
            "achieved_at": r.achieved_at.isoformat(),
            "display_name": tax_names.get(r.activity_type, r.activity_type),
        })

    # For any activities with plausible logs not already in out from PersonalBest:
    logs = session.scalars(
        select(ActivityLog)
        .where(
            ActivityLog.user_id == user_id,
            ActivityLog.implausible.is_(False),
        )
        .order_by(ActivityLog.amount_per_set.desc(), ActivityLog.logged_at.desc())
    ).all()

    for l in logs:
        key = l.activity_type or l.custom_exercise_id
        if not key or key in out:
            continue
        display_name = tax_names.get(l.activity_type) or custom_names.get(l.custom_exercise_id) or key
        out[key] = [{
            "value": float(l.amount_per_set),
            "sets": l.sets_done,
            "unit": l.unit,
            "total_volume": float(l.total_volume),
            "achieved_at": l.logged_at.isoformat(),
            "display_name": display_name,
        }]

    return out




def log_activity(session: Session, user_id: str, *, activity_type: str | None = None,
                 custom_exercise_id: str | None = None, amount: float = 0.0, sets: int = 1,
                 unit: str | None = None, source: str = "manual",
                 client_entry_id: str | None = None,
                 workout_session_id: str | None = None,
                 set_amounts: list[float] | None = None) -> dict:
    if bool(activity_type) == bool(custom_exercise_id):
        raise ValidationError("Provide exactly one of activity_type or custom_exercise_id.")
    # Validated up front, before anything else: a bad workout_session_id
    # must fail the whole entry cleanly rather than log the exercise and
    # silently drop the session tag.
    workout_session = (
        _get_active_workout_session(session, user_id, workout_session_id)
        if workout_session_id else None
    )
    # Per-set breakdown (e.g. [10, 8, 6]) — only meaningful for the official
    # taxonomy's reps/hold exercises, never custom exercises (which have no
    # taxonomy row to validate a shape against) and never distance/duration
    # (already a single effort, nothing to break down). `sets`/`amount` are
    # DERIVED here — length and average — rather than trusted separately, so
    # every existing check below (positive, within caps, plausibility) runs
    # unchanged against the real numbers instead of needing its own parallel
    # validation path. Deriving `amount` as the plain average is what makes
    # `row.total_volume(sets, amount)` below compute the true SUM of the real
    # per-set values rather than an assumed-uniform total — sets * average ==
    # sum, by definition — with no separate total-volume formula needed.
    if set_amounts is not None:
        if custom_exercise_id:
            raise ValidationError("set_amounts is only supported for taxonomy activities.")
        if not set_amounts:
            raise ValidationError("set_amounts cannot be empty.")
        if len(set_amounts) > MAX_LOGGABLE_SETS:
            raise ValidationError(f"Cannot log more than {MAX_LOGGABLE_SETS} sets.")
        for v in set_amounts:
            if not math.isfinite(v):
                raise ValidationError("Each value in set_amounts must be a real, finite number.")
            # Found live: `v <= 0` alone let a value like 0.004 through — it's
            # genuinely positive, but ActivityLog.set_amounts is
            # Numeric(10,2), so storage rounds it to 0.00, which the
            # database's own CHECK(amount > 0)-style constraint then rejects
            # as an uncaught 500 instead of a clean validation error. Reject
            # anything that would round to zero at the precision it's
            # actually stored at, not just anything <= 0.
            if round(v, 2) <= 0:
                raise ValidationError("Each value in set_amounts must be greater than zero.")
            if v > MAX_LOGGABLE_AMOUNT:
                raise ValidationError(f"Each value in set_amounts cannot exceed {MAX_LOGGABLE_AMOUNT}.")
        sets = len(set_amounts)
        amount = sum(set_amounts) / sets
    # Found live (second-pass code audit): Python's json module accepts bare
    # NaN/Infinity literals by default (json.loads('{"amount": NaN}') ->
    # {'amount': nan}). NaN comparisons are always False, so `amount >
    # MAX_LOGGABLE_AMOUNT` and `amount <= 0` below both silently pass for
    # NaN — execution reached the implausible-flag Decimal comparison
    # further down and raised an uncaught decimal.InvalidOperation instead
    # of a clean 422. math.isfinite() explicitly rejects NaN and +/-Infinity
    # up front, before either bound check.
    if not math.isfinite(amount):
        raise ValidationError("Amount must be a real, finite number.")
    if amount > MAX_LOGGABLE_AMOUNT:
        raise ValidationError(f"Amount cannot exceed {MAX_LOGGABLE_AMOUNT}.")
    if sets > MAX_LOGGABLE_SETS:
        raise ValidationError(f"Sets cannot exceed {MAX_LOGGABLE_SETS}.")
    if client_entry_id is not None and len(client_entry_id) > 64:
        # Found live (code audit): client_entry_id is String(64) — an
        # overlong value used to hit the database as an uncaught
        # "value too long" error instead of a clean 422.
        raise ValidationError("client_entry_id cannot exceed 64 characters.")
    if source not in LOG_SOURCES:
        # Found live (code audit): `source` went straight from the request
        # body to the INSERT, guarded only by the table's own
        # CheckConstraint("source IN ('manual','voice')") — so any other
        # value surfaced as an uncaught IntegrityError (500), and in a batch
        # call that single bad entry rolled back every other entry in the
        # same transaction. An offline-replay client would then retry the
        # whole doomed batch forever.
        raise ValidationError(f"source must be one of: {', '.join(sorted(LOG_SOURCES))}")

    if custom_exercise_id:
        # 4.4 — same entry point, same validation shape as a taxonomy log;
        # the only different source is where the ceiling/unit come from.
        custom = session.get(CustomExercise, custom_exercise_id)
        if custom is None or custom.user_id != user_id or custom.deleted_at is not None:
            raise NotFound(f"Unknown custom exercise: {custom_exercise_id}")
        # unit->type is 1:1 (count/km/min never collide) — a custom exercise
        # has exactly one measurement_type now, so the log's unit just has to
        # match it, never pick between several the way it once could.
        measurement_type = custom.measurement_type
        expected_unit = CUSTOM_MEASUREMENT_UNITS[measurement_type]
        if unit is not None and unit != expected_unit:
            raise ValidationError(f"{custom.name} is measured in {expected_unit}, not {unit}.")
        unit = expected_unit
        # Found live: same gap as set_amounts above — amount is stored as
        # Numeric(10,2); a value like 0.004 passes `amount <= 0` but rounds
        # to 0.00 in storage, which the database then rejects uncaught.
        if round(amount, 2) <= 0:
            raise ValidationError("Amount must be greater than zero.")
        if sets < 1:
            raise ValidationError("Sets must be at least 1.")
        if measurement_type in ("distance", "duration") and sets != 1:
            raise ValidationError(f"{custom.name} is a single effort, not sets.")
        total = amount * sets
        if total > MAX_TOTAL_VOLUME:
            raise ValidationError(f"Total volume (amount x sets) cannot exceed {MAX_TOTAL_VOLUME}.")
        implausible = Decimal(str(total)) > Decimal(str(CUSTOM_MEASUREMENT_DEFAULT_MAX[measurement_type]))
        prescription = f"{sets} x {amount} {unit}" if sets > 1 else f"{amount} {unit}"
        row_activity_type = None
        # Personal Bests only cover curated taxonomy activities (walk, run,
        # cycle, swim) — a custom exercise has no plausible_max_total to be
        # "extraordinary" relative to, so it's never eligible.
        extraordinary = False
    else:
        row = session.get(ActivityTaxonomy, activity_type)
        if row is None:
            raise NotFound(f"Unknown activity: {activity_type}")

        unit = unit or row.amount_unit
        if unit != row.amount_unit:
            raise ValidationError(
                f"{row.display_name} is measured in {row.amount_unit}, not {unit}."
            )
        # Found live: same gap as set_amounts/custom-exercise amount above —
        # amount is stored as Numeric(10,2); a value like 0.004 passes
        # `amount <= 0` but rounds to 0.00 in storage, which the database
        # then rejects as an uncaught 500 instead of a clean 422.
        if round(amount, 2) <= 0:
            raise ValidationError("Amount must be greater than zero.")
        if sets < 1:
            raise ValidationError("Sets must be at least 1.")
        if row.prescription_type in ("distance", "duration") and sets != 1:
            # One continuous effort. Sets would be meaningless and would double-count
            # into total_volume.
            raise ValidationError(f"{row.display_name} is a single effort, not sets.")

        total = row.total_volume(sets, amount)
        if total > MAX_TOTAL_VOLUME:
            raise ValidationError(f"Total volume (amount x sets) cannot exceed {MAX_TOTAL_VOLUME}.")
        # 4.6 — kept and flagged, never rejected. A user's entry is theirs.
        implausible = Decimal(str(total)) > row.plausible_max_total
        prescription = (
            f"{', '.join(f'{v:g}' for v in set_amounts)} {unit}"
            if set_amounts is not None else row.describe(sets, amount)
        )
        row_activity_type = activity_type
        # An implausible distance/duration effort (never reps/sets — those
        # keep today's behaviour unchanged) that ranks in this user's own
        # top-PERSONAL_BEST_LIMIT is a likely genuine extraordinary result,
        # not a typo/glitch — see PersonalBest and the module docstring above
        # PERSONAL_BEST_LIMIT. The actual row is only saved after `log` below
        # gets an id (_save_personal_best needs it as source_log_id).
        extraordinary = (
            implausible and row.prescription_type in ("distance", "duration")
            and _personal_best_qualifies(session, user_id, activity_type, total)
        )

    if client_entry_id:
        existing = session.scalar(
            select(ActivityLog).where(
                ActivityLog.user_id == user_id,
                ActivityLog.client_entry_id == client_entry_id)
        )
        if existing:  # offline replay (12.5) — not a second log
            return {"id": existing.id, "duplicate": True,
                    "implausible": existing.implausible,
                    "total_volume": float(existing.total_volume)}

    log = ActivityLog(
        user_id=user_id, activity_type=row_activity_type,
        custom_exercise_id=custom_exercise_id,
        sets_done=sets, amount_per_set=amount, unit=unit, total_volume=total,
        local_date=today_ist(), source=source, implausible=implausible,
        extraordinary=extraordinary,
        client_entry_id=client_entry_id,
        workout_session_id=workout_session.id if workout_session else None,
        set_amounts=set_amounts,
    )
    if client_entry_id:
        # The existence check above is TOCTOU: two concurrent offline-replay
        # requests carrying the same client_entry_id (exactly the flaky-
        # mobile-network case this field exists for) can both pass it, then
        # both try to insert, and the second one used to surface as an
        # uncaught IntegrityError -> 500 instead of the intended
        # `duplicate: true`. A SAVEPOINT scopes the failure to just this
        # insert — a batch call logs several entries in one transaction, and
        # this must not roll back the ones that already succeeded.
        try:
            with session.begin_nested():
                session.add(log)
                if workout_session:
                    workout_session.completed_activity_count += 1
                session.flush()
        except IntegrityError:
            existing = session.scalar(
                select(ActivityLog).where(
                    ActivityLog.user_id == user_id,
                    ActivityLog.client_entry_id == client_entry_id)
            )
            if existing is None:
                raise  # a different constraint failed — not the dedupe race
            # A genuine replay, not a fresh log — the session's count was
            # never meant to count this twice.
            return {"id": existing.id, "duplicate": True,
                    "implausible": existing.implausible,
                    "total_volume": float(existing.total_volume)}
    else:
        session.add(log)
        if workout_session:
            workout_session.completed_activity_count += 1
        session.flush()
    if extraordinary:
        _save_personal_best(session, user_id, row_activity_type, total, unit, log.id)
    return {"id": log.id, "duplicate": False, "implausible": implausible,
            "extraordinary": extraordinary,
            "total_volume": total, "prescription": prescription}


# --- Soreness reporting & target reduction ----------------------------------

SORENESS_REDUCE_PCT = 0.25  # 25% target reduction on trigger
SORENESS_AUTO_RESTORE_DAYS = 14


def report_soreness(session: Session, user_id: str, activity_type: str) -> dict:
    """Records a soreness report, then checks both trigger conditions. If
    either is met and the entry isn't already reduced, reduces it instantly
    (does not wait for the weekly job). Returns {"reduced": bool,
    "target_amount": float} so the frontend knows whether to show the
    'reduced' state immediately."""
    t = session.get(ActivityTaxonomy, activity_type)
    if t is None:
        raise NotFound(f"Unknown activity: {activity_type}")
    entry = session.get(UserProgram, (user_id, activity_type))
    if entry is None:
        raise NotFound(f"{activity_type} is not in your plan.")

    session.add(SorenessReport(user_id=user_id, activity_type=activity_type))
    session.flush()

    if entry.soreness_reduced_since is not None:
        return {"reduced": True, "target_amount": float(entry.target_amount)}

    recent = session.scalars(
        select(SorenessReport.reported_at)
        .where(SorenessReport.user_id == user_id,
               SorenessReport.activity_type == activity_type)
        .order_by(SorenessReport.reported_at.desc())
        .limit(3)
    ).all()
    # (a) two most recent reports are THIS one and the immediately-preceding
    # one, with no successful (non-sore) log of this same activity logged
    # in between -- approximate "consecutive" by just checking the 2 most
    # recent soreness reports exist; a simpler, equally valid reading of
    # "2 in a row" for v1. (b) 3rd-most-recent report (if any) is within 7
    # days of now.
    triggered = len(recent) >= 2
    if not triggered and len(recent) >= 3:
        triggered = (datetime.now(timezone.utc) - recent[2]).days <= 7

    if not triggered:
        return {"reduced": False, "target_amount": float(entry.target_amount)}

    entry.pre_soreness_target_amount = entry.target_amount
    entry.pre_soreness_internal_target_amount = entry.internal_target_amount
    entry.target_amount = round(float(entry.target_amount) * (1 - SORENESS_REDUCE_PCT), 2)
    if entry.internal_target_amount is not None:
        entry.internal_target_amount = float(entry.internal_target_amount) * (1 - SORENESS_REDUCE_PCT)
    entry.soreness_reduced_since = datetime.now(timezone.utc)
    session.add(ProgressionHistory(
        user_id=user_id, activity_type=activity_type,
        old_target=entry.pre_soreness_target_amount, new_target=entry.target_amount,
        adherence_pct=0, curve_pct=-SORENESS_REDUCE_PCT * 100, reason="soreness_reduce",
    ))
    session.flush()
    return {"reduced": True, "target_amount": float(entry.target_amount)}


def clear_soreness(session: Session, user_id: str, activity_type: str) -> dict:
    """Explicit 'no longer sore' — restores the exact pre-reduction target."""
    entry = session.get(UserProgram, (user_id, activity_type))
    if entry is None or entry.soreness_reduced_since is None:
        raise NotFound(f"{activity_type} is not currently soreness-reduced.")
    old_target = entry.target_amount
    entry.target_amount = entry.pre_soreness_target_amount
    entry.internal_target_amount = entry.pre_soreness_internal_target_amount
    entry.soreness_reduced_since = None
    entry.pre_soreness_target_amount = None
    entry.pre_soreness_internal_target_amount = None
    session.add(ProgressionHistory(
        user_id=user_id, activity_type=activity_type,
        old_target=old_target, new_target=entry.target_amount,
        adherence_pct=0, curve_pct=0, reason="soreness_restore",
    ))
    session.flush()
    return {"target_amount": float(entry.target_amount)}


def auto_restore_stale_soreness(session: Session, user_id: str) -> None:
    """Safety net: call this once at the top of whatever function already
    builds the dashboard summary (find the existing `def dashboard` data
    assembly in app/profile/service.py used by GET /v1/dashboard/summary,
    and call this at its start) -- restores any entry whose
    soreness_reduced_since is more than SORENESS_AUTO_RESTORE_DAYS days old
    AND has had no new SorenessReport since then, exactly like clear_soreness
    above, just triggered automatically instead of by the user tapping a
    button."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=SORENESS_AUTO_RESTORE_DAYS)
    stale = session.scalars(
        select(UserProgram).where(
            UserProgram.user_id == user_id,
            UserProgram.soreness_reduced_since.is_not(None),
            UserProgram.soreness_reduced_since < cutoff,
        )
    ).all()
    for entry in stale:
        latest_report = session.scalar(
            select(func.max(SorenessReport.reported_at))
            .where(SorenessReport.user_id == user_id,
                   SorenessReport.activity_type == entry.activity_type)
        )
        if latest_report and latest_report >= cutoff:
            continue  # reported again recently -- stay reduced
        clear_soreness(session, user_id, entry.activity_type)


def list_soreness_status(session: Session, user_id: str) -> list[dict]:
    rows = session.scalars(
        select(UserProgram).where(
            UserProgram.user_id == user_id,
            UserProgram.soreness_reduced_since.is_not(None),
        )
    ).all()
    out = []
    for r in rows:
        t = session.get(ActivityTaxonomy, r.activity_type)
        out.append({
            "activity_type": r.activity_type,
            "display_name": t.display_name if t else r.activity_type,
            "reduced_since": r.soreness_reduced_since.isoformat(),
        })
    return out


# --- Water intake logging ---------------------------------------------------

WATER_DAILY_CAP = 40  # sanity ceiling, not a real medical limit -- just
                      # guards against a mis-tap spam loop logging something absurd


def add_water_glass(session: Session, user_id: str) -> dict:
    today = today_ist()
    row = session.get(WaterIntakeLog, (user_id, today))
    if row is None:
        row = WaterIntakeLog(user_id=user_id, local_date=today, glasses_count=0)
        session.add(row)
    if row.glasses_count >= WATER_DAILY_CAP:
        raise ValidationError(f"Cannot log more than {WATER_DAILY_CAP} glasses in one day.")
    row.glasses_count += 1
    session.flush()
    return {"glasses_count": row.glasses_count, "date": today.isoformat()}


def get_water_today(session: Session, user_id: str) -> dict:
    today = today_ist()
    row = session.get(WaterIntakeLog, (user_id, today))
    return {"glasses_count": row.glasses_count if row else 0, "date": today.isoformat()}


# --- Workout sessions (guided player) ---------------------------------------

def _get_active_workout_session(session: Session, user_id: str,
                                workout_session_id: str) -> WorkoutSession:
    row = session.get(WorkoutSession, workout_session_id)
    if row is None or row.user_id != user_id:
        raise NotFound(f"Unknown workout session: {workout_session_id}")
    if row.status != "active":
        raise Conflict("This workout session has already ended.")
    return row


def start_workout_session(session: Session, user_id: str,
                          activity_types: list[str]) -> dict:
    """Starts a guided Workout Player session. `activity_types` is exactly
    the list the player is about to show (taken from a /v1/suggestions call
    moments earlier) — planned_activity_count is a snapshot of that, frozen
    at start time, not recomputed later.

    Self-healing: any session this user already has marked 'active' gets
    closed out as 'abandoned' first. There is no age check here — starting a
    new session always supersedes whatever was active before, however old it
    was. A user who simply never comes back at all (no new session ever
    started) is instead caught by the periodic reaper,
    scripts/close_stale_workout_sessions.py.
    """
    session.execute(
        update(WorkoutSession)
        .where(WorkoutSession.user_id == user_id, WorkoutSession.status == "active")
        .values(status="abandoned", ended_at=datetime.now(timezone.utc))
    )
    row = WorkoutSession(
        user_id=user_id,
        started_at=datetime.now(timezone.utc),
        planned_activity_count=len(activity_types),
    )
    session.add(row)
    session.flush()
    return {
        "id": row.id,
        "started_at": row.started_at.isoformat(),
        "planned_activity_count": row.planned_activity_count,
    }


def finish_workout_session(session: Session, user_id: str, workout_session_id: str) -> dict:
    """Ends a session and returns its real, server-computed duration — never
    the client's own stopwatch, which can drift or freeze if the tab is
    backgrounded."""
    row = _get_active_workout_session(session, user_id, workout_session_id)
    row.status = "completed"
    row.ended_at = datetime.now(timezone.utc)
    session.flush()
    duration = (row.ended_at - row.started_at).total_seconds()
    return {
        "duration_seconds": round(duration),
        "planned_activity_count": row.planned_activity_count,
        "completed_activity_count": row.completed_activity_count,
    }


# --- Custom exercises (4.4) --------------------------------------------------
# Personal, tracking-only. Never read by suggestions_today, build_program, or
# list_catalogue — those stay taxonomy-only, on purpose (13.1).

CUSTOM_MEASUREMENT_UNITS = {"reps": "count", "distance": "km", "duration": "min"}
CUSTOM_MEASUREMENT_DEFAULT_MAX = {"reps": 500, "distance": 50, "duration": 300}
CUSTOM_EXERCISE_CAP = 20


def _normalise_exercise_name(name: str) -> str:
    return " ".join(name.strip().lower().split())


def _custom_exercise_out(row: CustomExercise, *, completed_today: bool = False) -> dict:
    return {"id": row.id, "name": row.name,
            "measurement_type": row.measurement_type,
            "unit": CUSTOM_MEASUREMENT_UNITS[row.measurement_type],
            "target_sets": row.target_sets if row.measurement_type == "reps" else None,
            "target_amount": float(row.target_amount),
            "completed_today": completed_today}


def list_custom_exercises(session: Session, user_id: str) -> list[dict]:
    rows = session.scalars(
        select(CustomExercise).where(CustomExercise.user_id == user_id,
                                      CustomExercise.deleted_at.is_(None))
        .order_by(CustomExercise.created_at)
    ).all()
    # Same "done today" concept the built-in cards already use (suggestions_today).
    done_today = set(session.scalars(
        select(ActivityLog.custom_exercise_id).where(
            ActivityLog.user_id == user_id,
            ActivityLog.local_date == today_ist(),
            ActivityLog.custom_exercise_id.is_not(None))
    ).all())
    return [_custom_exercise_out(r, completed_today=r.id in done_today) for r in rows]


def _reject_unsafe_name(name: str) -> None:
    """Server-side backstop for a name that reaches every client verbatim
    (the list card, the dashboard card, and — worse — this exact string
    reflected into several of this module's own error messages, which a
    client is likely to render less carefully than a normal list item).
    Today's frontend escapes correctly, but that means the ONLY thing
    stopping a stored-XSS name from executing is every future render path
    remembering to escape it too — the same class of bug this codebase
    already had to fix once (custom exercise list, see git history). Reject
    at the source instead of trusting every consumer forever."""
    if "<" in name or ">" in name:
        raise ValidationError("Name cannot contain '<' or '>'.")


def _reject_if_matches_taxonomy(session: Session, normalized: str, label: str) -> None:
    """Dedup against the official taxonomy (4.4) — "jogging" must resolve to
    the real `run` activity, never spawn a personal duplicate of it. Shared by
    create and rename so the matching rule can't drift between the two."""
    for t in session.scalars(select(ActivityTaxonomy)).all():
        synonyms = {s.lower() for s in (t.synonyms or [])}
        if normalized == t.display_name.lower() or normalized in synonyms:
            raise ValidationError(
                f'"{label}" matches the built-in activity "{t.display_name}" — log that instead.'
            )


def create_custom_exercise(session: Session, user_id: str, *, name: str,
                           measurement_type: str, target_amount: float,
                           target_sets: int = 1,
                           acting_manager_id: str | None = None) -> dict:
    """Managed plans: a manager can create a custom exercise for their
    managed person too — see MANAGED_PLANS_TEACHER_DOCTOR_PARENT_PLAN.md.
    Custom exercises have no auto_progress concept at all, for anyone, today
    (never auto-progressed — the target here is a fixed prescription, not
    something this app adjusts on its own), so unlike add_program_entry
    there's nothing to toggle here; this purely extends WHO can create one,
    not what it does.
    """
    if acting_manager_id is not None:
        require_active_relationship(session, acting_manager_id, user_id)
    name = (name or "").strip()
    if not name:
        raise ValidationError("Name is required.")
    if len(name) > 64:
        raise ValidationError("Name must be 64 characters or fewer.")
    _reject_unsafe_name(name)
    if measurement_type not in CUSTOM_MEASUREMENT_UNITS:
        raise ValidationError(
            f"measurement_type must be one of: {', '.join(sorted(CUSTOM_MEASUREMENT_UNITS))}")
    if round(target_amount, 2) <= 0:
        raise ValidationError("Enter a target greater than zero.")
    if measurement_type in ("distance", "duration"):
        target_sets = 1  # a single effort, same convention as ActivityTaxonomy
    elif target_sets < 1:
        raise ValidationError("Sets must be at least 1.")

    normalized = _normalise_exercise_name(name)
    _reject_if_matches_taxonomy(session, normalized, name)

    existing = session.scalar(
        select(CustomExercise).where(
            CustomExercise.user_id == user_id,
            CustomExercise.normalized_name == normalized,
            CustomExercise.deleted_at.is_(None))
    )
    if existing is not None:
        raise ValidationError(f'You already have a custom exercise called "{existing.name}".')

    count = session.scalar(
        select(func.count()).select_from(CustomExercise)
        .where(CustomExercise.user_id == user_id, CustomExercise.deleted_at.is_(None))
    )
    if count >= CUSTOM_EXERCISE_CAP:
        raise ValidationError(f"You can have at most {CUSTOM_EXERCISE_CAP} custom exercises.")

    row = CustomExercise(
        user_id=user_id, name=name, normalized_name=normalized,
        measurement_type=measurement_type, target_sets=target_sets,
        target_amount=target_amount,
    )
    session.add(row)
    session.flush()
    return _custom_exercise_out(row)


def rename_custom_exercise(session: Session, user_id: str, exercise_id: str,
                           new_name: str) -> dict:
    row = session.get(CustomExercise, exercise_id)
    if row is None or row.user_id != user_id or row.deleted_at is not None:
        raise NotFound("Custom exercise not found.")
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValidationError("Name is required.")
    if len(new_name) > 64:
        raise ValidationError("Name must be 64 characters or fewer.")
    _reject_unsafe_name(new_name)
    normalized = _normalise_exercise_name(new_name)
    _reject_if_matches_taxonomy(session, normalized, new_name)

    clash = session.scalar(
        select(CustomExercise).where(
            CustomExercise.user_id == user_id,
            CustomExercise.normalized_name == normalized,
            CustomExercise.deleted_at.is_(None),
            CustomExercise.id != exercise_id)
    )
    if clash is not None:
        raise ValidationError(f'You already have a custom exercise called "{clash.name}".')
    row.name = new_name
    row.normalized_name = normalized
    session.flush()
    return _custom_exercise_out(row)


def delete_custom_exercise(session: Session, user_id: str, exercise_id: str) -> dict:
    """Soft delete (4.4). Past logs keep referencing this row via the FK -
    recent_logs joins on it by name - so history keeps displaying exactly
    as it did before, it just drops off list_custom_exercises and can no
    longer be logged against (see log_activity above).
    """
    row = session.get(CustomExercise, exercise_id)
    if row is None or row.user_id != user_id or row.deleted_at is not None:
        raise NotFound("Custom exercise not found.")
    row.deleted_at = datetime.now(timezone.utc)
    session.flush()
    return {"deleted": True}


def recent_logs(session: Session, user_id: str, limit: int = 10) -> list[dict]:
    rows = session.execute(
        select(ActivityLog, ActivityTaxonomy, CustomExercise)
        .outerjoin(ActivityTaxonomy,
                   ActivityTaxonomy.activity_type == ActivityLog.activity_type)
        .outerjoin(CustomExercise,
                   CustomExercise.id == ActivityLog.custom_exercise_id)
        .where(ActivityLog.user_id == user_id)
        .order_by(ActivityLog.logged_at.desc())
        .limit(limit)
    ).all()
    out = []
    for l, t, c in rows:
        if t is not None:
            display_name = t.display_name
            prescription = t.describe(l.sets_done, float(l.amount_per_set))
        else:
            display_name = c.name
            prescription = (f"{l.sets_done} x {l.amount_per_set} {l.unit}"
                            if l.sets_done > 1 else f"{l.amount_per_set} {l.unit}")
        out.append({
            "display_name": display_name,
            "activity_type": l.activity_type,
            "custom": t is None,
            "sets": l.sets_done, "amount": float(l.amount_per_set), "unit": l.unit,
            "prescription": prescription,
            "logged_at": l.logged_at.isoformat(),
            "implausible": l.implausible, "source": l.source,
        })
    return out


def streak(session: Session, user_id: str) -> dict:
    """4.2 — computed from the logs, never cached as the authority."""
    days = session.scalars(
        select(ActivityLog.local_date).where(
            ActivityLog.user_id == user_id,
            # Found live (code audit): every other date-aggregation function
            # in this file (trend, activity_log_detail) already excludes
            # flagged-implausible logs; this one didn't, letting a single
            # garbage entry extend or preserve a streak it shouldn't.
            # An `extraordinary` log is still `implausible` (likely a genuine
            # personal-best effort, not a typo/glitch — see PersonalBest) and
            # DOES count as a real active day, unlike an ordinary implausible
            # one.
            or_(ActivityLog.implausible.is_(False), ActivityLog.extraordinary.is_(True)),
        )
        .group_by(ActivityLog.local_date).order_by(ActivityLog.local_date.desc())
    ).all()
    if not days:
        return {"current": 0, "last_active": None, "active_days": 0, "longest": 0}

    today = today_ist()
    # A streak survives "not yet today" — it breaks only after a full day missed.
    cursor = today if days[0] == today else today - timedelta(days=1)
    current = 0
    for d in days:
        if d == cursor:
            current += 1
            cursor -= timedelta(days=1)
        elif d < cursor:
            break

    # Longest run of consecutive active days ever, not just the one ending
    # today/yesterday — reuses the same `days` list, no extra query.
    longest = run = 0
    prev = None
    for d in sorted(days):
        run = run + 1 if prev is not None and d == prev + timedelta(days=1) else 1
        longest = max(longest, run)
        prev = d

    return {"current": current, "last_active": days[0].isoformat(),
            "active_days": len(days), "longest": longest}


MAX_TREND_DAYS = 365


def trend(session: Session, user_id: str, *, days: int | None = None,
          start_date: date | None = None, end_date: date | None = None) -> list[dict]:
    """4.3 — logged-activity counts over an arbitrary window.

    Either pass `days` (counts back from today, inclusive) or an explicit
    start_date/end_date pair for a custom range. weekly_volume below is just
    this with days=7, so the original 7-day dashboard chart is unchanged.
    """
    today = today_ist()
    if start_date is not None or end_date is not None:
        if start_date is None or end_date is None:
            raise ValidationError("start_date and end_date must both be provided together.")
        if start_date > end_date:
            raise ValidationError("start_date must be on or before end_date.")
        if end_date > today:
            end_date = today
        if (end_date - start_date).days + 1 > MAX_TREND_DAYS:
            raise ValidationError(f"Custom range cannot exceed {MAX_TREND_DAYS} days.")
        start, end = start_date, end_date
    else:
        n = days if days is not None else 7
        if n < 1 or n > MAX_TREND_DAYS:
            raise ValidationError(f"days must be between 1 and {MAX_TREND_DAYS}.")
        start, end = today - timedelta(days=n - 1), today

    rows = session.execute(
        select(ActivityLog.local_date, func.count())
        .where(ActivityLog.user_id == user_id,
               ActivityLog.local_date >= start,
               ActivityLog.local_date <= end,
               ActivityLog.implausible.is_(False))
        .group_by(ActivityLog.local_date)
    ).all()
    counts = {d: n for d, n in rows}
    span = (end - start).days + 1
    return [{"date": (start + timedelta(days=i)).isoformat(),
             "label": (start + timedelta(days=i)).strftime("%a")[0],
             "count": counts.get(start + timedelta(days=i), 0)}
            for i in range(span)]


def weekly_volume(session: Session, user_id: str) -> list[dict]:
    """Seven days of logged volume for the dashboard chart (unchanged, 4.3)."""
    return trend(session, user_id, days=7)


def signup_date(session: Session, user_id: str) -> date:
    """Account creation date — the activity heatmap's real lower bound, so a
    multi-month view doesn't render pre-signup days as if the account (and
    thus zero activity) already existed back then."""
    created_at = session.scalar(select(User.created_at).where(User.id == user_id))
    return created_at.date() if created_at else today_ist()


def activity_log_detail(session: Session, user_id: str, *,
                         start_date: date, end_date: date) -> list[dict]:
    """The itemized entries `trend()` deliberately doesn't give — which
    exercise, how much, on which day — not just a per-day count. Built for
    "what did I do yesterday" style questions (found live: RLM's analyze_
    performance only ever computes rolled-up trend numbers — adherence %,
    streak, plateau — and has no notion of a specific day's literal log, so
    that question got an honest "I don't have the exact details" instead of
    a real answer). Joins whichever of taxonomy/custom-exercise this row
    actually points at (schema-enforced: exactly one is ever set, 4.1).

    Found live (code audit): unlike trend(), which bounds its range at
    MAX_TREND_DAYS before doing any date math, this had NO upper bound at
    all — an explicit ?start=0001-01-01&end=9999-12-31 was accepted and ran
    an unbounded, unpaginated query. Same cap as trend() for the range, plus
    a hard row limit as a second line of defense for a long-tenured,
    frequently-logging user even within a valid range.
    """
    if start_date > end_date:
        raise ValidationError("start_date must be on or before end_date.")
    if (end_date - start_date).days + 1 > MAX_TREND_DAYS:
        raise ValidationError(f"Date range cannot exceed {MAX_TREND_DAYS} days.")
    rows = session.execute(
        select(ActivityLog, ActivityTaxonomy.display_name, CustomExercise.name)
        .outerjoin(ActivityTaxonomy, ActivityTaxonomy.activity_type == ActivityLog.activity_type)
        .outerjoin(CustomExercise, CustomExercise.id == ActivityLog.custom_exercise_id)
        .where(ActivityLog.user_id == user_id,
               ActivityLog.local_date >= start_date, ActivityLog.local_date <= end_date,
               ActivityLog.implausible.is_(False))
        .order_by(ActivityLog.local_date.desc(), ActivityLog.logged_at.desc())
        .limit(2000)
    ).all()
    return [
        {
            "date": log.local_date.isoformat(),
            "name": taxonomy_name or custom_name or log.activity_type,
            "sets": log.sets_done,
            "amount": float(log.amount_per_set),
            "unit": log.unit,
            "total_volume": float(log.total_volume),
            # The real per-set breakdown when one was logged (e.g. a set
            # edited to something other than its target) — "amount" above is
            # their average, which a display can fall back to when this is
            # null rather than show a blended number that was never the
            # actual figure for any single set.
            "set_amounts": [float(v) for v in log.set_amounts] if log.set_amounts else None,
        }
        for log, taxonomy_name, custom_name in rows
    ]


def activity_log_date_bounds(session: Session, user_id: str) -> tuple[date | None, date | None]:
    """(earliest, latest) logged date, or (None, None) if nothing's logged yet.
    Built for date references that aren't pure calendar math from today ("my
    first day", "when did I start") — those need the real earliest date, not
    a guess, the same way a weekday name ("Thursday") needs the real day of
    the week today falls on rather than the model inventing one."""
    row = session.execute(
        select(func.min(ActivityLog.local_date), func.max(ActivityLog.local_date))
        .where(ActivityLog.user_id == user_id, ActivityLog.implausible.is_(False))
    ).one()
    return row[0], row[1]


# --- Suggestions + videos --------------------------------------------------

def _dashboard_slice(session: Session, user_id: str
                     ) -> tuple[list[dict], set[str], str, set[str], set[str]]:
    """The activities the dashboard actually shows, and the tier band used to
    decide it. Shared by suggestions_today (renders them) and list_catalogue
    (offers everything ELSE) so the two can never quietly disagree about what
    "already on your dashboard" means.

    Tier slicing governs `template` entries only. Anything the user put there
    themselves — via the catalogue (`manual`) or an override (`user_override`)
    — is always visible regardless of tier; a deliberate add should never be
    silently swallowed by a beginner-tier cap the user has already stepped
    past for that one activity.
    """
    full_program = get_program(session, user_id, include_suspended=True)
    program = [p for p in full_program if p["status"] == "active"]
    prof = session.get(UserProfile, user_id)
    band = prof.experience_band if prof else "beginner"

    # A manual removal (remove_program_entry) marks the row suspended rather
    # than deleting it, specifically so it can be excluded here WITHOUT ever
    # being backfilled. _tier_slice is run as if the removal never happened
    # (the removed row is temporarily added back into consideration just for
    # this calculation), then subtracted straight out of the result - this
    # is what actually cancels the backfill, whatever the tier's cap is.
    #
    # A plain "trim N off the tail" correction (tried first, wrong) over-
    # corrects the moment nothing was being backfilled in the first place -
    # e.g. the "experienced" band has no cap at all, or the activity pool
    # has simply run out - and ends up dropping one extra, unrelated
    # activity on top of the one actually removed.
    user_removed = set(session.scalars(
        select(UserProgram.activity_type).where(
            UserProgram.user_id == user_id, UserProgram.status == "suspended",
            UserProgram.suspended_reason == "user_removed")
    ).all())
    program_for_tier = [p for p in full_program
                        if p["status"] == "active" or p["activity_type"] in user_removed]
    default_codes, optional_codes = _tier_slice(program_for_tier, band)
    default_codes -= user_removed
    optional_codes -= user_removed

    self_added = {p["activity_type"] for p in program
                  if p["source"] in ("manual", "user_override")}
    return program, default_codes | optional_codes | self_added, band, default_codes, optional_codes


def suggestions_today(session: Session, user_id: str, limit: int = 3) -> dict:
    if is_referred(session, user_id):
        return {"referred": True, "message": REFERRAL_MESSAGE, "items": []}

    program, visible, band, default_codes, optional_codes = _dashboard_slice(session, user_id)
    # Latest log per activity today, not just "did it happen": a log from
    # before this program entry's most recent add/re-add (see add_program_entry)
    # must not count as completing today's fresh instance of it.
    #
    # Found live (code audit): this query didn't exclude flagged-implausible
    # logs, unlike every sibling aggregation in this file (streak, trend,
    # activity_log_detail) — a single garbage entry (e.g. a 9000km walk) could
    # mark today's goal "completed" while streak() correctly ignored the same
    # log and reported 0, contradicting itself in the same API response.
    # Mirrors streak()'s own exception: an `extraordinary` log still counts as
    # completing today's goal, same reasoning as there.
    last_logged = dict(session.execute(
        select(ActivityLog.activity_type, func.max(ActivityLog.logged_at)).where(
            ActivityLog.user_id == user_id,
            ActivityLog.local_date == today_ist(),
            ActivityLog.activity_type.is_not(None),
            or_(ActivityLog.implausible.is_(False), ActivityLog.extraordinary.is_(True)))
        .group_by(ActivityLog.activity_type)
    ).all())
    added_at_by_type = dict(session.execute(
        select(UserProgram.activity_type, UserProgram.added_at).where(
            UserProgram.user_id == user_id, UserProgram.status == "active")
    ).all())

    videos = {v.activity_type: v for v in session.scalars(select(VideoCatalog)).all()}

    def tier_of(activity_type: str) -> str:
        if activity_type in optional_codes:
            return "optional"
        if activity_type in default_codes:
            return "default"
        return "added"  # self-added, visible beyond what this tier normally shows

    items = []
    for p in program:
        if p["activity_type"] not in visible:
            continue
        v = videos.get(p["activity_type"])
        logged_at = last_logged.get(p["activity_type"])
        added_at = added_at_by_type.get(p["activity_type"])
        completed = (logged_at is not None and added_at is not None
                     and logged_at >= added_at)
        items.append({**p, "completed": completed,
                      # `or None`: every uncurated row is seeded with
                      # watch_url="" (the column is NOT NULL, see
                      # catalog/loader.py's seed_videos), and no video has
                      # been curated yet — so this returned "" for every
                      # card, against the documented "string or null"
                      # contract. A client checking `video_url !== null`
                      # rendered a dead play button on every exercise.
                      # video_library() already normalises this the same way.
                      "video_url": (v.watch_url or None) if v else None,
                      "tier": tier_of(p["activity_type"])})

    excluded_count = len(_blocked_activities(session, user_id))
    tier_note = (f"{excluded_count} exercise(s) left out based on your health conditions"
                 if excluded_count else None)

    # Surface what is still outstanding first; within that, most useful to
    # THIS user's own conditions first — the same usefulness_rank the tier
    # slice uses, not a flat generic order.
    items.sort(key=lambda i: (i["completed"], i["usefulness_rank"], i["priority_order"]))
    return {"referred": False, "message": None, "items": items[:limit],
            "all_items": items, "tier": band, "tier_note": tier_note}


# Product decision: walking and running need no demonstration video — unlike
# the rehab/mobility moves here, everyone already knows how to do them, and
# every row in this catalogue is an uncurated placeholder today anyway
# (see data/README.md), so showing a real-looking "Walking — demonstration"
# card for these two is actively misleading rather than just unfinished.
NO_VIDEO_NEEDED = {"walk", "running"}


def video_library(session: Session, user_id: str) -> dict:
    if is_referred(session, user_id):
        # 6.3 — the referral gate applies on every exercise-content path.
        return {"referred": True, "message": REFERRAL_MESSAGE, "videos": []}
    blocked = set(_blocked_activities(session, user_id)) | NO_VIDEO_NEEDED
    rows = session.execute(
        select(VideoCatalog, ActivityTaxonomy)
        .join(ActivityTaxonomy,
              ActivityTaxonomy.activity_type == VideoCatalog.activity_type)
        .order_by(ActivityTaxonomy.display_name)
    ).all()
    return {"referred": False, "message": None, "videos": [{
        "activity_type": v.activity_type,
        "display_name": t.display_name,
        "title": v.title,
        "video_id": v.youtube_video_id or None,
        "curated": bool(v.youtube_video_id),
        "watch_url": (f"https://www.youtube.com/watch?v={v.youtube_video_id}"
                      if v.youtube_video_id else None),
        "prescription_type": t.prescription_type,
        "per_side": t.per_side,
        "demo_images": t.demo_images,
        "demo_gif": t.demo_gif,
        "demo_gif_source": t.demo_gif_source,
        "instructions": t.instructions,
    } for v, t in rows if v.activity_type not in blocked]}


_EXERCISEDB_DATA = Path(__file__).resolve().parents[2] / "data"
_exercise_library_cache: list[dict] | None = None


def exercise_library(session: Session, user_id: str) -> dict:
    """Reference browse list built from the ExerciseDB bulk import (see
    scripts/dev_tools/fetch_full_exercisedb.py). Only exercises we actually
    have a downloaded GIF for are included -- the RapidAPI free-tier quota
    means not all 1300+ exercises have one yet; re-running that script next
    billing cycle fills in more over time.

    A managed account (child/student/patient on someone else's plan) has no
    use for a self-service exercise browser — their plan is built for them,
    same reasoning as video_library's is_referred gate above."""
    if is_managed_account(session, user_id):
        raise Forbidden("Ask the person managing your plan about this.")
    global _exercise_library_cache
    if _exercise_library_cache is None:
        catalog_path = _EXERCISEDB_DATA / "exercisedb_catalog.json"
        progress_path = _EXERCISEDB_DATA / "exercisedb_fetch_progress.json"
        catalog = json.loads(catalog_path.read_text()) if catalog_path.exists() else []
        fetched_ids = (set(json.loads(progress_path.read_text())["fetched_ids"])
                       if progress_path.exists() else set())
        _exercise_library_cache = sorted((
            {"id": ex["id"], "name": ex["name"].title(),
             "body_part": ex.get("bodyPart"), "target": ex.get("target"),
             "equipment": ex.get("equipment"),
             "instructions": ex.get("instructions", []),
             "gif_url": f"/static/exercisedb_gifs/{ex['id']}.gif"}
            for ex in catalog if ex["id"] in fetched_ids
        ), key=lambda e: e["name"])
    return {"count": len(_exercise_library_cache), "exercises": _exercise_library_cache}


def set_video(session: Session, activity_type: str, url_or_id: str,
              title: str | None = None) -> dict:
    """Admin curation (7.1). Accepts a full YouTube URL or a bare id."""
    row = session.scalar(
        select(VideoCatalog).where(VideoCatalog.activity_type == activity_type))
    if row is None:
        raise NotFound(f"No catalogue row for {activity_type}")

    vid = _extract_video_id(url_or_id)
    if not vid:
        raise ValidationError(
            "Could not read a YouTube video id from that. Paste a watch URL, a "
            "youtu.be link, or the 11-character id itself."
        )
    row.youtube_video_id = vid
    row.watch_url = f"https://www.youtube.com/watch?v={vid}"
    if title:
        row.title = title
    return {"activity_type": activity_type, "video_id": vid}


def _extract_video_id(value: str) -> str | None:
    """Pull the id out of any of the shapes a person is likely to paste."""
    import re
    value = (value or "").strip()
    if not value:
        return None
    patterns = [
        r"(?:youtube\.com/watch\?(?:.*&)?v=)([A-Za-z0-9_-]{11})",
        r"(?:youtu\.be/)([A-Za-z0-9_-]{11})",
        r"(?:youtube\.com/embed/)([A-Za-z0-9_-]{11})",
        r"(?:youtube\.com/shorts/)([A-Za-z0-9_-]{11})",
        r"^([A-Za-z0-9_-]{11})$",
    ]
    for pat in patterns:
        m = re.search(pat, value)
        if m:
            return m.group(1)
    return None


def profile_summary(session: Session, user_id: str) -> dict:
    prof = session.get(UserProfile, user_id)
    conds = session.execute(
        select(Condition.condition_code, Condition.display_name, Condition.bucket)
        .join(UserCondition, UserCondition.condition_code == Condition.condition_code)
        .where(UserCondition.user_id == user_id)
    ).all()
    return {
        "onboarded": prof is not None,
        "age": prof.age if prof else None,
        "goal": prof.goal if prof else None,
        "experience_band": prof.experience_band if prof else None,
        "unit_preference": prof.unit_preference if prof else "km",
        "conditions": [{"code": c, "name": n, "bucket": b} for c, n, b in conds],
        "referred": is_referred(session, user_id),
        # Managed plans (MANAGED_PLANS_TEACHER_DOCTOR_PARENT_PLAN.md): a
        # managed person can't also manage others — the frontend hides the
        # "Manage Plans for Others" entry point based on this.
        "is_managed_account": is_managed_account(session, user_id),
    }
