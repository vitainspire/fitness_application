"""'undo my changes' puts the plan back to exactly what it was before a chat Switch - not only what the Switch hid, but also what it added.

The real case this came from: the user removed Running, Dead Bug, Glute Bridge and Walking (8 left), the chat offered Glute Bridge and Walking,
the user tapped Switch (2 left, 8 hidden), then "undo my changes" brought the 8 back but left Glute Bridge and Walking, so the plan was 10, not 8.
No AI is involved in the apply/undo steps; only the two "chat says" tests below call the chat endpoint (a free, code-only reply).
"""
from sqlalchemy import select

from app.platform.db import session_scope
from app.profile import service as profile_service
from app.profile.models import UserProgram
from app.rag import service as rag_service


def _active(user):
    with session_scope() as s:
        return {a for (a,) in s.execute(select(UserProgram.activity_type).where(UserProgram.user_id == user["user_id"], UserProgram.status == "active")).all()}


def _entry(user, activity_type):
    with session_scope() as s:
        e = s.get(UserProgram, (user["user_id"], activity_type))
        return None if e is None else (e.status, e.suspended_reason, e.switch_undo)


def _undo(user):
    """What the Undo button does: it sends every candidate the chat offered."""
    with session_scope() as s:
        cands = rag_service._revert_candidates(s, user["user_id"])
        return cands, rag_service._confirm_revert(s, user["user_id"], {"activity_types": [c["activity_type"] for c in cands], "confirmed": True})


def test_undo_after_a_switch_returns_the_original_plan(make_user):
    user = make_user(condition_codes=["none"])
    with session_scope() as s:
        for a in ("running", "dead_bug", "glute_bridge", "walk"):
            profile_service.remove_program_entry(s, user["user_id"], a)
    original = _active(user)
    assert len(original) == 8
    with session_scope() as s:
        profile_service.set_program_to_activities(s, user["user_id"], ["glute_bridge", "walk"])
    assert _active(user) == {"glute_bridge", "walk"}

    cands, result = _undo(user)
    assert {c["activity_type"] for c in cands if c["action"] == "pause"} == {"glute_bridge", "walk"}
    assert result["program_updated"] is True and "Put back as before the switch" in result["message"]
    assert _active(user) == original                                                    # exactly the 8 the user had, not 10
    assert _entry(user, "walk") == ("suspended", "user_removed", None)                  # removed again, as the user left it; marker cleared
    assert _entry(user, "glute_bridge") == ("suspended", "user_removed", None)

    with session_scope() as s:                                                          # a second undo has nothing left to reverse
        assert rag_service._revert_candidates(s, user["user_id"]) == []


def test_undo_pauses_an_exercise_the_switch_created_and_keeps_it_not_deleted(make_user):
    user = make_user(condition_codes=["none"])
    extra = "neck_isometric"
    with session_scope() as s:                                                          # a default plan already holds every exercise: drop one row so it is truly "never in the plan"
        s.delete(s.get(UserProgram, (user["user_id"], extra)))
    original = _active(user)
    assert _entry(user, extra) is None
    with session_scope() as s:
        profile_service.set_program_to_activities(s, user["user_id"], ["walk", extra])
    assert _entry(user, extra) == ("active", None, "new")
    _undo(user)
    assert _active(user) == original
    status, reason, marker = _entry(user, extra)
    assert (status, marker) == ("suspended", None) and reason == "switch_undone"        # kept and paused, not deleted


def test_add_mode_leaves_no_marker_so_undo_has_nothing_to_reverse(make_user):
    user = make_user(condition_codes=["none"])
    with session_scope() as s:
        profile_service.remove_program_entry(s, user["user_id"], "walk")
    with session_scope() as s:
        profile_service.set_program_to_activities(s, user["user_id"], ["walk"], hide_others=False)       # the Add button
    assert _entry(user, "walk") == ("active", None, None)
    with session_scope() as s:
        assert rag_service._revert_candidates(s, user["user_id"]) == []


