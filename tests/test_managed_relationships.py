"""Managed plans (teacher/doctor/parent) — see
MANAGED_PLANS_TEACHER_DOCTOR_PARENT_PLAN.md. No account-level manager role
exists; every test here exercises the real permission boundary: an active
row in managed_relationships, nothing about who a user generally is.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.platform.db import session_scope
from app.platform.errors import Conflict, Forbidden, NotFound
from app.profile import service as profile_service
from app.profile.service import today_ist
from app.progression.service import apply_escalation, run_weekly_progression
from app.relationships import service as rel_service


def _backdate(session, user_id, activity_type, weeks=1):
    entry = session.get(profile_service.UserProgram, (user_id, activity_type))
    entry.added_at = datetime.now(timezone.utc) - timedelta(days=weeks * 7 + 1)
    session.flush()


def test_invite_lifecycle_create_claim_list(make_user):
    manager = make_user(condition_codes=["none"])
    managed = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student")
        assert invite["status"] == "pending"
        assert invite["managed_user_id"] is None

        claimed = rel_service.claim_invite(s, invite["invite_token"], managed["user_id"])
        assert claimed["status"] == "active"
        assert claimed["managed_user_id"] == managed["user_id"]

        rels = rel_service.list_my_relationships(s, manager["user_id"])
        assert len(rels) == 1
        assert rels[0]["id"] == invite["id"]


def test_claiming_the_same_token_twice_fails(make_user):
    manager = make_user(condition_codes=["none"])
    managed = make_user(condition_codes=["none"])
    other = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "child")
        rel_service.claim_invite(s, invite["invite_token"], managed["user_id"])
        with pytest.raises(Conflict):
            rel_service.claim_invite(s, invite["invite_token"], other["user_id"])


def test_claiming_an_unknown_token_is_not_found(make_user):
    user = make_user(condition_codes=["none"])
    with session_scope() as s:
        with pytest.raises(NotFound):
            rel_service.claim_invite(s, "not-a-real-token", user["user_id"])


def test_revoke_only_the_manager_can_revoke(make_user):
    manager = make_user(condition_codes=["none"])
    managed = make_user(condition_codes=["none"])
    stranger = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "patient")
        rel_service.claim_invite(s, invite["invite_token"], managed["user_id"])
        with pytest.raises(NotFound):
            rel_service.revoke_relationship(s, stranger["user_id"], invite["id"])
        rel_service.revoke_relationship(s, manager["user_id"], invite["id"])
        with pytest.raises(Forbidden):
            rel_service.require_active_relationship(s, manager["user_id"], managed["user_id"])


def test_manager_can_add_entry_to_managed_users_program(make_user):
    manager = make_user(condition_codes=["none"])
    managed = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student")
        rel_service.claim_invite(s, invite["invite_token"], managed["user_id"])

        result = profile_service.add_program_entry(
            s, managed["user_id"], "walk", acting_manager_id=manager["user_id"],
            auto_progress=True,
        )
        assert result["added"] is True
        entry = s.get(profile_service.UserProgram, (managed["user_id"], "walk"))
        assert entry.managed_by_relationship_id == invite["id"]
        assert entry.auto_progress is True


def test_manager_without_a_relationship_is_forbidden(make_user):
    manager = make_user(condition_codes=["none"])
    managed = make_user(condition_codes=["none"])  # never invited/linked
    with session_scope() as s:
        with pytest.raises(Forbidden):
            profile_service.add_program_entry(
                s, managed["user_id"], "walk", acting_manager_id=manager["user_id"],
            )


def test_second_manager_is_blocked_not_merged(make_user):
    doctor = make_user(condition_codes=["none"])
    parent = make_user(condition_codes=["none"])
    patient = make_user(condition_codes=["none"])
    with session_scope() as s:
        doc_invite = rel_service.create_invite(s, doctor["user_id"], "patient")
        rel_service.claim_invite(s, doc_invite["invite_token"], patient["user_id"])
        profile_service.add_program_entry(
            s, patient["user_id"], "walk", acting_manager_id=doctor["user_id"],
        )

        parent_invite = rel_service.create_invite(s, parent["user_id"], "child")
        rel_service.claim_invite(s, parent_invite["invite_token"], patient["user_id"])
        with pytest.raises(Conflict):
            profile_service.add_program_entry(
                s, patient["user_id"], "walk", acting_manager_id=parent["user_id"],
            )


def test_manager_can_manually_set_a_target(make_user):
    manager = make_user(condition_codes=["none"])
    managed = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student")
        rel_service.claim_invite(s, invite["invite_token"], managed["user_id"])
        profile_service.add_program_entry(
            s, managed["user_id"], "walk", acting_manager_id=manager["user_id"],
        )
        result = profile_service.set_managed_target(
            s, manager["user_id"], managed["user_id"], "walk",
            target_sets=1, target_amount=3.0,
        )
        assert result["target_amount"] == 3.0
        entry = s.get(profile_service.UserProgram, (managed["user_id"], "walk"))
        assert float(entry.target_amount) == 3.0


def test_manual_target_rounds_reps_to_whole_numbers(make_user):
    manager = make_user(condition_codes=["none"])
    managed = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student")
        rel_service.claim_invite(s, invite["invite_token"], managed["user_id"])
        profile_service.add_program_entry(
            s, managed["user_id"], "glute_bridge", acting_manager_id=manager["user_id"],
        )
        result = profile_service.set_managed_target(
            s, manager["user_id"], managed["user_id"], "glute_bridge",
            target_sets=2, target_amount=12.6,
        )
        assert result["target_amount"] == 13.0  # half-up whole number, not 12.6


def test_manager_cannot_set_target_on_an_entry_they_dont_manage(make_user):
    manager = make_user(condition_codes=["none"])
    managed = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student")
        rel_service.claim_invite(s, invite["invite_token"], managed["user_id"])
        # managed user adds "walk" to their OWN plan themselves — not manager-owned.
        profile_service.add_program_entry(s, managed["user_id"], "walk")
        with pytest.raises(NotFound):
            profile_service.set_managed_target(
                s, manager["user_id"], managed["user_id"], "walk",
                target_sets=1, target_amount=5.0,
            )


def test_revoked_relationship_entry_is_untouched_and_keeps_working(make_user):
    """§5's worked example: revoking changes nothing about the entry itself —
    it just becomes an ordinary part of the managed user's own program."""
    doctor = make_user(condition_codes=["none"])
    patient = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, doctor["user_id"], "patient")
        rel_service.claim_invite(s, invite["invite_token"], patient["user_id"])
        profile_service.add_program_entry(
            s, patient["user_id"], "walk", acting_manager_id=doctor["user_id"],
            auto_progress=True,
        )
        rel_service.revoke_relationship(s, doctor["user_id"], invite["id"])

        entry = s.get(profile_service.UserProgram, (patient["user_id"], "walk"))
        assert entry.auto_progress is True  # unchanged by revocation
        assert entry.managed_by_relationship_id == invite["id"]  # historical record kept

        # The doctor can no longer manage it — no active relationship at all
        # anymore, so this is Forbidden, not NotFound (which is for "the
        # relationship is active but doesn't own this specific entry").
        with pytest.raises(Forbidden):
            profile_service.set_managed_target(
                s, doctor["user_id"], patient["user_id"], "walk",
                target_sets=1, target_amount=5.0,
            )
        # ...but the patient's own program/suggestions still work normally.
        program = profile_service.get_program(s, patient["user_id"])
        assert any(p["activity_type"] == "walk" for p in program)


