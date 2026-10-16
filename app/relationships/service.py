"""See MANAGED_PLANS_TEACHER_DOCTOR_PARENT_PLAN.md. No account-level manager
role exists — every permission check here is "does an active row in
managed_relationships link this caller to that managed user," never anything
about who the caller generally is.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.orm import Session

from app.platform.config import Config
from app.platform.errors import Conflict, Forbidden, NotFound, ValidationError
from app.relationships.models import (
    ManagedRelationship, PendingCustomExercise, PendingManagedEntry,
)

RELATION_TYPES = {"child", "student", "patient", "other"}


def is_managed_account(session: Session, user_id: str) -> bool:
    """True if someone else is actively managing this account — a managed
    person (child/student/patient/other) must not also be able to create
    plans for OTHER people themselves. Only an ACTIVE relationship counts: if
    it's ever revoked, this account goes back to being a completely normal
    user, free to use every feature including this one.
    """
    return session.scalar(
        select(ManagedRelationship.id).where(
            ManagedRelationship.managed_user_id == user_id,
            ManagedRelationship.status == "active",
        ).limit(1)
    ) is not None


def create_invite(session: Session, manager_user_id: str, relation_type: str, name: str) -> dict:
    if relation_type not in RELATION_TYPES:
        raise ValidationError(f"relation_type must be one of {sorted(RELATION_TYPES)}.")
    name = (name or "").strip()
    if not name:
        raise ValidationError("Enter their name.")
    if len(name) > 100:
        raise ValidationError("Name must be 100 characters or fewer.")
    if is_managed_account(session, manager_user_id):
        raise Forbidden(
            "This account is managed by someone else, so it can't create "
            "plans for other people."
        )
    row = ManagedRelationship(
        manager_user_id=manager_user_id, relation_type=relation_type, name=name,
        status="pending", invite_token=secrets.token_urlsafe(32),
    )
    session.add(row)
    session.flush()
    return _out(row)


def list_my_relationships(session: Session, manager_user_id: str) -> list[dict]:
    rows = session.scalars(
        select(ManagedRelationship)
        .where(ManagedRelationship.manager_user_id == manager_user_id)
        .order_by(ManagedRelationship.created_at.desc())
    ).all()
    return [_out(r) for r in rows]


def blocked_activities_for_codes(session: Session, condition_codes: list[str] | None
                                  ) -> dict[str, str]:
    """Same avoid-verdict lookup as app/profile/service.py's
    _blocked_activities, but against a raw list of condition codes instead of
    a real user's saved UserCondition rows — needed because a pending plan's
    staged conditions (ManagedRelationship.pending_condition_codes) belong to
    no account yet, so there's no UserCondition row to join against."""
    from app.catalog.models import ConditionInteractionCell, SafetyGridCell
    if not condition_codes:
        return {}
    out = dict(session.execute(
        select(SafetyGridCell.activity_type, SafetyGridCell.note)
        .where(SafetyGridCell.condition_code.in_(condition_codes),
               SafetyGridCell.verdict == "avoid")
    ).all())
    codes = sorted(set(condition_codes))
    pairs = [(a, b) for i, a in enumerate(codes) for b in codes[i + 1:]]
    if pairs:
        rows = session.execute(
            select(ConditionInteractionCell.activity_type, ConditionInteractionCell.note)
            .where(tuple_(ConditionInteractionCell.condition_a,
                           ConditionInteractionCell.condition_b).in_(pairs),
                   ConditionInteractionCell.verdict == "avoid")
        ).all()
        for activity_type, note in rows:
            out.setdefault(activity_type, note)
    return out


def blocked_activities(session: Session, manager_user_id: str, relationship_id: str
                        ) -> dict[str, str]:
    """What the "Add to their plan" picker greys out — a managed plan never
    offers the override-and-confirm escape hatch the self-service app gives a
    user over their own conditions (add_pending_entry/add_program_entry still
    carry that mechanism for the rare fallback case, see their own
    docstrings, but the UI built on top of this never surfaces it to a
    manager). The only way around a block here is Custom Exercise, which has
    no grid check at all by design — see PendingCustomExercise's docstring."""
    row = session.get(ManagedRelationship, relationship_id)
    if row is None or row.manager_user_id != manager_user_id:
        raise NotFound("No such managed relationship.")
    if row.status == "active" and row.managed_user_id:
        from app.profile.service import _blocked_activities
        return _blocked_activities(session, row.managed_user_id)
    return blocked_activities_for_codes(session, row.pending_condition_codes)


