"""Round 3 (from the fresh blind set): Hinglish / Hindi commands, Devanagari exercise names, reminders and account deletion in any language, 'no saved condition' can add any
exercise, and the risk carried forward from a fixed risk reply to an ambiguous follow-up. AI mocked."""
from app.rag import v2
from test_new_chat_pipeline import MSG, RR, ask, pipe, router, used  # noqa: F401  (fixtures)
from test_pipeline_v2 import KNEE, _set_program, v2on  # noqa: F401
from test_risk_classifier import _say, clf  # noqa: F401


def test_hinglish_and_hindi_add_commands_are_free_lookups(make_user, v2on):
    u = make_user(condition_codes=KNEE)
    _set_program(u, "shoulder_circles", None)
    for q in ("walking daal do", "dead bug jodo", "मेरे प्रोग्राम में वॉकिंग डालो"):
        r = ask(u, q)
        assert any(w in r["answer"] for w in ("already in your program", "approved for you", "paused", "can't add")), q
    r = ask(u, "शोल्डर सर्कल जोड़ दो")
    assert [a["display_name"] for a in r["suggested_activities"]] == ["Shoulder Circles"] or "can't add" in r["answer"]
    assert used(u) == 0 and not v2on["calls"] and v2on["router"]["calls"] == 0


def test_a_user_with_no_saved_condition_can_add_any_exercise(make_user, v2on):
    u = make_user(condition_codes=["none"])
    _set_program(u, "dead_bug", None)
    r = ask(u, "add dead bug")
    assert [a["display_name"] for a in r["suggested_activities"]] == ["Dead Bug"]


def test_reminders_and_account_deletion_are_free_fixed_messages_in_any_language(make_user, v2on):
    u = make_user(condition_codes=KNEE)
    for q in ("remind me to walk at 7pm", "roz subah 6 baje reminder lagao", "रिमाइंडर सेट कर दो", "kya notification aayega?"):
        assert ask(u, q)["answer"] == v2.REMINDER_MESSAGE, q
    for q in ("Delete my account right now.", "mera account delete kar do"):
        assert ask(u, q)["answer"] == v2.ACCOUNT_MESSAGE, q
    assert used(u) == 0 and not v2on["calls"] and v2on["router"]["calls"] == 0
    assert not v2.asks_delete_account("how do I delete a bad habit")


def test_target_changes_in_hinglish_and_hindi_are_recognised():
    for q in ("target 50 kar do", "टारगेट कम कर दो", "target kam kar do bahut zyada hai", "मेरा लक्ष्य 20 मिनट से बदलकर 2 घंटे कर दो", "set my walking target to 100 reps"):
        assert v2.asks_to_set_target(q), q
    assert not v2.asks_to_set_target("mera target kitna hai") and not v2.asks_to_set_target("can I increase my target?")


def test_an_ambiguous_follow_up_to_a_risky_reply_keeps_the_risk(make_user, clf):
    u = make_user(condition_codes=KNEE)
    _say(clf, "emergency")
    assert ask(u, "seene me dard hai left side")["answer"] == MSG["urgent"]
    clf["res"] = {"class": "none", "confidence": 0.46, "probs": {"emergency": 0.49, "none": 0.48}, "source": "jev"}
    assert ask(u, "ab kya karu?")["answer"] == MSG["urgent"]                                   # not clearly a new topic: the risk still applies
    clf["v2on"]["router"]["rr"] = RR(wants_exercises=False)
    clf["v2on"]["answer"] = "Walking is gentle and builds endurance without stressing the joint. Stop if your pain gets worse."
    clf["res"] = {"class": "none", "confidence": 0.99, "probs": {"none": 0.99}, "source": "jev"}
    assert "Walking is gentle" in ask(u, "which exercises are good for my knee?")["answer"]       # a clearly separate question is answered normally