def test_auto_progress_false_is_excluded_from_the_weekly_job(make_user):
    """The teacher's "keep stretching fixed" example from the plan doc —
    auto_progress=False must behave exactly like the existing
    source='user_override' exclusion: never touched by the weekly job."""
    manager = make_user(condition_codes=["none"])
    managed = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student")
        rel_service.claim_invite(s, invite["invite_token"], managed["user_id"])
        profile_service.add_program_entry(
            s, managed["user_id"], "glute_bridge", acting_manager_id=manager["user_id"],
            auto_progress=False,
        )
        entry = s.get(profile_service.UserProgram, (managed["user_id"], "glute_bridge"))
        target_before = float(entry.target_amount)
        _backdate(s, managed["user_id"], "glute_bridge", weeks=1)
        profile_service.log_activity(s, managed["user_id"], activity_type="glute_bridge",
                                      amount=20, sets=2)
        s.flush()
        rows = run_weekly_progression(s, managed["user_id"], today=today_ist())
        assert all(r.activity_type != "glute_bridge" for r in rows)
        entry = s.get(profile_service.UserProgram, (managed["user_id"], "glute_bridge"))
        assert float(entry.target_amount) == target_before  # unchanged, still fixed


def test_auto_progress_false_blocks_chat_escalation_too(make_user):
    manager = make_user(condition_codes=["none"])
    managed = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student")
        rel_service.claim_invite(s, invite["invite_token"], managed["user_id"])
        profile_service.add_program_entry(
            s, managed["user_id"], "glute_bridge", acting_manager_id=manager["user_id"],
            auto_progress=False,
        )
        with pytest.raises(NotFound):
            apply_escalation(s, managed["user_id"], "glute_bridge", proposed_pct_increase=0.10)