# Experience doesn't change WHICH exercises get suggested (that's purely the
# safety grid's therapeutic_rank) — only how big the starting number is.
# 5 bands collapse to this smooth 1.0-1.6x gradient rather than 2 sharp
# tiers, so a jump from "1m" to "2m" isn't a cliff. beginner always gets the
# activity's plain, unscaled default.
_EXPERIENCE_MULTIPLIER = {"beginner": 1.0, "1m": 1.15, "2m": 1.3, "3m": 1.45, "experienced": 1.6}


def suggested_exercises(session: Session, manager_user_id: str, relationship_id: str) -> list[dict]:
    """What the plan screen offers to one-click add — ONLY activities the
    safety grid has flagged as actually therapeutic for at least one of the
    relevant conditions (SafetyGridCell.therapeutic_rank), scaled by
    experience level. A condition with no therapeutic_rank entries at all
    (most of the 12 built-in activities don't treat most conditions — see
    that column's own docstring) simply contributes nothing here, same as
    "no suggestions" rather than a fallback to generic ones.

    Works at both stages of the relationship, matching blocked_activities'
    own split: before the link is claimed, off the manager's staged
    pending_condition_codes/pending_experience_band; after, off the managed
    person's REAL UserCondition rows and UserProfile.experience_band — the
    pending fields are cleared at claim time (open_invite), so this is the
    only data source once active. Excludes whatever's already on the plan —
    re-suggesting something already added would just be clutter.
    """
    from app.catalog.models import ActivityTaxonomy, SafetyGridCell

    row = session.get(ManagedRelationship, relationship_id)
    if row is None or row.manager_user_id != manager_user_id:
        raise NotFound("No such managed relationship.")

    if row.status == "active" and row.managed_user_id:
        from app.profile.models import UserCondition, UserProfile, UserProgram
        codes = list(session.scalars(
            select(UserCondition.condition_code)
            .where(UserCondition.user_id == row.managed_user_id)
        ).all())
        profile = session.get(UserProfile, row.managed_user_id)
        experience_band = profile.experience_band if profile else None
        already_staged = set(session.scalars(
            select(UserProgram.activity_type)
            .where(UserProgram.user_id == row.managed_user_id, UserProgram.status == "active")
        ).all())
    else:
        codes = row.pending_condition_codes
        experience_band = row.pending_experience_band
        already_staged = set(session.scalars(
            select(PendingManagedEntry.activity_type)
            .where(PendingManagedEntry.relationship_id == relationship_id)
        ).all())
    if not codes:
        return []

    # Best (lowest) therapeutic_rank across all relevant conditions for the
    # same activity — mirrors app.profile.service._usefulness_ranks exactly,
    # just against therapeutic_rank instead of usefulness_rank.
    ranked = session.execute(
        select(SafetyGridCell.activity_type, func.min(SafetyGridCell.therapeutic_rank))
        .where(SafetyGridCell.condition_code.in_(codes),
               SafetyGridCell.verdict != "avoid",
               SafetyGridCell.therapeutic_rank.is_not(None))
        .group_by(SafetyGridCell.activity_type)
    ).all()
    if not ranked:
        return []

    from app.progression.service import _round_reps
    multiplier = _EXPERIENCE_MULTIPLIER.get(experience_band or "beginner", 1.0)
    taxonomy = {t.activity_type: t for t in session.scalars(select(ActivityTaxonomy)).all()}

    out = []
    for activity_type, rank in sorted(ranked, key=lambda r: r[1]):
        if activity_type in already_staged:
            continue
        t = taxonomy.get(activity_type)
        if t is None:
            continue
        scaled = float(t.default_amount) * multiplier
        suggested_amount = (_round_reps(scaled) if t.prescription_type in ("reps", "hold")
                            else round(scaled, 1))
        out.append({
            "activity_type": activity_type, "display_name": t.display_name,
            "therapeutic_rank": rank,
            "suggested_sets": 1 if t.prescription_type in ("distance", "duration") else t.default_sets,
            "suggested_amount": suggested_amount, "amount_unit": t.amount_unit,
            "prescription_type": t.prescription_type,
        })
    return out


