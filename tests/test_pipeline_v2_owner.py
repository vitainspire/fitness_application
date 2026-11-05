"""Fixes from the owner's own manual test of 2026-11-02 (three saved conditions: back pain, slip disc, knee pain). Every AI call is mocked."""
from sqlalchemy import update

from app.platform.db import session_scope
from app.profile.models import UserProgram
from app.rag import service as rag_service
from app.rag import v2
from test_new_chat_pipeline import MSG, RR, ask, pipe, router, used  # noqa: F401  (fixtures)
from test_pipeline_v2 import KNEE, _approved, _set_program, v2on  # noqa: F401

THREE = ["chronic_back_pain", "herniated_disk", "arthritis_knee"]


def test_aur_means_and_in_hindi_not_more():
    assert not v2.asks_for_more("ghutne aur kamar dard ke liye kaunsi exercise karu?")
    assert v2.asks_for_more("aur exercises batao") and v2.asks_for_more("give me more exercises")


def test_no_chip_when_the_program_already_is_exactly_the_suggestion(make_user, v2on):
    u = make_user(condition_codes=THREE)
    names, by_name = _approved()
    keep = {by_name[n] for n in names}
    with session_scope() as s:                                                  # only the suggested exercises are active
        s.execute(update(UserProgram).where(UserProgram.user_id == u["user_id"], ~UserProgram.activity_type.in_(keep)).values(status="suspended"))
    v2on["router"]["rr"] = RR(wants_exercises=True)
    v2on["answer"] = f"You can start with {', '.join(names)} for your joints, and stop if the pain gets worse.\nSUGGESTED_EXERCISES: {', '.join(names)}"
    r = ask(u, "Which exercises should I start with?")
    assert not r["suggested_activities"] and not r["needs_program_update_confirmation"]


def test_a_greeting_or_a_name_gets_no_chat_history(make_user, v2on):
    u = make_user(condition_codes=KNEE)
    v2on["router"]["rr"] = RR(intent="conversation", wants_exercises=False)
    v2on["answer"] = "Hello Priya! How can I help you today?"
    ask(u, "my name is Priya")
    assert "not needed" in v2on["calls"][-1][1]["content"] and "Q:" not in v2on["calls"][-1][1]["content"]
    ask(u, "what is my name?")                                                   # a recall question sees the user's own earlier words
    sent = v2on["calls"][-1][1]["content"]
    assert "my name is Priya" in sent


def test_information_answers_never_see_old_answers(make_user, v2on):
    u = make_user(condition_codes=KNEE)
    names, _ = _approved()
    v2on["router"]["rr"] = RR(wants_exercises=False)
    v2on["answer"] = "Your adherence is declining at 42 percent and your streak is 0 days, so push harder."
    ask(u, "show my progress please")
    v2on["answer"] = f"The {names[0]} helps strengthen your hips and glutes in a gentle way. Stop if your pain gets worse."
    ask(u, "tell me more about glute bridge")
    sent = v2on["calls"][-1][1]["content"]
    assert "adherence is declining" not in sent and "show my progress please" in sent


def test_offers_the_app_cannot_keep_are_removed(make_user, v2on):
    u = make_user(condition_codes=KNEE)
    names, _ = _approved()
    v2on["router"]["rr"] = RR(wants_exercises=False)
    v2on["answer"] = f"You can walk daily and use {names[0]} gently. Stop if the pain gets worse.\n\nWould you like me to help you set reminders for these exercises?"
    r = ask(u, "how do I stay consistent with my exercises")
    assert "reminder" not in r["answer"].lower() and "Would you like" not in r["answer"] and "Stop if the pain" in r["answer"]


def test_high_sugar_or_blood_pressure_gets_the_doctor_first_message_for_free(make_user, v2on):
    u = make_user(condition_codes=KNEE)
    for q in ("my sugar is high", "my BP is 170/110", "bp is high today"):
        assert ask(u, q)["answer"] == MSG["controls"]
    assert used(u) == 0 and not v2on["calls"] and not v2.says_high_sugar_or_bp("what is blood pressure")


def test_a_weekly_routine_uses_only_the_users_own_targets(make_user, v2on):
    u = make_user(condition_codes=THREE)
    v2on["router"]["rr"] = RR(wants_exercises=True)
    r = ask(u, "give me a routine for the week")
    assert "Monday:" in r["answer"] and "Sunday: Rest" in r["answer"] and "Walking" in r["answer"] and not v2on["calls"]
    assert "20 minutes" not in r["answer"] and "reminder" not in r["answer"].lower() and not r["suggested_activities"]


def test_what_should_i_avoid_comes_from_the_table_cautions(make_user, v2on):
    u = make_user(condition_codes=THREE)
    v2on["router"]["rr"] = RR(wants_exercises=False)
    r = ask(u, "what should I avoid?")
    assert r["answer"].startswith("For your saved health conditions, take care with this:") and "bending forward" in r["answer"] and not v2on["calls"]
    assert "(NHS" not in r["answer"] and "AAOS" not in r["answer"]


def test_increase_target_says_not_yet_with_the_real_numbers(make_user, v2on):
    u = make_user(condition_codes=KNEE)
    v2on["router"]["rr"] = RR(intent="increase_target", wants_exercises=False)
    r = ask(u, "can I increase my target? by how much?")
    assert r["answer"].startswith("Not yet.") and "100% or more" in r["answer"] and not r["needs_escalation_confirmation"] and not v2on["calls"]


def test_typed_add_accepts_what_the_safety_grid_calls_safe_for_all_conditions(make_user, v2on):
    u = make_user(condition_codes=THREE)
    _set_program(u, "ankle_circles", None)                                       # Ankle Circles: safe for back pain, slip disc and knee pain in the grid; not a top pick in the table
    r = ask(u, "add ankle circles")
    assert [a["display_name"] for a in r["suggested_activities"]] == ["Ankle Circles"] and r["program_update_mode"] == "add"
    with session_scope() as s:
        out = rag_service.answer_query(s, u["user_id"], "", rag_db_url="unused", openrouter_api_key="unused",
                                       program_confirmation={"confirmed": True, "activity_types": ["ankle_circles"], "mode": "add"})
    assert out["program_updated"] is True
    assert "I can't add Dead Bug" in ask(u, "add dead bug")["answer"]            # "modify" for back pain and slip disc: still refused


def test_a_typo_in_an_exercise_name_asks_did_you_mean(make_user, v2on):
    u = make_user(condition_codes=THREE)
    v2on["router"]["rr"] = RR(intent="add_exercise", exercise="angle circles", wants_exercises=False)
    r = ask(u, "add angle circles")
    assert "Did you mean Ankle Circles?" in r["answer"] and not r["suggested_activities"]


def test_the_healthy_message_joins_three_conditions_in_plain_english():
    m = v2.healthy_message(["Long-standing back pain", "Slip disc", "Knee pain"])
    assert "(Long-standing back pain, Slip disc and Knee pain)" in m and "any of them" in m
    assert "don't have it any more" in v2.healthy_message(["Knee pain"])


def test_a_negated_phrase_is_not_self_harm():
    assert not v2.self_harm("i dont want to end it all") and v2.self_harm("I want to end it all")
