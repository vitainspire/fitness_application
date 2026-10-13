"""See MANAGED_PLANS_TEACHER_DOCTOR_PARENT_PLAN.md. No account-level manager
role exists — every permission check here is "does an active row in
managed_relationships link this caller to that managed user," never anything
about who the caller generally is.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.platform.errors import Conflict, Forbidden, NotFound, ValidationError
from app.relationships.models import ManagedRelationship, PendingManagedEntry

RELATION_TYPES = {"child", "student", "patient", "other"}


def create_invite(session: Session, manager_user_id: str, relation_type: str) -> dict:
    if relation_type not in RELATION_TYPES:
        raise ValidationError(f"relation_type must be one of {sorted(RELATION_TYPES)}.")
    row = ManagedRelationship(
        manager_user_id=manager_user_id, relation_type=relation_type,
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


def add_pending_entry(session: Session, manager_user_id: str, relationship_id: str, *,
                      activity_type: str, target_sets: int, target_amount: float,
                      auto_progress: bool) -> dict:
    """The reordering this feature actually needs: a manager builds out the
    plan BEFORE the link is ever shared, so the moment it's claimed, the
    person sees it already set up — not an empty account waiting on the
    manager to come back and fill it in. Only usable before the invite is
    claimed; once someone's linked, add_program_entry (the normal path) is
    used directly against their real account instead.

    Fully validated here, not deferred to claim time — a mistake must be
    caught now, in front of the manager who made it, never surfacing instead
    to the unrelated person claiming the link later.
    """
    from app.catalog.models import ActivityTaxonomy  # local: avoids importing
    # app.profile's model layer at module level purely for one lookup.

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
    else:
        existing = PendingManagedEntry(
            relationship_id=relationship_id, activity_type=activity_type,
            target_sets=target_sets, target_amount=target_amount,
            auto_progress=auto_progress,
        )
        session.add(existing)
    session.flush()
    return {
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
        )
        if result.get("needs_confirmation"):
            # The person reported real conditions (during onboarding, which
            # happens after claiming — see route()'s own ordering) that make
            # this specific staged activity an avoid-verdict conflict the
            # manager couldn't have known about when they set it up, since no
            # conditions existed yet. Never fail the whole claim over this —
            # drop just this one entry; the manager can revisit and confirm
            # it manually from the relationship's own plan screen afterward.
            session.delete(entry)
            continue
        profile_service.set_managed_target(
            session, row.manager_user_id, row.managed_user_id, entry.activity_type,
            target_sets=entry.target_sets, target_amount=float(entry.target_amount),
        )
        session.delete(entry)


def claim_invite(session: Session, invite_token: str, managed_user_id: str) -> dict:
    """Single-use: the atomic UPDATE ... WHERE status='pending' means two
    tabs or a retried request can't double-claim the same token, same spirit
    as the refresh-token rotation's row lock elsewhere in this codebase."""
    now = datetime.now(timezone.utc)
    result = session.execute(
        update(ManagedRelationship)
        .where(ManagedRelationship.invite_token == invite_token,
               ManagedRelationship.status == "pending")
        .values(managed_user_id=managed_user_id, status="active", accepted_at=now)
    )
    if result.rowcount == 0:
        existing = session.scalar(
            select(ManagedRelationship).where(ManagedRelationship.invite_token == invite_token)
        )
        if existing is None:
            raise NotFound("Unknown or expired invite link.")
        raise Conflict("This invite link has already been used.")
    session.flush()
    row = session.scalar(
        select(ManagedRelationship).where(ManagedRelationship.invite_token == invite_token)
    )
    _materialize_pending_entries(session, row)
    return _out(row)


def revoke_relationship(session: Session, manager_user_id: str, relationship_id: str) -> None:
    row = session.get(ManagedRelationship, relationship_id)
    if row is None or row.manager_user_id != manager_user_id:
        raise NotFound("No such managed relationship.")
    if row.status == "revoked":
        return  # idempotent — revoking twice is not an error
    row.status = "revoked"
    row.revoked_at = datetime.now(timezone.utc)


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
        "managed_user_id": row.managed_user_id, "relation_type": row.relation_type,
        "status": row.status, "invite_token": row.invite_token,
        "created_at": row.created_at.isoformat(),
        "accepted_at": row.accepted_at.isoformat() if row.accepted_at else None,
    }