def test_auto_progress_true_still_progresses_normally_for_a_managed_entry(make_user):
    """The teacher's "auto-upgrade running" half of the same example — a
    managed entry with auto_progress=True must progress exactly like any
    normal self-managed entry, proving the new flag only ever narrows
    eligibility, never grants anything extra."""
    manager = make_user(condition_codes=["none"])
    managed = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student")
        rel_service.claim_invite(s, invite["invite_token"], managed["user_id"])
        profile_service.add_program_entry(
            s, managed["user_id"], "glute_bridge", acting_manager_id=manager["user_id"],
            auto_progress=True,
        )
        _backdate(s, managed["user_id"], "glute_bridge", weeks=1)
        profile_service.log_activity(s, managed["user_id"], activity_type="glute_bridge",
                                      amount=20, sets=2)
        s.flush()
        rows = run_weekly_progression(s, managed["user_id"], today=today_ist())
        assert any(r.activity_type == "glute_bridge" for r in rows)


def test_manager_can_create_a_custom_exercise_for_the_managed_user(make_user):
    manager = make_user(condition_codes=["none"])
    managed = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "child")
        rel_service.claim_invite(s, invite["invite_token"], managed["user_id"])
        result = profile_service.create_custom_exercise(
            s, managed["user_id"], name="Trampoline", measurement_types=["reps"],
            acting_manager_id=manager["user_id"],
        )
        assert result["name"] == "Trampoline"
        row = s.get(profile_service.CustomExercise, result["id"])
        assert row.user_id == managed["user_id"]  # owned by the managed user, not the manager


def test_manager_without_a_relationship_cannot_create_a_custom_exercise(make_user):
    manager = make_user(condition_codes=["none"])
    managed = make_user(condition_codes=["none"])  # never invited/linked
    with session_scope() as s:
        with pytest.raises(Forbidden):
            profile_service.create_custom_exercise(
                s, managed["user_id"], name="Trampoline", measurement_types=["reps"],
                acting_manager_id=manager["user_id"],
            )


def test_pending_entries_are_materialized_on_claim(make_user):
    """The actual flow requested: build the plan BEFORE sharing the link, so
    the person sees it already set up the moment they claim it."""
    manager = make_user(condition_codes=["none"])
    managed = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student")

        rel_service.add_pending_entry(
            s, manager["user_id"], invite["id"], activity_type="walk",
            target_sets=1, target_amount=2.0, auto_progress=True,
        )
        rel_service.add_pending_entry(
            s, manager["user_id"], invite["id"], activity_type="glute_bridge",
            target_sets=2, target_amount=15.0, auto_progress=False,
        )
        pending = rel_service.list_pending_entries(s, manager["user_id"], invite["id"])
        assert {e["activity_type"] for e in pending} == {"walk", "glute_bridge"}

        # Claiming must not require any further action from the manager.
        rel_service.claim_invite(s, invite["invite_token"], managed["user_id"])

        walk = s.get(profile_service.UserProgram, (managed["user_id"], "walk"))
        assert walk is not None
        assert float(walk.target_amount) == 2.0
        assert walk.auto_progress is True
        assert walk.managed_by_relationship_id == invite["id"]

        bridge = s.get(profile_service.UserProgram, (managed["user_id"], "glute_bridge"))
        assert bridge is not None
        assert float(bridge.target_amount) == 15.0
        assert bridge.auto_progress is False

        # Staging rows are cleared after materializing — never read again.
        assert rel_service.list_pending_entries(s, manager["user_id"], invite["id"]) == []


