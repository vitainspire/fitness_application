"""An exercise that is ACTIVE in the plan but hidden from the exercise player by the player's level limit (Beginner shows 4, 1 month 5, 2 months 7,
3 months 9) must be shown when the user asks for it in the chat: "add Shoulder Circles" used to answer "already in your program" with no button while the
player did not show it.

The real case: Long-standing back pain + Slip disc + BP under control, Beginner level: the player shows Ankle Circles, Glute Bridge, Walking and Wrist Circles;
Shoulder Circles is active but ranked fifth. And the Undo prompt must only offer what it can really bring back.
Every AI call is mocked (see test_pipeline_v2.py for the fixtures).
"""
import pytest

from app.catalog.models import ActivityTaxonomy
from app.platform.db import session_scope
from app.profile import service as profile_service
from app.profile.models import UserProgram
from app.rag import service as rag_service
from test_new_chat_pipeline import MSG, RR, ask, good_answer, pipe, router, used  # noqa: F401  (fixtures)
from test_pipeline_v2 import v2on  # noqa: F401  (fixture)
from test_rag_switch_undo import _active, _entry

CODES = ["chronic_back_pain", "herniated_disk", "bp_controlled"]
SHOWN_AT_BEGINNER = {"ankle_circles", "glute_bridge", "walk", "wrist_circles"}
HIDDEN = "shoulder_circles"


def _pause_for_sport_switch(user, *activity_types):
    """Leave these exercises paused by an earlier chat Switch (suspended_reason sport_focus_change), creating the plan row if the account has none."""
    with session_scope() as s:
        for a in activity_types:
            e = s.get(UserProgram, (user["user_id"], a))
            if e is None:
                tx = s.get(ActivityTaxonomy, a)
                e = UserProgram(user_id=user["user_id"], activity_type=a, target_sets=tx.default_sets, target_amount=tx.default_amount,
                                unit=tx.amount_unit, source="template")
                s.add(e)
            e.status, e.suspended_reason = "suspended", "sport_focus_change"


def _visible(user):
    with session_scope() as s:
        return set(profile_service._dashboard_slice(s, user["user_id"])[1])


def test_the_player_hides_shoulder_circles_at_the_beginner_level_for_this_account(make_user):
    u = make_user(condition_codes=CODES)
    assert HIDDEN in _active(u) and _visible(u) == SHOWN_AT_BEGINNER                 # the bug's starting point: active in the plan, hidden in the player


def test_asking_the_chat_to_add_a_hidden_exercise_offers_the_button_and_the_button_shows_it(make_user, v2on):
    u = make_user(condition_codes=CODES)
    active_before = _active(u)
    v2on["router"]["rr"] = RR(intent="add_exercise", exercise="Shoulder Circles", wants_exercises=False)
    r = ask(u, "add shoulder circles")
    assert r["needs_program_update_confirmation"] and r["program_update_mode"] == "add"
    assert [a["display_name"] for a in r["suggested_activities"]] == ["Shoulder Circles"]
    assert "not shown in your exercise player" in r["answer"] and "already in your program" not in r["answer"]

    with session_scope() as s:
        out = rag_service._confirm_program_update(s, u["user_id"], {"activity_types": [HIDDEN], "mode": "add", "confirmed": True})
    assert out["program_updated"] is True and "Now shown in your exercise player: Shoulder Circles" in out["message"]
    assert _visible(u) == SHOWN_AT_BEGINNER | {HIDDEN}                                # shown now, and nothing else was pushed out or hidden
    assert _active(u) == active_before                                                # nothing was added to or paused in the plan itself
    with session_scope() as s:
        assert s.get(UserProgram, (u["user_id"], HIDDEN)).source == "manual"          # a deliberate choice, so it keeps showing


def test_an_exercise_the_player_already_shows_still_says_already_in_your_program_with_no_button(make_user, v2on):
    u = make_user(condition_codes=CODES)
    v2on["router"]["rr"] = RR(intent="add_exercise", exercise="Walking", wants_exercises=False)
    r = ask(u, "add walking")
    assert "already in your program" in r["answer"] and not r["needs_program_update_confirmation"]


def test_adding_several_at_once_shows_every_hidden_one_not_just_two(make_user):
    u = make_user(condition_codes=CODES)
    hidden_now = _active(u) - _visible(u)
    assert HIDDEN in hidden_now
    with session_scope() as s:
        out = profile_service.set_program_to_activities(s, u["user_id"], sorted(hidden_now | SHOWN_AT_BEGINNER), hide_others=False)
    assert set(out["shown"]) == hidden_now and _visible(u) >= hidden_now          # all of them, however many were asked for


def test_a_chat_add_of_a_new_exercise_is_visible_even_past_the_level_limit(make_user):
    u = make_user(condition_codes=["none"])
    with session_scope() as s:
        for a in ("walk", "glute_bridge", "ankle_circles", "wrist_circles", "calf_stretch"):          # leave a plan the player cannot hold five of
            profile_service.remove_program_entry(s, u["user_id"], a)
    with session_scope() as s:
        profile_service.set_program_to_activities(s, u["user_id"], ["walk", "glute_bridge", "ankle_circles", "wrist_circles", "calf_stretch"], hide_others=False)
    assert {"walk", "glute_bridge", "ankle_circles", "wrist_circles", "calf_stretch"} <= _visible(u)       # five asked for at the Beginner level: all five show


def test_the_undo_prompt_offers_only_what_it_can_bring_back_and_says_why_for_the_rest(make_user, client):
    u = make_user(condition_codes=CODES)
    _pause_for_sport_switch(u, "calf_stretch", "dead_bug", "running")               # dead_bug and running are `avoid` for these three conditions in the audited table
    r = client.post("/v1/rehab/query", json={"question": "undo my changes"}, headers=u["headers"]).get_json()
    assert r["needs_revert_confirmation"] is True
    assert [c["activity_type"] for c in r["revert_candidates"]] == ["calf_stretch"]
    assert "Wall Calf Stretch" in r["answer"] and "Not brought back" in r["answer"] and "Dead Bug" in r["answer"] and "Running" in r["answer"]


def test_if_nothing_paused_can_come_back_the_undo_prompt_says_so_and_has_no_button(make_user, client):
    u = make_user(condition_codes=CODES)
    _pause_for_sport_switch(u, "dead_bug")
    r = client.post("/v1/rehab/query", json={"question": "undo my changes"}, headers=u["headers"]).get_json()
    assert r["needs_revert_confirmation"] is False and not r["revert_candidates"]
    assert "can't bring" in r["answer"] and "Dead Bug" in r["answer"]
