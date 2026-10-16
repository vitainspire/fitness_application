"""Managed plans (teacher/doctor/parent) — see
MANAGED_PLANS_TEACHER_DOCTOR_PARENT_PLAN.md. No account-level manager role
exists; every test here exercises the real permission boundary: an active
row in managed_relationships, nothing about who a user generally is.

The managed person (child/student/patient/other) never signs up or logs in —
opening the invite link (open_invite) silently creates their real account the
first time, and just re-issues tokens for it on every later open, until the
manager revokes it. That's the mechanism every test below exercises.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.platform.config import load_config
from app.platform.db import session_scope
from app.platform.errors import Conflict, Forbidden, NotFound, ValidationError
from app.profile import service as profile_service
from app.profile.service import today_ist
from app.progression.service import apply_escalation, run_weekly_progression
from app.relationships import service as rel_service


def _cfg():
    return load_config()


def _open(session, invite_token, device="test-device"):
    """Opens the link exactly like the real /v1/relationships/open route
    does, returning just the resulting managed user's id — what almost every
    test here actually needs."""
    pair, user, row = rel_service.open_invite(session, _cfg(), invite_token, device)
    return user.id


def _backdate(session, user_id, activity_type, weeks=1):
    entry = session.get(profile_service.UserProgram, (user_id, activity_type))
    entry.added_at = datetime.now(timezone.utc) - timedelta(days=weeks * 7 + 1)
    session.flush()


def test_invite_lifecycle_create_open_list(make_user):
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student", "Priya")
        assert invite["status"] == "pending"
        assert invite["managed_user_id"] is None
        assert invite["name"] == "Priya"

        managed_id = _open(s, invite["invite_token"])
        assert managed_id is not None

        rels = rel_service.list_my_relationships(s, manager["user_id"])
        assert len(rels) == 1
        assert rels[0]["id"] == invite["id"]
        assert rels[0]["status"] == "active"
        assert rels[0]["managed_user_id"] == managed_id
        assert rels[0]["name"] == "Priya"  # so a manager with several can tell them apart


def test_name_is_required_to_create_an_invite(make_user):
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        with pytest.raises(ValidationError):
            rel_service.create_invite(s, manager["user_id"], "student", "")
        with pytest.raises(ValidationError):
            rel_service.create_invite(s, manager["user_id"], "student", "   ")


def test_opening_the_same_link_twice_reuses_the_same_account(make_user):
    """The actual design: the link is permanent — opening it again (a second
    device, or the same device after signing out) must return the SAME
    account, never create a second one or raise Conflict."""
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "child", "Test Name")
        first_id = _open(s, invite["invite_token"], device="device-1")
        second_id = _open(s, invite["invite_token"], device="device-2")
        assert first_id == second_id


def test_opening_an_unknown_token_is_not_found(make_user):
    with session_scope() as s:
        with pytest.raises(NotFound):
            rel_service.open_invite(s, _cfg(), "not-a-real-token", "test-device")


def test_revoked_link_can_no_longer_be_opened(make_user):
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "patient", "Test Name")
        managed_id = _open(s, invite["invite_token"])
        rel_service.revoke_relationship(s, manager["user_id"], invite["id"])
        with pytest.raises(Forbidden):
            rel_service.open_invite(s, _cfg(), invite["invite_token"], "test-device")
        # The account itself still exists and isn't touched — only future
        # opens of the link are refused.
        assert s.get(profile_service.UserProfile, managed_id) is not None


def test_revoke_only_the_manager_can_revoke(make_user):
    manager = make_user(condition_codes=["none"])
    stranger = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "patient", "Test Name")
        managed_id = _open(s, invite["invite_token"])
        with pytest.raises(NotFound):
            rel_service.revoke_relationship(s, stranger["user_id"], invite["id"])
        rel_service.revoke_relationship(s, manager["user_id"], invite["id"])
        with pytest.raises(Forbidden):
            rel_service.require_active_relationship(s, manager["user_id"], managed_id)


def test_manager_can_add_entry_to_managed_users_program(make_user):
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student", "Test Name")
        managed_id = _open(s, invite["invite_token"])

        result = profile_service.add_program_entry(
            s, managed_id, "walk", acting_manager_id=manager["user_id"],
            auto_progress=True,
        )
        assert result["added"] is True
        entry = s.get(profile_service.UserProgram, (managed_id, "walk"))
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
    with session_scope() as s:
        doc_invite = rel_service.create_invite(s, doctor["user_id"], "patient", "Test Name")
        patient_id = _open(s, doc_invite["invite_token"])
        profile_service.add_program_entry(
            s, patient_id, "walk", acting_manager_id=doctor["user_id"],
        )

        # A second, independent relationship onto a DIFFERENT auto-created
        # account can't target the same real person without the app knowing
        # they're the same account — this exercises the block using two
        # distinct managed accounts' shared activity_type collision isn't
        # possible by construction here, so instead confirm directly via the
        # service call with the same managed_id as both "invites" pointing
        # at it, simulating two managers who happen to manage the same
        # underlying account (e.g. a future multi-manager scenario).
        parent_invite = rel_service.create_invite(s, parent["user_id"], "child", "Test Name")
        # Simulate the parent's invite already being linked to the SAME
        # patient (the realistic case this guards: two managers, one real
        # person) by claiming straight onto that id at the DB level.
        from app.relationships.models import ManagedRelationship
        row = s.get(ManagedRelationship, parent_invite["id"])
        row.managed_user_id = patient_id
        row.status = "active"
        s.flush()

        with pytest.raises(Conflict):
            profile_service.add_program_entry(
                s, patient_id, "walk", acting_manager_id=parent["user_id"],
            )


def test_manager_can_manually_set_a_target(make_user):
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student", "Test Name")
        managed_id = _open(s, invite["invite_token"])
        profile_service.add_program_entry(
            s, managed_id, "walk", acting_manager_id=manager["user_id"],
        )
        result = profile_service.set_managed_target(
            s, manager["user_id"], managed_id, "walk",
            target_sets=1, target_amount=3.0,
        )
        assert result["target_amount"] == 3.0
        entry = s.get(profile_service.UserProgram, (managed_id, "walk"))
        assert float(entry.target_amount) == 3.0


def test_manual_target_rounds_reps_to_whole_numbers(make_user):
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student", "Test Name")
        managed_id = _open(s, invite["invite_token"])
        profile_service.add_program_entry(
            s, managed_id, "glute_bridge", acting_manager_id=manager["user_id"],
        )
        result = profile_service.set_managed_target(
            s, manager["user_id"], managed_id, "glute_bridge",
            target_sets=2, target_amount=12.6,
        )
        assert result["target_amount"] == 13.0  # half-up whole number, not 12.6


def test_manager_cannot_set_target_on_an_entry_they_dont_manage(make_user):
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student", "Test Name")
        managed_id = _open(s, invite["invite_token"])
        # managed user adds "walk" to their OWN plan themselves — not manager-owned.
        profile_service.add_program_entry(s, managed_id, "walk")
        with pytest.raises(NotFound):
            profile_service.set_managed_target(
                s, manager["user_id"], managed_id, "walk",
                target_sets=1, target_amount=5.0,
            )


def test_revoked_relationship_entry_is_untouched_and_keeps_working(make_user):
    """§5's worked example: revoking changes nothing about the entry itself —
    it just becomes an ordinary part of the managed user's own program."""
    doctor = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, doctor["user_id"], "patient", "Test Name")
        patient_id = _open(s, invite["invite_token"])
        profile_service.add_program_entry(
            s, patient_id, "walk", acting_manager_id=doctor["user_id"],
            auto_progress=True,
        )
        rel_service.revoke_relationship(s, doctor["user_id"], invite["id"])

        entry = s.get(profile_service.UserProgram, (patient_id, "walk"))
        assert entry.auto_progress is True  # unchanged by revocation
        assert entry.managed_by_relationship_id == invite["id"]  # historical record kept

        # The doctor can no longer manage it — no active relationship at all
        # anymore, so this is Forbidden, not NotFound (which is for "the
        # relationship is active but doesn't own this specific entry").
        with pytest.raises(Forbidden):
            profile_service.set_managed_target(
                s, doctor["user_id"], patient_id, "walk",
                target_sets=1, target_amount=5.0,
            )
        # ...but the patient's own program/suggestions still work normally.
        program = profile_service.get_program(s, patient_id)
        assert any(p["activity_type"] == "walk" for p in program)