def test_a_minor_stays_a_minor_for_later_exercise_requests(make_user, clf):
    u = make_user(condition_codes=["none"])
    _say(clf, "minor")
    assert ask(u, "main 14 saal ka hu")["answer"] == v2.MINOR_MESSAGE
    clf["res"] = {"class": "none", "confidence": 0.7, "probs": {"none": 0.7, "minor": 0.3}, "source": "jev"}
    assert ask(u, "koi exercise bata do na")["answer"] == v2.MINOR_MESSAGE


def test_a_modest_self_harm_probability_acts_even_when_none_is_the_top_class(make_user, clf):
    u = make_user(condition_codes=KNEE)
    clf["res"] = {"class": "none", "confidence": 0.57, "probs": {"none": 0.57, "self_harm": 0.35}, "source": "jev"}
    assert ask(u, "sab khatam kar dena chahta hu")["answer"] == v2.SELF_HARM_MESSAGE
    clf["v2on"]["router"]["rr"] = RR(wants_exercises=False)
    clf["v2on"]["answer"] = "Walking is gentle and builds endurance without stressing the joint. Stop if your pain gets worse."
    clf["res"] = {"class": "none", "confidence": 0.99, "probs": {"none": 0.99, "self_harm": 0.01}, "source": "jev"}
    assert "Walking is gentle" in ask(u, "workout khatam kar diya")["answer"]


def test_a_yes_no_question_about_one_activity_never_gets_a_chip():
    rr = {"intent": "medical", "condition": None, "sport": "", "exercise": "", "claims_healthy": False, "unsupported_data": False, "wants_exercises": True, "add_mode": "switch"}
    for q in ("Is yoga good for my back and knee?", "can I squat?", "is cycling ok for me", "aur cycling?".replace("aur ", "can I do ")):
        assert v2.apply_router_checks(q, rr, set(), set())["wants_exercises"] is False, q
    for q in ("which exercises are good for my knee?", "can you suggest exercises for my back?", "what can I do for my hip?"):
        assert v2.apply_router_checks(q, rr, set(), set())["wants_exercises"] is True, q


def test_several_exercises_in_one_add_command_are_split_never_asked_which_one():
    assert v2.split_exercise_parts("running and ankle circles") == ["running", "ankle circles"]
    assert v2.split_exercise_parts("walking, dead bug & neck hold") == ["walking", "dead bug", "neck hold"]
    assert v2.fast_add_name("add running and ankle circles") == "running and ankle circles"
    assert v2.is_choice_followup("both") and v2.is_choice_followup("dono") and not v2.is_choice_followup("both knees hurt")


def test_add_two_exercises_then_both_after_our_question(make_user, v2on):
    u = make_user(condition_codes=KNEE)
    _set_program(u, "shoulder_circles", None)
    r = ask(u, "add running and ankle circles")
    assert "Which one do you mean" not in r["answer"]
    assert "Ankle Circles" in r["answer"] + " ".join(a["display_name"] for a in (r["suggested_activities"] or []))
    assert used(u) == 0 and not v2on["calls"]                                                  # free lookup, no model call
    r = ask(u, "add circles")                                                                   # genuinely ambiguous: we ask
    assert r["answer"].startswith("Which one do you mean: ")
    r = ask(u, "both")                                                                          # the answer to OUR question: resolved from our own reply, never a medical answer
    assert "neck" not in r["answer"].lower() and not v2on["calls"] and v2on["router"]["calls"] == 0


def test_a_choice_reply_to_our_own_question_stays_an_add_and_is_not_demoted_to_medical():
    rr = {"intent": "add_exercise", "condition": None, "sport": "", "exercise": "both", "claims_healthy": False, "unsupported_data": False, "wants_exercises": False, "add_mode": "switch"}
    ask_q = "Which one do you mean: Shoulder Circles, Ankle Circles, Wrist Circles?"
    g = v2.apply_router_checks("both", rr, set(), set(), ask_q)
    assert g["intent"] == "add_exercise" and g["exercise"] == ""
    assert v2.apply_router_checks("both", rr, set(), set(), "Walking is gentle.")["intent"] == "medical"        # no question of ours before it: still not an add
