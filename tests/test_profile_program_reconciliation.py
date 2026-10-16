"""Regression tests for build_program's condition-change reconciliation
(app/profile/service.py), fixed twice this session:

1. It used to unconditionally wipe and rebuild the whole program from
   taxonomy defaults every time it ran — silently resetting weeks of earned
   progression on every OTHER exercise any time a condition changed, even one
   unrelated to that exercise. Fixed to only touch entries whose safety
   verdict actually changed.

2. Found live (fourth-pass audit): that fix only ever moved active ->
   suspended when a condition newly blocked an entry — nothing ever reversed
   it. Remove the blocking condition later, and the entry stayed suspended
   forever with no automatic path back. Fixed to also resurrect a
   suspended_reason="condition_safety" entry (a SYSTEM suspension) back to
   active, with its prior target intact, once it's no longer blocked — while
   leaving a suspended_reason="user_removed" entry (the user's OWN
   remove_program_entry call) untouched regardless of condition changes,
   since a manual removal must never be silently backfilled.
"""
from app.platform.db import session_scope
from app.profile import service as profile_service
from app.profile.models import UserProgram
from app.progression.service import apply_escalation

ACTIVITY = "hamstring_stretch"  # avoid-verdicted for herniated_disk


def _onboard(session, user_id, condition_codes):
    profile_service.complete_onboarding(
        session, user_id, age=30, goal="health", experience_band="beginner",
        condition_codes=condition_codes, unit_preference="km",
    )


def test_removing_the_blocking_condition_resurrects_the_entry_with_its_progress(make_user, force_avoid_cell):
    force_avoid_cell("herniated_disk", ACTIVITY)
    user = make_user(condition_codes=["none"])
    with session_scope() as s:
        apply_escalation(s, user["user_id"], ACTIVITY, 5.0)
        progressed_amount = s.get(UserProgram, (user["user_id"], ACTIVITY)).target_amount

    with session_scope() as s:
        _onboard(s, user["user_id"], ["herniated_disk"])
        entry = s.get(UserProgram, (user["user_id"], ACTIVITY))
        assert entry.status == "suspended"
        assert entry.suspended_reason == "condition_safety"
        assert entry.target_amount == progressed_amount  # untouched by the suspension itself

    with session_scope() as s:
        _onboard(s, user["user_id"], ["none"])
        entry = s.get(UserProgram, (user["user_id"], ACTIVITY))
        assert entry.status == "active"
        assert entry.suspended_reason is None
        assert entry.target_amount == progressed_amount  # restored, not reset to baseline


def test_a_manually_removed_entry_is_never_resurrected_by_a_condition_change(make_user):
    user = make_user(condition_codes=["none"])
    with session_scope() as s:
        profile_service.remove_program_entry(s, user["user_id"], ACTIVITY)

    # A condition cycle that WOULD resurrect a system-suspended entry must
    # have no effect on a user-removed one.
    with session_scope() as s:
        _onboard(s, user["user_id"], ["herniated_disk"])
    with session_scope() as s:
        _onboard(s, user["user_id"], ["none"])

    with session_scope() as s:
        entry = s.get(UserProgram, (user["user_id"], ACTIVITY))
        assert entry.status == "suspended"
        assert entry.suspended_reason == "user_removed"