def test_auto_progress_false_is_excluded_from_the_weekly_job(make_user):
    """The teacher's "keep stretching fixed" example from the plan doc —
    auto_progress=False must behave exactly like the existing
    source='user_override' exclusion: never touched by the weekly job."""
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student", "Test Name")
        managed_id = _open(s, invite["invite_token"])
        profile_service.add_program_entry(
            s, managed_id, "glute_bridge", acting_manager_id=manager["user_id"],
            auto_progress=False,
        )
        entry = s.get(profile_service.UserProgram, (managed_id, "glute_bridge"))
        target_before = float(entry.target_amount)
        _backdate(s, managed_id, "glute_bridge", weeks=1)
        profile_service.log_activity(s, managed_id, activity_type="glute_bridge",
                                      amount=20, sets=2)
        s.flush()
        rows = run_weekly_progression(s, managed_id, today=today_ist())
        assert all(r.activity_type != "glute_bridge" for r in rows)
        entry = s.get(profile_service.UserProgram, (managed_id, "glute_bridge"))
        assert float(entry.target_amount) == target_before  # unchanged, still fixed


def test_auto_progress_false_blocks_chat_escalation_too(make_user):
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student", "Test Name")
        managed_id = _open(s, invite["invite_token"])
        profile_service.add_program_entry(
            s, managed_id, "glute_bridge", acting_manager_id=manager["user_id"],
            auto_progress=False,
        )
        with pytest.raises(NotFound):
            apply_escalation(s, managed_id, "glute_bridge", proposed_pct_increase=0.10)


