"""The PROGRAM AGENT: reads the real exercise player only when the router wants exercises; the model is told the facts; buttons only for what is not already active; a blocked
exercise sitting in the player gets the 'I cannot remove it' note. AI mocked (no OpenRouter calls)."""
from app.rag import program_agent
from test_new_chat_pipeline import RR, ask, pipe, router, used  # noqa: F401  (fixtures)
from test_pipeline_v2 import KNEE, _approved, _set_program, v2on  # noqa: F401


def _put_blocked_running_in_player(u):
    """A new account no longer gets Running when it is blocked for the saved conditions (the audited table is applied at signup), but a blocked exercise can still reach the
    player (a doctor override, an older account). Put it there directly."""
    from app.catalog.models import ActivityTaxonomy
    from app.platform.db import session_scope
    from app.profile.models import UserProgram
    with session_scope() as s:
        t = s.get(ActivityTaxonomy, "running")
        s.add(UserProgram(user_id=u["user_id"], activity_type="running", target_sets=t.default_sets, target_amount=t.default_amount, unit=t.amount_unit, source="user_override"))


def _suggestion(names):
    return f"For the knee, start with {', '.join(names)}. Stop if the pain gets worse.\nSUGGESTED_EXERCISES: {', '.join(names)}"


def test_the_model_gets_the_player_state_only_when_exercises_are_wanted(make_user, v2on):
    u = make_user(condition_codes=KNEE)
    names, _ = _approved()
    v2on["router"]["rr"] = RR(wants_exercises=True)
    v2on["answer"] = _suggestion(names[:2])
    ask(u, "which exercises should I do?")
    assert "PLAYER STATE" in v2on["calls"][-1][1]["content"]
    v2on["router"]["rr"] = RR(wants_exercises=False)
    v2on["answer"] = "Knee pain is common with arthritis. Rest if it is hot or swollen."
    ask(u, "why does my knee hurt after walking?")
    assert "PLAYER STATE" not in v2on["calls"][-1][1]["content"]                 # not run on every message


def test_buttons_only_for_exercises_not_already_in_the_player_and_the_reply_says_which(make_user, v2on):
    u = make_user(condition_codes=KNEE)
    names, by_name = _approved()
    suggested = names[:5]
    for n in suggested[:3]:
        _set_program(u, by_name[n], "suspended")                                  # 3 are paused (can be added back), 2 stay active
    v2on["router"]["rr"] = RR(wants_exercises=True)
    v2on["answer"] = _suggestion(suggested)
    r = ask(u, "which exercises should I do?")
    assert sorted(a["display_name"] for a in r["suggested_activities"]) == sorted(suggested[:3])     # which exercises get a button; their order is not the contract
    assert "already in your exercise player" in r["answer"] and suggested[3] in r["answer"] and "Tap the button to add the others" in r["answer"]
    assert r["program_update_modes"] == ["add"]                                   # Switch would pause what the user keeps
    assert r["program_update_mode"] == "add"                                      # the older single-button field must agree: it used to say "switch" here, so the button was labelled Switch


def test_everything_suggested_already_in_the_player_gives_no_button_and_says_so(make_user, v2on):
    u = make_user(condition_codes=KNEE)
    names, _ = _approved()
    v2on["router"]["rr"] = RR(wants_exercises=True)
    v2on["answer"] = _suggestion(names[:2])
    r = ask(u, "which exercises should I do?")
    assert not r["suggested_activities"] and "already in your exercise player" in r["answer"] and "Tap the button" not in r["answer"]


def test_a_blocked_exercise_in_the_player_gets_the_cannot_remove_note(make_user, v2on):
    u = make_user(condition_codes=KNEE)
    _put_blocked_running_in_player(u)                                             # Running is not approved for a knee but sits in the player (e.g. a doctor override)
    names, _ = _approved()
    v2on["router"]["rr"] = RR(wants_exercises=True)
    v2on["answer"] = _suggestion(names[:2])
    r = ask(u, "which exercises should I do?")
    assert "Running is in your exercise player but is not approved for your health conditions" in r["answer"]
    assert "I can't remove it for you; you can remove it yourself in the exercise player" in r["answer"]


def test_state_reading_is_plain_data(make_user):
    u = make_user(condition_codes=KNEE)
    _put_blocked_running_in_player(u)
    from app.platform.db import session_scope
    names, by_name = _approved()
    _set_program(u, by_name[names[0]], "suspended")
    with session_scope() as s:
        st = program_agent.read(s, u["user_id"], names)
    assert names[0] in st.paused and names[0] not in st.active and "Running" in st.active_not_approved
    text = program_agent.block(st)
    assert "PLAYER STATE" in text and "cannot add or remove exercises from chat" in text