def add_pending_entry(session: Session, manager_user_id: str, relationship_id: str, *,
                      activity_type: str, target_sets: int, target_amount: float,
                      auto_progress: bool, override_confirmed: bool = False) -> dict:
    """The reordering this feature actually needs: a manager builds out the
    plan BEFORE the link is ever shared, so the moment it's claimed, the
    person sees it already set up — not an empty account waiting on the
    manager to come back and fill it in. Only usable before the invite is
    claimed; once someone's linked, add_program_entry (the normal path) is
    used directly against their real account instead.

    Fully validated here, not deferred to claim time — a mistake must be
    caught now, in front of the manager who made it, never surfacing instead
    to the unrelated person claiming the link later.

    Found live: an activity staged here against an avoid-verdict cell for
    whatever conditions were already staged via set_pending_profile used to
    pass silently, then get SILENTLY DROPPED later at open_invite time (the
    real add_program_entry call there correctly refuses it, and
    _materialize_pending_entries just skips it with no one ever told) — a
    manager could stage two activities, share the link, and the person would
    quietly end up with only one, no warning anywhere. Fixed by running the
    exact same safety-grid check here, at staging time, in front of the
    manager who can actually resolve it — same
    needs_confirmation/override_confirmed shape add_program_entry already
    uses, so the frontend's existing confirm-dialog handles both identically.
    """
    from app.catalog.models import ActivityTaxonomy, Condition, SafetyGridCell  # local:
    # avoids importing app.catalog's model layer at module level purely for
    # these lookups.

    row = session.get(ManagedRelationship, relationship_id)
    if row is None or row.manager_user_id != manager_user_id:
        raise NotFound("No such managed relationship.")
    if row.status != "pending":
        raise Conflict(
            "This invite has already been claimed — add activities to their "
            "real plan directly instead."
        )

    t = session.get(ActivityTaxonomy, activity_type)
    if t is None:
        raise NotFound(f"Unknown activity: {activity_type}")
    if round(target_amount, 2) <= 0:
        raise ValidationError("Amount must be greater than zero.")
    if target_sets < 1:
        raise ValidationError("Sets must be at least 1.")
    if t.prescription_type in ("distance", "duration") and target_sets != 1:
        raise ValidationError(f"{t.display_name} is a single effort, not sets.")
    if t.prescription_type == "reps":
        from app.progression.service import _round_reps  # local: see
        # app/profile/service.py's set_managed_target for why (import cycle).
        target_amount = _round_reps(target_amount)

    if row.pending_condition_codes and not override_confirmed:
        cells = session.execute(
            select(SafetyGridCell.verdict, Condition.display_name)
            .join(Condition, Condition.condition_code == SafetyGridCell.condition_code)
            .where(SafetyGridCell.condition_code.in_(row.pending_condition_codes),
                   SafetyGridCell.activity_type == activity_type,
                   SafetyGridCell.verdict == "avoid")
        ).all()
        if cells:
            from app.profile.service import _override_warning  # local: see
            # add_program_entry's own warning text — identical wording,
            # single canonical place it's written (14.7).
            return {
                "added": False, "needs_confirmation": True, "verdict": "avoid",
                "condition_code": None, "condition_name": cells[0].display_name,
                "warning_text": _override_warning(cells[0].display_name),
            }

    existing = session.scalar(
        select(PendingManagedEntry).where(
            PendingManagedEntry.relationship_id == relationship_id,
            PendingManagedEntry.activity_type == activity_type,
        )
    )
    if existing is not None:
        existing.target_sets = target_sets
        existing.target_amount = target_amount
        existing.auto_progress = auto_progress
        existing.override_confirmed = override_confirmed
    else:
        existing = PendingManagedEntry(
            relationship_id=relationship_id, activity_type=activity_type,
            target_sets=target_sets, target_amount=target_amount,
            auto_progress=auto_progress, override_confirmed=override_confirmed,
        )
        session.add(existing)
    session.flush()
    return {
        "added": True, "needs_confirmation": False,
        "activity_type": existing.activity_type, "target_sets": existing.target_sets,
        "target_amount": float(existing.target_amount), "auto_progress": existing.auto_progress,
    }