def test_auto_progress_true_still_progresses_normally_for_a_managed_entry(make_user):
    """The teacher's "auto-upgrade running" half of the same example — a
    managed entry with auto_progress=True must progress exactly like any
    normal self-managed entry, proving the new flag only ever narrows
    eligibility, never grants anything extra."""
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student", "Test Name")
        managed_id = _open(s, invite["invite_token"])
        profile_service.add_program_entry(
            s, managed_id, "glute_bridge", acting_manager_id=manager["user_id"],
            auto_progress=True,
        )
        _backdate(s, managed_id, "glute_bridge", weeks=1)
        profile_service.log_activity(s, managed_id, activity_type="glute_bridge",
                                      amount=20, sets=2)
        s.flush()
        rows = run_weekly_progression(s, managed_id, today=today_ist())
        assert any(r.activity_type == "glute_bridge" for r in rows)


def test_manager_can_create_a_custom_exercise_for_the_managed_user(make_user):
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "child", "Test Name")
        managed_id = _open(s, invite["invite_token"])
        result = profile_service.create_custom_exercise(
            s, managed_id, name="Trampoline", measurement_type="reps", target_amount=10,
            acting_manager_id=manager["user_id"],
        )
        assert result["name"] == "Trampoline"
        row = s.get(profile_service.CustomExercise, result["id"])
        assert row.user_id == managed_id  # owned by the managed user, not the manager


def test_manager_without_a_relationship_cannot_create_a_custom_exercise(make_user):
    manager = make_user(condition_codes=["none"])
    managed = make_user(condition_codes=["none"])  # never invited/linked
    with session_scope() as s:
        with pytest.raises(Forbidden):
            profile_service.create_custom_exercise(
                s, managed["user_id"], name="Trampoline", measurement_type="reps", target_amount=10,
                acting_manager_id=manager["user_id"],
            )


def test_pending_entries_are_materialized_on_open(make_user):
    """The actual flow requested: build the plan BEFORE sharing the link, so
    the person sees it already set up the moment the link is opened."""
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student", "Test Name")

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

        # Opening the link must not require any further action from the manager.
        managed_id = _open(s, invite["invite_token"])

        walk = s.get(profile_service.UserProgram, (managed_id, "walk"))
        assert walk is not None
        assert float(walk.target_amount) == 2.0
        assert walk.auto_progress is True
        assert walk.managed_by_relationship_id == invite["id"]

        bridge = s.get(profile_service.UserProgram, (managed_id, "glute_bridge"))
        assert bridge is not None
        assert float(bridge.target_amount) == 15.0
        assert bridge.auto_progress is False

        # Staging rows are cleared after materializing — never read again.
        assert rel_service.list_pending_entries(s, manager["user_id"], invite["id"]) == []


def test_pending_entry_rounds_reps_to_whole_numbers(make_user):
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student", "Test Name")
        result = rel_service.add_pending_entry(
            s, manager["user_id"], invite["id"], activity_type="glute_bridge",
            target_sets=2, target_amount=12.6, auto_progress=True,
        )
        assert result["target_amount"] == 13.0


def test_cannot_add_pending_entries_once_the_invite_is_opened(make_user):
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student", "Test Name")
        _open(s, invite["invite_token"])
        with pytest.raises(Conflict):
            rel_service.add_pending_entry(
                s, manager["user_id"], invite["id"], activity_type="walk",
                target_sets=1, target_amount=2.0, auto_progress=True,
            )