def test_pending_entry_rounds_reps_to_whole_numbers(make_user):
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student")
        result = rel_service.add_pending_entry(
            s, manager["user_id"], invite["id"], activity_type="glute_bridge",
            target_sets=2, target_amount=12.6, auto_progress=True,
        )
        assert result["target_amount"] == 13.0


def test_cannot_add_pending_entries_once_the_invite_is_claimed(make_user):
    manager = make_user(condition_codes=["none"])
    managed = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student")
        rel_service.claim_invite(s, invite["invite_token"], managed["user_id"])
        with pytest.raises(Conflict):
            rel_service.add_pending_entry(
                s, manager["user_id"], invite["id"], activity_type="walk",
                target_sets=1, target_amount=2.0, auto_progress=True,
            )


def test_stranger_cannot_add_pending_entries_to_someone_elses_invite(make_user):
    manager = make_user(condition_codes=["none"])
    stranger = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student")
        with pytest.raises(NotFound):
            rel_service.add_pending_entry(
                s, stranger["user_id"], invite["id"], activity_type="walk",
                target_sets=1, target_amount=2.0, auto_progress=True,
            )


def test_pending_entry_can_be_removed_before_claim(make_user):
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student")
        rel_service.add_pending_entry(
            s, manager["user_id"], invite["id"], activity_type="walk",
            target_sets=1, target_amount=2.0, auto_progress=True,
        )
        rel_service.remove_pending_entry(s, manager["user_id"], invite["id"], "walk")
        assert rel_service.list_pending_entries(s, manager["user_id"], invite["id"]) == []


def test_monitoring_endpoint_reuses_existing_math(make_user):
    manager = make_user(condition_codes=["none"])
    managed = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student")
        rel_service.claim_invite(s, invite["invite_token"], managed["user_id"])
        profile_service.add_program_entry(
            s, managed["user_id"], "walk", acting_manager_id=manager["user_id"],
        )
        profile_service.log_activity(s, managed["user_id"], activity_type="walk", amount=2, sets=1)

        result = rel_service.get_managed_activity(s, manager["user_id"], invite["id"])
        assert result["relation_type"] == "student"
        assert "streak" in result and "suggestions_today" in result
        assert any(a["name"] == "Walking" for a in result["recent_activity"])
        assert result["managed_entries"] == [
            {"activity_type": "walk", "target_sets": 1, "target_amount": 1.0,
             "auto_progress": True}
        ]


def test_monitoring_endpoint_blocked_for_a_non_manager(make_user):
    manager = make_user(condition_codes=["none"])
    managed = make_user(condition_codes=["none"])
    stranger = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student")
        rel_service.claim_invite(s, invite["invite_token"], managed["user_id"])
        with pytest.raises(NotFound):
            rel_service.get_managed_activity(s, stranger["user_id"], invite["id"])


def test_monitoring_endpoint_blocked_after_revocation(make_user):
    manager = make_user(condition_codes=["none"])
    managed = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "patient")
        rel_service.claim_invite(s, invite["invite_token"], managed["user_id"])
        rel_service.revoke_relationship(s, manager["user_id"], invite["id"])
        with pytest.raises(Forbidden):
            rel_service.get_managed_activity(s, manager["user_id"], invite["id"])


def test_monitoring_endpoint_surfaces_safety_overrides_for_a_patient(make_user):
    """The doctor-specific addition from §6: avoid-verdict overrides should
    be visible in the patient's monitoring view."""
    doctor = make_user(condition_codes=["none"])
    patient = make_user(condition_codes=["herniated_disk"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, doctor["user_id"], "patient")
        rel_service.claim_invite(s, invite["invite_token"], patient["user_id"])
        # hamstring_stretch is an actual avoid-verdict cell for herniated_disk
        # (same fixture used elsewhere in the test suite for this reason).
        profile_service.add_program_entry(
            s, patient["user_id"], "hamstring_stretch",
            override_confirmed=True, acting_manager_id=doctor["user_id"],
        )
        result = rel_service.get_managed_activity(s, doctor["user_id"], invite["id"])
        assert "safety_overrides" in result
        assert any(o["activity_type"] == "hamstring_stretch" for o in result["safety_overrides"])
