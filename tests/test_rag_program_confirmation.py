"""Tests for the "add these suggested exercises to your program?" feature
requested by the user directly (not from an automated audit): after the
rehab chatbot suggests exercises for a sport, a confirmation tap should add
whichever aren't already in the user's program, and hide (never delete)
anything currently active that ISN'T in the new suggestion — so switching
focus to a new sport doesn't destroy progress on something the user might
want back later.
"""
from app.platform.db import session_scope
from app.profile import service as profile_service
from app.profile.models import UserProgram
from app.progression.service import apply_escalation
from app.rag import service as rag_service

ALL_ACTIVITY_TYPES = {
    "walk", "running", "glute_bridge", "dead_bug", "ankle_circles", "wrist_circles",
    "shoulder_circles", "neck_isometric", "hamstring_stretch", "quad_stretch",
    "calf_stretch", "lower_back_side_stretch",
}


def test_confirming_a_subset_hides_the_rest_without_deleting_them(make_user):
    user = make_user(condition_codes=["none"])
    keep = ["walk", "running", "glute_bridge"]

    with session_scope() as s:
        result = rag_service._confirm_program_update(
            s, user["user_id"], {"activity_types": keep, "confirmed": True},
        )
    active = {p["activity_type"] for p in result["program"]}
    assert active == set(keep)

    # Everything else must still exist in the database, just hidden — never
    # actually deleted.
    with session_scope() as s:
        hidden_rows = s.query(UserProgram).filter(
            UserProgram.user_id == user["user_id"],
            UserProgram.activity_type.in_(ALL_ACTIVITY_TYPES - set(keep)),
        ).all()
        assert len(hidden_rows) == len(ALL_ACTIVITY_TYPES) - len(keep)
        for row in hidden_rows:
            assert row.status == "suspended"
            assert row.suspended_reason == "sport_focus_change"


def test_confirming_preserves_progress_on_kept_exercises(make_user):
    user = make_user(condition_codes=["none"])
    with session_scope() as s:
        apply_escalation(s, user["user_id"], "glute_bridge", 5.0)
        progressed_amount = s.get(UserProgram, (user["user_id"], "glute_bridge")).target_amount

    with session_scope() as s:
        rag_service._confirm_program_update(
            s, user["user_id"], {"activity_types": ["glute_bridge", "walk"], "confirmed": True},
        )

    with session_scope() as s:
        entry = s.get(UserProgram, (user["user_id"], "glute_bridge"))
        assert entry.status == "active"
        assert entry.target_amount == progressed_amount  # not reset to baseline


def test_re_confirming_a_previously_hidden_exercise_restores_it_not_a_duplicate(make_user):
    user = make_user(condition_codes=["none"])
    with session_scope() as s:
        rag_service._confirm_program_update(
            s, user["user_id"], {"activity_types": ["walk"], "confirmed": True},
        )
    with session_scope() as s:
        entry = s.get(UserProgram, (user["user_id"], "glute_bridge"))
        assert entry.status == "suspended"

    with session_scope() as s:
        result = rag_service._confirm_program_update(
            s, user["user_id"], {"activity_types": ["walk", "glute_bridge"], "confirmed": True},
        )
    active = {p["activity_type"] for p in result["program"]}
    assert active == {"walk", "glute_bridge"}


def test_a_user_override_entry_is_never_auto_hidden(make_user, force_avoid_cell):
    """Same protection build_program already gives a deliberate, informed
    user choice — a sport-focus confirmation must not silently reverse it."""
    force_avoid_cell("herniated_disk", "hamstring_stretch")
    user = make_user(condition_codes=["herniated_disk"])  # forced avoid-verdict hamstring_stretch
    with session_scope() as s:
        r = profile_service.add_program_entry(s, user["user_id"], "hamstring_stretch")
        assert r["needs_confirmation"] is True
        profile_service.add_program_entry(
            s, user["user_id"], "hamstring_stretch", override_confirmed=True,
        )
        entry = s.get(UserProgram, (user["user_id"], "hamstring_stretch"))
        assert entry.source == "user_override"

    with session_scope() as s:
        rag_service._confirm_program_update(
            s, user["user_id"], {"activity_types": ["walk"], "confirmed": True},
        )

    with session_scope() as s:
        entry = s.get(UserProgram, (user["user_id"], "hamstring_stretch"))
        assert entry.status == "active"  # untouched, not hidden


def test_confirmation_rejects_an_activity_not_currently_approved(make_user):
    """16.3's principle: re-verify against the CURRENT approved list, never
    trust a client-supplied activity_types list at face value — a condition
    could have changed since the chat answer was shown."""
    user = make_user(condition_codes=["sprain_strain"])  # only "walk" is safe
    with session_scope() as s:
        result = rag_service._confirm_program_update(
            s, user["user_id"],
            {"activity_types": ["walk", "running", "glute_bridge"], "confirmed": True},
        )
    active = {p["activity_type"] for p in result["program"]}
    assert active == {"walk"}  # running/glute_bridge silently dropped, not approved


def test_declining_makes_no_changes(make_user):
    user = make_user(condition_codes=["none"])
    with session_scope() as s:
        result = rag_service._confirm_program_update(
            s, user["user_id"], {"activity_types": ["walk"], "confirmed": False},
        )
    assert result["program_updated"] is False
    with session_scope() as s:
        active = {
            p.activity_type for p in s.query(UserProgram).filter(
                UserProgram.user_id == user["user_id"], UserProgram.status == "active",
            ).all()
        }
    assert active == ALL_ACTIVITY_TYPES  # still the full default program, untouched


def test_message_mentions_every_confirmed_exercise_even_if_already_active(make_user):
    """Found live (user report): "it suggested 5, only 4 got added" — every
    suggested exercise WAS correctly active afterward; the confirmation
    message just never mentioned the one that was already in the program
    before this confirmation ran, since it only reported what CHANGED.
    Silently omitting an exercise from the message is indistinguishable from
    it having failed, even though the underlying data was never wrong."""
    user = make_user(condition_codes=["none"])  # starts with ALL 12 already active
    confirmed = ["walk", "running", "glute_bridge"]

    with session_scope() as s:
        result = rag_service._confirm_program_update(
            s, user["user_id"], {"activity_types": confirmed, "confirmed": True},
        )

    # Every one of these was ALREADY active (none needed adding) — the old
    # message would have said nothing about any of them.
    for name in ["Walking", "Running", "Glute Bridge"]:
        assert name in result["message"], (
            f"{name!r} was already active but never mentioned in: {result['message']!r}"
        )