def list_pending_entries(session: Session, manager_user_id: str,
                         relationship_id: str) -> list[dict]:
    row = session.get(ManagedRelationship, relationship_id)
    if row is None or row.manager_user_id != manager_user_id:
        raise NotFound("No such managed relationship.")
    rows = session.scalars(
        select(PendingManagedEntry).where(PendingManagedEntry.relationship_id == relationship_id)
    ).all()
    return [
        {"activity_type": e.activity_type, "target_sets": e.target_sets,
         "target_amount": float(e.target_amount), "auto_progress": e.auto_progress}
        for e in rows
    ]


def remove_pending_entry(session: Session, manager_user_id: str, relationship_id: str,
                         activity_type: str) -> None:
    row = session.get(ManagedRelationship, relationship_id)
    if row is None or row.manager_user_id != manager_user_id:
        raise NotFound("No such managed relationship.")
    entry = session.scalar(
        select(PendingManagedEntry).where(
            PendingManagedEntry.relationship_id == relationship_id,
            PendingManagedEntry.activity_type == activity_type,
        )
    )
    if entry is not None:
        session.delete(entry)


def set_pending_profile(session: Session, manager_user_id: str, relationship_id: str, *,
                        age: int, condition_codes: list[str],
                        experience_band: str | None = None) -> dict:
    """The manager answers the onboarding questions (age, health conditions,
    experience level) on the managed person's behalf, WHILE BUILDING THE PLAN
    — confirmed in conversation: never asked of the managed person
    themselves, and only collected at all if the plan needs it (an
    auto-progressing activity needs real condition data for the safety grid;
    an all-fixed plan never calls this). Consumed exactly once, the first
    time the link is opened — see open_invite below. experience_band also
    drives suggested_exercises' suggested numbers (bigger for someone with
    more experience) before the link is ever opened.
    """
    from app.catalog.models import Condition  # local: see add_pending_entry's
    # own ActivityTaxonomy import for why (avoids a module-level import of
    # app.catalog purely for one lookup).
    from app.profile.service import BANDS, MAX_AGE

    row = session.get(ManagedRelationship, relationship_id)
    if row is None or row.manager_user_id != manager_user_id:
        raise NotFound("No such managed relationship.")
    if row.status != "pending":
        raise Conflict("This invite has already been claimed.")

    # Same bare sanity floor as everywhere else now — the app has no 18+
    # age gate at all (see UserProfile's own docstring).
    if not isinstance(age, int) or age < 1 or age > MAX_AGE:
        raise ValidationError(f"Age must be a whole number between 1 and {MAX_AGE}.")
    known = {c.condition_code for c in session.scalars(select(Condition)).all()}
    codes = list(dict.fromkeys(c for c in (condition_codes or []) if c))
    unknown = [c for c in codes if c not in known]
    if unknown:
        raise ValidationError(f"Unknown condition(s): {', '.join(unknown)}")
    if not codes:
        codes = ["none"]
    if experience_band is not None and experience_band not in BANDS:
        raise ValidationError(f"Experience band must be one of: {', '.join(sorted(BANDS))}")

    row.pending_age = age
    row.pending_condition_codes = codes
    row.pending_experience_band = experience_band
    session.flush()
    return {"age": row.pending_age, "condition_codes": row.pending_condition_codes,
            "experience_band": row.pending_experience_band}