def test_stranger_cannot_add_pending_entries_to_someone_elses_invite(make_user):
    manager = make_user(condition_codes=["none"])
    stranger = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student", "Test Name")
        with pytest.raises(NotFound):
            rel_service.add_pending_entry(
                s, stranger["user_id"], invite["id"], activity_type="walk",
                target_sets=1, target_amount=2.0, auto_progress=True,
            )


def test_pending_entry_can_be_removed_before_open(make_user):
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student", "Test Name")
        rel_service.add_pending_entry(
            s, manager["user_id"], invite["id"], activity_type="walk",
            target_sets=1, target_amount=2.0, auto_progress=True,
        )
        rel_service.remove_pending_entry(s, manager["user_id"], invite["id"], "walk")
        assert rel_service.list_pending_entries(s, manager["user_id"], invite["id"]) == []


def test_pending_profile_is_applied_when_the_link_is_opened(make_user):
    """Confirmed in conversation: the manager answers the onboarding
    questions (age, conditions) while BUILDING the plan — never the managed
    person, and never at link-open time."""
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "child", "Test Name")
        rel_service.set_pending_profile(
            s, manager["user_id"], invite["id"], age=9, condition_codes=["none"],
        )
        managed_id = _open(s, invite["invite_token"])
        profile = s.get(profile_service.UserProfile, managed_id)
        assert profile.age == 9  # the real child's age, not the 18+ adult floor


def test_pending_profile_age_has_no_18_plus_floor(make_user):
    """A parent enters their real child's age — the normal 18+ self-
    registration consent rule must not apply here."""
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "child", "Test Name")
        rel_service.set_pending_profile(
            s, manager["user_id"], invite["id"], age=7, condition_codes=[],
        )
        with pytest.raises(ValidationError):
            rel_service.set_pending_profile(
                s, manager["user_id"], invite["id"], age=0, condition_codes=[],
            )


def test_plan_with_no_pending_profile_still_gets_a_working_account(make_user):
    """An all-fixed plan never needs condition data — opening the link must
    still work, with sensible defaults, not crash for lack of a profile."""
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student", "Test Name")
        rel_service.add_pending_entry(
            s, manager["user_id"], invite["id"], activity_type="walk",
            target_sets=1, target_amount=2.0, auto_progress=False,
        )
        managed_id = _open(s, invite["invite_token"])
        profile = s.get(profile_service.UserProfile, managed_id)
        assert profile is not None
        program = profile_service.get_program(s, managed_id)
        assert any(p["activity_type"] == "walk" for p in program)


def test_managed_account_never_gets_the_generic_default_program(make_user):
    """build_default_program=False: a manager-curated plan must consist of
    exactly what the manager staged, never also the generic tier-based
    program every normal new account gets."""
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student", "Test Name")
        rel_service.add_pending_entry(
            s, manager["user_id"], invite["id"], activity_type="walk",
            target_sets=1, target_amount=2.0, auto_progress=True,
        )
        managed_id = _open(s, invite["invite_token"])
        program = profile_service.get_program(s, managed_id)
        assert [p["activity_type"] for p in program] == ["walk"]


def test_monitoring_endpoint_reuses_existing_math(make_user):
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student", "Test Name")
        managed_id = _open(s, invite["invite_token"])
        profile_service.add_program_entry(
            s, managed_id, "walk", acting_manager_id=manager["user_id"],
        )
        profile_service.log_activity(s, managed_id, activity_type="walk", amount=2, sets=1)

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
    stranger = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student", "Test Name")
        _open(s, invite["invite_token"])
        with pytest.raises(NotFound):
            rel_service.get_managed_activity(s, stranger["user_id"], invite["id"])


def test_monitoring_endpoint_blocked_after_revocation(make_user):
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "patient", "Test Name")
        _open(s, invite["invite_token"])
        rel_service.revoke_relationship(s, manager["user_id"], invite["id"])
        with pytest.raises(Forbidden):
            rel_service.get_managed_activity(s, manager["user_id"], invite["id"])


