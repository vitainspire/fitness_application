"""Plan rescale on an experience-level change, "walked 30 minutes" -> distance,
recall grouping, and editing a staged plan item."""
from datetime import datetime, timedelta, timezone

import pytest

from app.auth.models import User
from app.platform.db import session_scope
from app.profile import service
from app.profile.models import UserProgram
from app.rag.service import _activity_recall_answer, _recall_group_text
from app.relationships import service as rel_service
from app.voice import service as voice_service


def _amount(client, user, activity="glute_bridge"):
    program = client.get("/v1/program", headers=user["headers"]).get_json()["program"]
    return next(p for p in program if p["activity_type"] == activity)["amount"]


def _put_profile(client, user, **body):
    return client.put("/v1/profile", json=body, headers=user["headers"])


def _age_account(user, days=30):
    with session_scope() as s:
        s.get(User, user["user_id"]).created_at = datetime.now(timezone.utc) - timedelta(days=days)


# ── rescale: new account ────────────────────────────────────────────────────

def test_level_up_on_a_new_account_moves_to_the_new_levels_starting_numbers(client, make_user):
    user = make_user(experience_band="beginner")
    assert _amount(client, user) == 5.0
    r = _put_profile(client, user, experience_band="experienced")
    assert r.status_code == 200
    body = r.get_json()
    assert "needs_confirmation" not in body                      # an increase applies silently
    assert any(c["activity_type"] == "glute_bridge" and c["old"] == "2 x 5" and c["new"] == "2 x 8"
               for c in body["plan_rescaled"])
    assert _amount(client, user) == 8.0


def test_new_account_rescale_matches_what_a_fresh_account_at_that_level_gets(client, make_user):
    moved = make_user(experience_band="beginner")
    _put_profile(client, moved, experience_band="2m")
    fresh = make_user(experience_band="2m")
    a = client.get("/v1/program", headers=moved["headers"]).get_json()["program"]
    b = client.get("/v1/program", headers=fresh["headers"]).get_json()["program"]
    assert {p["activity_type"]: p["amount"] for p in a} == {p["activity_type"]: p["amount"] for p in b}


def test_same_level_does_nothing(client, make_user):
    user = make_user(experience_band="beginner")
    r = _put_profile(client, user, experience_band="beginner", goal="fitness")
    assert r.status_code == 200 and "plan_rescaled" not in r.get_json()
    assert _amount(client, user) == 5.0


# ── rescale: level down asks first ──────────────────────────────────────────

def test_level_down_asks_for_confirmation_and_saves_nothing(client, make_user):
    user = make_user(experience_band="experienced")
    assert _amount(client, user) == 8.0
    r = _put_profile(client, user, experience_band="beginner", goal="fitness")
    body = r.get_json()
    assert r.status_code == 200 and body["needs_confirmation"] is True and body["reason"] == "level_down"
    profile = client.get("/v1/profile", headers=user["headers"]).get_json()
    assert profile["experience_band"] == "experienced" and profile["goal"] != "fitness"  # nothing saved
    assert _amount(client, user) == 8.0


def test_level_down_with_confirmation_rescales(client, make_user):
    user = make_user(experience_band="experienced")
    r = _put_profile(client, user, experience_band="beginner", confirm_rescale=True)
    assert r.status_code == 200 and "needs_confirmation" not in r.get_json()
    assert r.get_json()["plan_rescaled"]
    assert _amount(client, user) == 5.0


# ── rescale: account with history ───────────────────────────────────────────

def test_with_history_the_best_single_set_in_three_weeks_becomes_the_target(client, make_user):
    user = make_user(experience_band="beginner")
    _age_account(user)
    with session_scope() as s:
        service.log_activity(s, user["user_id"], activity_type="glute_bridge", set_amounts=[12, 9, 7])
        service.log_activity(s, user["user_id"], activity_type="glute_bridge", set_amounts=[8, 8])
    r = _put_profile(client, user, experience_band="experienced")
    assert r.status_code == 200
    assert _amount(client, user) == 12.0                    # best single SET, not a day's total
    # an exercise with no history falls back to the new level's starting numbers
    assert _amount(client, user, "dead_bug") == 8.0 or _amount(client, user, "dead_bug") > 5.0


def test_with_history_target_is_never_below_baseline(client, make_user):
    user = make_user(experience_band="beginner")
    _age_account(user)
    with session_scope() as s:
        service.log_activity(s, user["user_id"], activity_type="glute_bridge", set_amounts=[2, 2])
    _put_profile(client, user, experience_band="experienced")
    assert _amount(client, user) == 5.0                     # baseline floor