def _materialize_pending_entries(session: Session, row: ManagedRelationship) -> None:
    """Called once, right after a claim activates `row` — turns whatever the
    manager staged via add_pending_entry into real program entries for the
    newly-linked person, then clears the staging rows (never read again)."""
    from app.profile import service as profile_service  # local: see
    # get_managed_activity's own docstring for why (import cycle).

    pending = session.scalars(
        select(PendingManagedEntry).where(PendingManagedEntry.relationship_id == row.id)
    ).all()
    for entry in pending:
        result = profile_service.add_program_entry(
            session, row.managed_user_id, entry.activity_type,
            acting_manager_id=row.manager_user_id, auto_progress=entry.auto_progress,
            # The manager already resolved this at staging time if it needed
            # resolving (add_pending_entry runs the same safety-grid check
            # against whatever conditions were staged) — carrying their
            # decision through here is what stops it from being silently
            # re-blocked and dropped with no one told.
            override_confirmed=entry.override_confirmed,
        )
        if result.get("needs_confirmation"):
            # Rare fallback: only reachable if the manager staged this entry
            # before staging any profile at all (nothing to check against
            # yet), and the eventual real conditions turn out to conflict.
            # Never fail the whole claim over this — drop just this one
            # entry; the manager can revisit and confirm it manually from the
            # relationship's own plan screen afterward.
            session.delete(entry)
            continue
        profile_service.set_managed_target(
            session, row.manager_user_id, row.managed_user_id, entry.activity_type,
            target_sets=entry.target_sets, target_amount=float(entry.target_amount),
        )
        session.delete(entry)


def add_pending_custom_exercise(session: Session, manager_user_id: str, relationship_id: str, *,
                                 name: str, measurement_type: str, target_amount: float,
                                 target_sets: int = 1) -> dict:
    """Staging-time twin of app/profile/service.py's create_custom_exercise —
    deliberately runs no safety-grid check at all (see PendingCustomExercise's
    docstring); the only validation here is the same name/target sanity
    checks that function does, re-run for real against the actual new
    account at materialization time (_materialize_pending_custom_exercises)."""
    from app.profile.service import (
        CUSTOM_MEASUREMENT_UNITS, _normalise_exercise_name, _reject_if_matches_taxonomy,
        _reject_unsafe_name,
    )

    row = session.get(ManagedRelationship, relationship_id)
    if row is None or row.manager_user_id != manager_user_id:
        raise NotFound("No such managed relationship.")
    if row.status != "pending":
        raise Conflict(
            "This invite has already been claimed — add a custom exercise to "
            "their real account directly instead."
        )
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
        target_sets = 1
    elif target_sets < 1:
        raise ValidationError("Sets must be at least 1.")
    normalized = _normalise_exercise_name(name)
    _reject_if_matches_taxonomy(session, normalized, name)
    existing = session.scalar(
        select(PendingCustomExercise).where(
            PendingCustomExercise.relationship_id == relationship_id,
            PendingCustomExercise.normalized_name == normalized)
    )
    if existing is not None:
        raise ValidationError(f'Already staged a custom exercise called "{existing.name}".')

    entry = PendingCustomExercise(
        relationship_id=relationship_id, name=name, normalized_name=normalized,
        measurement_type=measurement_type, target_sets=target_sets,
        target_amount=target_amount,
    )
    session.add(entry)
    session.flush()
    return {"id": entry.id, "name": entry.name, "measurement_type": entry.measurement_type,
            "target_sets": entry.target_sets, "target_amount": float(entry.target_amount)}


def list_pending_custom_exercises(session: Session, manager_user_id: str, relationship_id: str
                                   ) -> list[dict]:
    row = session.get(ManagedRelationship, relationship_id)
    if row is None or row.manager_user_id != manager_user_id:
        raise NotFound("No such managed relationship.")
    rows = session.scalars(
        select(PendingCustomExercise)
        .where(PendingCustomExercise.relationship_id == relationship_id)
        .order_by(PendingCustomExercise.created_at)
    ).all()
    return [{"id": e.id, "name": e.name, "measurement_type": e.measurement_type,
             "target_sets": e.target_sets if e.measurement_type == "reps" else None,
             "target_amount": float(e.target_amount)} for e in rows]


def remove_pending_custom_exercise(session: Session, manager_user_id: str, relationship_id: str,
                                    custom_exercise_id: str) -> None:
    row = session.get(ManagedRelationship, relationship_id)
    if row is None or row.manager_user_id != manager_user_id:
        raise NotFound("No such managed relationship.")
    entry = session.get(PendingCustomExercise, custom_exercise_id)
    if entry is None or entry.relationship_id != relationship_id:
        raise NotFound("No such staged custom exercise.")
    session.delete(entry)