def test_monitoring_endpoint_surfaces_safety_overrides_for_a_patient(make_user, force_avoid_cell):
    """The doctor-specific addition from §6: avoid-verdict overrides should
    be visible in the patient's monitoring view."""
    force_avoid_cell("herniated_disk", "hamstring_stretch")
    doctor = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, doctor["user_id"], "patient", "Test Name")
        # Staged on the doctor's behalf, same as any other auto-progressing
        # plan — this is what makes hamstring_stretch an avoid-verdict
        # conflict once the account actually exists.
        rel_service.set_pending_profile(
            s, doctor["user_id"], invite["id"], age=40, condition_codes=["herniated_disk"],
        )
        patient_id = _open(s, invite["invite_token"])
        # hamstring_stretch is an actual avoid-verdict cell for herniated_disk
        # (same fixture used elsewhere in the test suite for this reason).
        profile_service.add_program_entry(
            s, patient_id, "hamstring_stretch",
            override_confirmed=True, acting_manager_id=doctor["user_id"],
        )
        result = rel_service.get_managed_activity(s, doctor["user_id"], invite["id"])
        assert "safety_overrides" in result
        assert any(o["activity_type"] == "hamstring_stretch" for o in result["safety_overrides"])


def test_staging_an_avoid_verdict_activity_asks_for_confirmation(make_user, force_avoid_cell):
    """Found live: staging an activity that conflicts with already-staged
    conditions used to succeed silently, then get dropped without warning at
    open_invite time. Must now surface the same confirmation the normal
    add_program_entry flow already uses, at staging time, in front of the
    manager who can actually resolve it."""
    force_avoid_cell("herniated_disk", "hamstring_stretch")
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "patient", "Test Name")
        rel_service.set_pending_profile(
            s, manager["user_id"], invite["id"], age=40, condition_codes=["herniated_disk"],
        )
        result = rel_service.add_pending_entry(
            s, manager["user_id"], invite["id"], activity_type="hamstring_stretch",
            target_sets=2, target_amount=15.0, auto_progress=True,
        )
        assert result["needs_confirmation"] is True
        assert result["added"] is False
        assert "herniated_disk" in result["warning_text"] or "disk" in result["warning_text"].lower() \
            or result["condition_name"]
        # Not staged — the manager hasn't confirmed yet.
        pending = rel_service.list_pending_entries(s, manager["user_id"], invite["id"])
        assert pending == []


def test_confirming_an_avoid_verdict_activity_stages_it_and_survives_open(make_user):
    """The other half: once the manager explicitly confirms, it stages
    normally AND actually ends up in the real plan when the link is opened —
    the decision made here must not be silently re-blocked later."""
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "patient", "Test Name")
        rel_service.set_pending_profile(
            s, manager["user_id"], invite["id"], age=40, condition_codes=["herniated_disk"],
        )
        result = rel_service.add_pending_entry(
            s, manager["user_id"], invite["id"], activity_type="hamstring_stretch",
            target_sets=2, target_amount=15.0, auto_progress=True,
            override_confirmed=True,
        )
        assert result["added"] is True

        patient_id = _open(s, invite["invite_token"])
        entry = s.get(profile_service.UserProgram, (patient_id, "hamstring_stretch"))
        assert entry is not None  # must actually be there, not silently dropped
        assert float(entry.target_amount) == 15.0


def test_managed_account_cannot_create_plans_for_others(make_user):
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "child", "Test Name")
        managed_id = _open(s, invite["invite_token"])
        assert rel_service.is_managed_account(s, managed_id) is True
        with pytest.raises(Forbidden):
            rel_service.create_invite(s, managed_id, "student", "Test Name")


def test_revoking_frees_the_account_to_manage_others_again(make_user):
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "child", "Test Name")
        managed_id = _open(s, invite["invite_token"])
        rel_service.revoke_relationship(s, manager["user_id"], invite["id"])
        assert rel_service.is_managed_account(s, managed_id) is False
        new_invite = rel_service.create_invite(s, managed_id, "student", "Test Name")
        assert new_invite["status"] == "pending"


def test_profile_summary_surfaces_is_managed_account(make_user):
    manager = make_user(condition_codes=["none"])
    normal = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "patient", "Test Name")
        managed_id = _open(s, invite["invite_token"])
        assert profile_service.profile_summary(s, managed_id)["is_managed_account"] is True
        assert profile_service.profile_summary(s, normal["user_id"])["is_managed_account"] is False


def test_managed_account_cannot_delete_itself(make_user):
    """Confirmed directly: only whoever created the link can delete it —
    never the managed person themselves."""
    from app.auth import service as auth_service
    from app.platform.config import load_config

    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "child", "Test Name")
        managed_id = _open(s, invite["invite_token"])
        with pytest.raises(Forbidden):
            # Password is irrelevant here — the managed-account check must
            # fire before any password is even checked.
            auth_service.delete_account(s, load_config(), managed_id, password="whatever")