def test_history_older_than_three_weeks_is_ignored(client, make_user):
    from app.profile.models import ActivityLog
    user = make_user(experience_band="beginner")
    _age_account(user, 60)
    with session_scope() as s:
        service.log_activity(s, user["user_id"], activity_type="glute_bridge", set_amounts=[20, 20])
        for log in s.query(ActivityLog).filter_by(user_id=user["user_id"]).all():
            log.local_date = service.today_ist() - timedelta(days=40)
    _put_profile(client, user, experience_band="experienced")
    assert _amount(client, user) == 8.0                     # new level's start, not the old 20


# ── rescale: what is left alone ─────────────────────────────────────────────

def test_fixed_managed_and_sore_entries_are_left_alone(client, make_user):
    user = make_user(experience_band="beginner")
    with session_scope() as s:
        service.set_own_target(s, user["user_id"], "glute_bridge", target_sets=2, target_amount=6,
                               auto_progress=False)               # the user fixed this one
        s.get(UserProgram, (user["user_id"], "dead_bug")).soreness_reduced_since = datetime.now(timezone.utc)
    before_dead_bug = _amount(client, user, "dead_bug")
    _put_profile(client, user, experience_band="experienced")
    assert _amount(client, user) == 6.0
    assert _amount(client, user, "dead_bug") == before_dead_bug


def test_rescale_clears_a_stale_per_set_breakdown(client, make_user):
    user = make_user(experience_band="beginner")
    with session_scope() as s:
        service.set_own_target(s, user["user_id"], "glute_bridge", target_sets=2, target_amount=0,
                               set_amounts=[9, 9])
        assert s.get(UserProgram, (user["user_id"], "glute_bridge")).set_targets is not None
    _put_profile(client, user, experience_band="experienced")
    with session_scope() as s:
        e = s.get(UserProgram, (user["user_id"], "glute_bridge"))
        assert e.set_targets is None and float(e.target_amount) == 8.0


def test_onboarding_rerun_with_a_new_level_rescales_and_asks_on_a_downgrade(client, make_user):
    user = make_user(experience_band="beginner")
    body = {"age": 30, "goal": "health", "experience_band": "experienced",
            "condition_codes": ["none"], "unit_preference": "km"}
    r = client.post("/v1/profile/onboarding", json=body, headers=user["headers"])
    assert r.status_code == 200 and r.get_json()["plan_rescaled"]
    assert _amount(client, user) == 8.0
    down = {**body, "experience_band": "beginner"}
    r = client.post("/v1/profile/onboarding", json=down, headers=user["headers"])
    assert r.get_json()["needs_confirmation"] is True and _amount(client, user) == 8.0
    r = client.post("/v1/profile/onboarding", json={**down, "confirm_rescale": True}, headers=user["headers"])
    assert "needs_confirmation" not in r.get_json() and _amount(client, user) == 5.0


# ── voice: time -> distance ─────────────────────────────────────────────────

def _extract(user, reply, transcript="x"):
    import json
    voice_service_chat = lambda *a, **k: json.dumps(reply)
    original = voice_service.chat_complete
    voice_service.chat_complete = voice_service_chat
    try:
        with session_scope() as s:
            return voice_service.extract_activities(s, user["user_id"], transcript, openrouter_api_key="unused")
    finally:
        voice_service.chat_complete = original


def _entry(code, sets, minutes=None, unit="km"):
    return {"activity_type": code, "activity_name": None, "is_custom": False,
            "set_amounts": sets, "unit": unit, "duration_minutes": minutes}


def test_walking_minutes_become_distance_using_the_taxonomy_pace(make_user):
    user = make_user()
    out = _extract(user, {"reply_text": "ok", "entries": [_entry("walk", [], 30)]})
    e = out["entries"][0]
    assert e["set_amounts"] == [2.5] and e["unit"] == "km" and e["estimated_from_minutes"] == 30   # 30 / 12


def test_running_minutes_use_the_running_pace(make_user):
    user = make_user()
    out = _extract(user, {"reply_text": "ok", "entries": [_entry("running", None, 30)]})
    assert out["entries"][0]["set_amounts"] == [4.29]                                              # 30 / 7


def test_a_stated_distance_is_never_overridden_by_the_time(make_user):
    user = make_user()
    out = _extract(user, {"reply_text": "ok", "entries": [_entry("walk", [2], 15)]})
    e = out["entries"][0]
    assert e["set_amounts"] == [2] and "estimated_from_minutes" not in e