def _materialize_pending_custom_exercises(session: Session, row: ManagedRelationship) -> None:
    """Twin of _materialize_pending_entries, for the custom-exercise staging
    table — runs the real create_custom_exercise validation against the
    now-real account. Never fails the whole claim over one bad entry (e.g. a
    staged name that collides with something created some other way in the
    interim); that entry is just dropped silently, same as the equivalent
    fallback above."""
    from app.profile.service import create_custom_exercise

    pending = session.scalars(
        select(PendingCustomExercise).where(
            PendingCustomExercise.relationship_id == row.id)
    ).all()
    for entry in pending:
        try:
            create_custom_exercise(
                session, row.managed_user_id, name=entry.name,
                measurement_type=entry.measurement_type,
                target_sets=entry.target_sets, target_amount=float(entry.target_amount),
                acting_manager_id=row.manager_user_id,
            )
        except (ValidationError, Conflict):
            pass
        session.delete(entry)


DEFAULT_MANAGED_GOAL = "health"
DEFAULT_MANAGED_EXPERIENCE_BAND = "beginner"


def open_invite(session: Session, cfg: Config, invite_token: str, device_id: str):
    """The actual mechanism confirmed in conversation: the managed person
    (child/student/patient/other) never signs up or logs in — opening the
    link itself grants access. A real account still gets created (so they
    count as a genuine user, same as everyone else), it's just created HERE,
    silently, the first time the link is opened, instead of through a
    signup form.

    - First open (relationship still 'pending'): creates the account, runs
      onboarding on their behalf using whatever the manager staged via
      set_pending_profile (or sensible defaults if the plan never needed
      that — see below), materializes the staged activities, and returns a
      real token pair for the brand-new account.
    - Later opens (already 'active'): same account, just issues a fresh
      token pair — e.g. a second device, or the same device after signing
      out. Nothing is re-created.
    - Revoked: refused outright. This is the ONLY thing that ever cuts off
      access — the link has no expiry and no password step of its own.

    Returns (TokenPair, User, ManagedRelationship).
    """
    from app.auth import tokens as tk
    from app.auth.models import User
    from app.auth.passwords import hash_password
    from app.profile import service as profile_service

    # FOR UPDATE: two near-simultaneous opens of the same brand-new link (a
    # double-click, two tabs, a retried request) must not both see
    # status='pending' and each create their own duplicate account — same
    # concurrency hazard, same fix, as rotate_refresh_token's own row lock
    # (app/auth/tokens.py).
    row = session.scalar(
        select(ManagedRelationship)
        .where(ManagedRelationship.invite_token == invite_token)
        .with_for_update()
    )
    if row is None:
        raise NotFound("Unknown invite link.")
    if row.status == "revoked":
        raise Forbidden("This link has been revoked by whoever created it.")

    if row.status == "pending":
        # Never guessable, never shown anywhere, never used to log in through
        # the normal password form — this account is only ever reachable
        # through this link (until revoked) or, later, a real password reset.
        placeholder_email = f"managed+{secrets.token_hex(12)}@managed.vitainspire.internal"
        user = User(email=placeholder_email, password_hash=hash_password(secrets.token_urlsafe(24)))
        session.add(user)
        session.flush()

        profile_service.complete_onboarding(
            session, user.id,
            age=row.pending_age if row.pending_age is not None else 18,
            goal=DEFAULT_MANAGED_GOAL,
            experience_band=row.pending_experience_band or DEFAULT_MANAGED_EXPERIENCE_BAND,
            condition_codes=row.pending_condition_codes or ["none"],
            build_default_program=False, min_age=1,
        )
        row.pending_age = None
        row.pending_condition_codes = None
        row.pending_experience_band = None

        row.managed_user_id = user.id
        row.status = "active"
        row.accepted_at = datetime.now(timezone.utc)
        session.flush()
        _materialize_pending_entries(session, row)
        _materialize_pending_custom_exercises(session, row)
    else:
        user = session.get(User, row.managed_user_id)
        if user is None:
            raise NotFound("The linked account no longer exists.")

    access, access_exp, _ = tk.issue_access_token(cfg, user.id)
    raw_refresh, refresh_row = tk.issue_refresh_token(session, cfg, user.id, device_id)
    pair = tk.TokenPair(access, raw_refresh, access_exp, refresh_row.expires_at)
    return pair, user, row