def test_an_exercise_the_user_adds_or_removes_by_hand_after_the_switch_is_left_alone_by_undo(make_user):
    user = make_user(condition_codes=["none"])
    with session_scope() as s:
        for a in ("walk", "glute_bridge"):
            profile_service.remove_program_entry(s, user["user_id"], a)
    with session_scope() as s:
        profile_service.set_program_to_activities(s, user["user_id"], ["walk", "glute_bridge"])
    with session_scope() as s:
        profile_service.remove_program_entry(s, user["user_id"], "glute_bridge")                # the user removes one in the player
        profile_service.add_program_entry(s, user["user_id"], "walk")                           # and deliberately re-adds the other
    _undo(user)
    assert _entry(user, "walk")[0] == "active" and _entry(user, "walk")[2] is None             # kept: the user chose it
    assert _entry(user, "glute_bridge") == ("suspended", "user_removed", None)                 # still removed


def test_two_switches_in_a_row_undo_back_to_the_plan_before_the_first(make_user):
    user = make_user(condition_codes=["none"])
    with session_scope() as s:
        profile_service.remove_program_entry(s, user["user_id"], "walk")
    original = _active(user)
    with session_scope() as s:
        profile_service.set_program_to_activities(s, user["user_id"], ["walk", "glute_bridge"])       # switch 1 re-adds Walking and hides the rest
        profile_service.set_program_to_activities(s, user["user_id"], ["dead_bug", "running"])        # switch 2 hides Walking again
    _undo(user)
    assert _active(user) == original
    assert _entry(user, "walk") == ("suspended", "user_removed", None)


def test_the_chat_says_what_undo_will_take_out_and_keeps_the_old_wording_when_nothing_was_added(make_user, client):
    user = make_user(condition_codes=["none"])
    with session_scope() as s:
        profile_service.remove_program_entry(s, user["user_id"], "walk")
    with session_scope() as s:
        profile_service.set_program_to_activities(s, user["user_id"], ["walk"])
    r = client.post("/v1/rehab/query", json={"question": "undo my changes"}, headers=user["headers"]).get_json()
    assert r["needs_revert_confirmation"] is True
    assert "take out Walking" in r["answer"] and "put your plan back as it was" in r["answer"]
    assert {c["action"] for c in r["revert_candidates"]} == {"restore", "pause"}

    plain = make_user(condition_codes=["none"])
    with session_scope() as s:
        profile_service.set_program_to_activities(s, plain["user_id"], ["walk"])               # Walking was already active: the switch only hid others
    r = client.post("/v1/rehab/query", json={"question": "undo my changes"}, headers=plain["headers"]).get_json()
    assert r["answer"].endswith("Want to bring these back?") and {c["action"] for c in r["revert_candidates"]} == {"restore"}


def test_undo_still_refuses_to_restore_what_is_no_longer_approved_but_puts_the_switch_additions_back(make_user, monkeypatch):
    user = make_user(condition_codes=["none"])
    with session_scope() as s:
        profile_service.remove_program_entry(s, user["user_id"], "walk")
    with session_scope() as s:
        profile_service.set_program_to_activities(s, user["user_id"], ["walk"])
    monkeypatch.setattr(rag_service, "_avoided_activity_types", lambda session, codes: {"glute_bridge"})      # a condition added since makes Glute Bridge unsafe
    cands, result = _undo(user)
    assert _entry(user, "glute_bridge")[0] == "suspended"                                    # not restored
    assert _entry(user, "walk")[0] == "suspended"                                            # the switch's addition is still put back
    assert "Not restored, no longer approved" in result["message"] and "Glute Bridge" in result["message"]


def test_a_crafted_undo_cannot_pause_an_exercise_a_switch_did_not_add(make_user):
    user = make_user(condition_codes=["none"])
    with session_scope() as s:
        result = rag_service._confirm_revert(s, user["user_id"], {"activity_types": ["walk", "running"], "confirmed": True})
    assert result["program_updated"] is False and {"walk", "running"} <= _active(user)