@pytest.mark.parametrize("minutes", [0, -5, None, "thirty", True])
def test_no_usable_time_and_no_distance_is_still_dropped(make_user, minutes):
    user = make_user()
    out = _extract(user, {"reply_text": "ok", "entries": [_entry("walk", [], minutes)]})
    assert out["entries"] == []


def test_time_only_for_a_non_distance_exercise_is_still_dropped(make_user):
    user = make_user()
    out = _extract(user, {"reply_text": "ok", "entries": [_entry("glute_bridge", [], 10, "count")]})
    assert out["entries"] == []


def test_the_prompt_carries_the_new_rules(make_user):
    user = make_user()
    seen = {}
    original = voice_service.chat_complete
    voice_service.chat_complete = lambda messages, *a, **k: seen.update(p=messages[0]["content"]) or '{"entries": []}'
    try:
        with session_scope() as s:
            voice_service.extract_activities(s, user["user_id"], "x", openrouter_api_key="unused")
    finally:
        voice_service.chat_complete = original
    assert "include its unit word" in seen["p"] and "empty list []" in seen["p"]


# ── recall grouping ─────────────────────────────────────────────────────────

def test_recall_combines_repeats_of_one_exercise_into_one_line(make_user):
    user = make_user()
    with session_scope() as s:
        service.log_activity(s, user["user_id"], activity_type="walk", amount=1.0, source="voice")
        service.log_activity(s, user["user_id"], activity_type="walk", amount=1.5, source="gps")
        service.log_activity(s, user["user_id"], activity_type="walk", amount=0.5)
        service.log_activity(s, user["user_id"], activity_type="glute_bridge", set_amounts=[10, 10])
        today = service.today_ist()
        answer = _activity_recall_answer(s, user["user_id"], today, today)
        assert answer.count("Walking") == 1
        assert "3 entries, 3 km in total" in answer
        assert "2 sets of 10 reps (20 in total)" in answer and "Glute Bridge" in answer


def test_group_text_for_reps_and_single_entries():
    one = {"sets": 2, "amount": 11.0, "unit": "count", "total_volume": 22.0, "set_amounts": [11.0, 11.0]}
    two = {"sets": 3, "amount": 10.33, "unit": "count", "total_volume": 31.0, "set_amounts": [10.0, 10.0, 11.0]}
    assert _recall_group_text([one]) == "2 sets of 11 reps (22 in total)"
    assert _recall_group_text([one, two]) == (
        "2 entries, 53 reps in total (2 sets of 11 reps (22 in total); "
        "3 sets of 10, 10 and 11 reps (31 in total))")


def test_group_text_long_lists_show_only_the_total():
    e = {"sets": 1, "amount": 1.0, "unit": "km", "total_volume": 1.0, "set_amounts": None}
    assert _recall_group_text([e] * 7) == "7 entries, 7 km in total"


# ── staged plan item: edit = re-stage ───────────────────────────────────────

def test_restaging_an_item_keeps_an_earlier_safety_confirmation(make_user):
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "patient", "P")
        rel_service.set_pending_profile(s, manager["user_id"], invite["id"], age=40,
                                        condition_codes=["herniated_disk"])
        first = rel_service.add_pending_entry(
            s, manager["user_id"], invite["id"], activity_type="hamstring_stretch",
            target_sets=2, target_amount=15.0, auto_progress=True, override_confirmed=True)
        assert first["added"] is True
        # the Edit screen re-stages with new numbers and no override flag
        again = rel_service.add_pending_entry(
            s, manager["user_id"], invite["id"], activity_type="hamstring_stretch",
            target_sets=3, target_amount=20.0, auto_progress=False)
        assert again["added"] is True and again["target_sets"] == 3
        listed = rel_service.list_pending_entries(s, manager["user_id"], invite["id"])
        assert len(listed) == 1 and listed[0]["target_amount"] == 20.0 and listed[0]["auto_progress"] is False
        from app.profile import service as ps
        from app.platform.config import load_config
        _p, user, _r = rel_service.open_invite(s, load_config(), invite["invite_token"], "dev")
        assert s.get(ps.UserProgram, (user.id, "hamstring_stretch")) is not None  # not dropped at open


def test_an_unconfirmed_avoid_item_still_asks_the_first_time(make_user):
    manager = make_user(condition_codes=["none"])
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "patient", "P")
        rel_service.set_pending_profile(s, manager["user_id"], invite["id"], age=40,
                                        condition_codes=["herniated_disk"])
        r = rel_service.add_pending_entry(
            s, manager["user_id"], invite["id"], activity_type="hamstring_stretch",
            target_sets=2, target_amount=15.0, auto_progress=True)
        assert r["needs_confirmation"] is True