def revoke_relationship(session: Session, manager_user_id: str, relationship_id: str, hard_delete: bool = False) -> None:
    row = session.get(ManagedRelationship, relationship_id)
    if row is None or row.manager_user_id != manager_user_id:
        raise NotFound("No such managed relationship.")
    if hard_delete or row.status == "revoked":
        session.delete(row)
        return
    row.status = "revoked"
    row.revoked_at = datetime.now(timezone.utc)


def clear_revoked_relationships(session: Session, manager_user_id: str) -> int:
    stmt = (
        delete(ManagedRelationship)
        .where(
            ManagedRelationship.manager_user_id == manager_user_id,
            ManagedRelationship.status == "revoked",
        )
    )
    res = session.execute(stmt)
    return res.rowcount or 0


def require_active_relationship(session: Session, manager_user_id: str,
                                managed_user_id: str) -> ManagedRelationship:
    """The one check every other managed-plan write (add_program_entry,
    the target-edit endpoint, the monitoring endpoint) must call before
    acting on someone else's account."""
    row = session.scalar(
        select(ManagedRelationship).where(
            ManagedRelationship.manager_user_id == manager_user_id,
            ManagedRelationship.managed_user_id == managed_user_id,
            ManagedRelationship.status == "active",
        )
    )
    if row is None:
        raise Forbidden("No active managed relationship with this user.")
    return row


def get_managed_activity(session: Session, manager_user_id: str, relationship_id: str) -> dict:
    """§6 of the plan doc: "what are they actually doing" — reuses every
    piece of math app/profile/service.py already has; the only new work is
    this authorization-gated assembly. Local import of profile.service: that
    module imports require_active_relationship from here, so a module-level
    import back would be circular.
    """
    from datetime import timedelta

    from app.profile import service as profile_service

    row = session.get(ManagedRelationship, relationship_id)
    if row is None or row.manager_user_id != manager_user_id:
        raise NotFound("No such managed relationship.")
    if row.status != "active" or row.managed_user_id is None:
        raise Forbidden("This relationship is not active.")
    managed_user_id = row.managed_user_id

    today = profile_service.today_ist()
    managed_entries = session.scalars(
        select(profile_service.UserProgram).where(
            profile_service.UserProgram.user_id == managed_user_id,
            profile_service.UserProgram.managed_by_relationship_id == row.id,
        )
    ).all()
    out = {
        "name": row.name,
        "relation_type": row.relation_type,
        "managed_user_id": managed_user_id,
        "streak": profile_service.streak(session, managed_user_id),
        "suggestions_today": profile_service.suggestions_today(session, managed_user_id),
        "weekly_volume": profile_service.weekly_volume(session, managed_user_id),
        "recent_activity": profile_service.activity_log_detail(
            session, managed_user_id,
            start_date=today - timedelta(days=30), end_date=today,
        ),
        # The entries THIS relationship is actually responsible for — what
        # the manager-facing UI lists for inline editing. Everything else on
        # the managed user's dashboard (self-added, or another manager's)
        # intentionally isn't surfaced here.
        "managed_entries": [
            {"activity_type": e.activity_type, "target_sets": e.target_sets,
             "target_amount": float(e.target_amount), "auto_progress": e.auto_progress}
            for e in managed_entries
        ],
    }
    if row.relation_type == "patient":
        from app.profile.models import OverrideConsent
        consents = session.scalars(
            select(OverrideConsent)
            .where(OverrideConsent.user_id == managed_user_id)
            .order_by(OverrideConsent.created_at.desc())
        ).all()
        out["safety_overrides"] = [
            {"activity_type": c.activity_type, "condition_code": c.condition_code,
             "grid_verdict": c.grid_verdict, "warning_text": c.warning_text,
             "created_at": c.created_at.isoformat()}
            for c in consents
        ]
    return out


def _out(row: ManagedRelationship) -> dict:
    return {
        "id": row.id, "manager_user_id": row.manager_user_id,
        "managed_user_id": row.managed_user_id, "name": row.name,
        "relation_type": row.relation_type,
        "status": row.status, "invite_token": row.invite_token,
        "created_at": row.created_at.isoformat(),
        "accepted_at": row.accepted_at.isoformat() if row.accepted_at else None,
        "pending_age": row.pending_age,
        "pending_condition_codes": row.pending_condition_codes,
        "pending_experience_band": row.pending_experience_band,
    }
