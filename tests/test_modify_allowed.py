"""The chat treats an exercise the audited table marks `modify` ("allowed with caution") the way the app does: the user may add it, the program agent never calls it
"not approved", and the add reply carries the table's caution. `avoid` stays blocked everywhere. Every AI call is mocked.

Example set used here: Knee pain + Slip disc. Table: walk/glute_bridge safe; dead_bug, quad_stretch, lower_back_side_stretch `modify`; running, hamstring_stretch `avoid`.
"""
from app.platform.db import session_scope
from app.rag import exercise_table, program_agent, v2
from app.rag import service as rag_service
from test_new_chat_pipeline import RR, ask, pipe, router, used  # noqa: F401  (fixtures)
from test_pipeline_v2 import _set_program, v2on  # noqa: F401

BOTH = ["arthritis_knee", "herniated_disk"]


def test_the_table_allows_modify_and_never_avoid():
    allowed = exercise_table.allowed_types(BOTH)
    assert "lower_back_side_stretch" in allowed and "dead_bug" in allowed and "walk" in allowed          # safe + modify
    assert "running" not in allowed and "hamstring_stretch" not in allowed                               # avoid
    assert exercise_table.verdict_for(BOTH, "lower_back_side_stretch") == "modify"
    assert exercise_table.verdict_for(BOTH, "running") == "avoid"
    assert "neutral spine" in exercise_table.caution_for(BOTH, "dead_bug")                               # the table's own caution for a ranked pick
    assert exercise_table.caution_for(BOTH, "lower_back_side_stretch") == ""                             # not a ranked pick: no per-exercise caution, the row's general ones are used
    assert exercise_table.allowed_types(["spinal_stenosis"]) == set()                                    # no table row: fail closed


def test_every_audited_avoid_is_never_allowed_for_any_set():
    for codes, row in exercise_table.get().rows.items():
        avoided = {a for a, v in row["verdicts"].items() if v == "avoid"}
        assert not (exercise_table.allowed_types(list(codes)) & avoided), codes
        assert exercise_table.approved_types(list(codes)) <= exercise_table.allowed_types(list(codes)), codes    # the ranked picks are always allowed too


def test_the_chat_lists_a_modify_exercise_as_addable(make_user):
    u = make_user(condition_codes=BOTH)
    with session_scope() as s:
        names = rag_service._allowed_names(s, BOTH)
    assert "Seated Side Stretch" in names and "Running" not in names and "Seated Hamstring Stretch" not in names


def test_the_program_agent_does_not_call_a_modify_exercise_in_the_player_not_approved(make_user):
    u = make_user(condition_codes=BOTH)                          # the default program holds Seated Side Stretch (modify)
    with session_scope() as s:
        state = program_agent.read(s, u["user_id"], rag_service._allowed_names(s, BOTH))
    assert "Seated Side Stretch" in state.active and "Seated Side Stretch" not in state.active_not_approved


def test_a_typed_add_of_a_modify_exercise_gets_a_button_and_the_caution(make_user, v2on):
    u = make_user(condition_codes=BOTH)
    _set_program(u, "lower_back_side_stretch", None)             # not in the player: the button is offered
    v2on["router"]["rr"] = RR(intent="add_exercise", exercise="seated side stretch", wants_exercises=False)
    r = ask(u, "add seated side stretch")
    assert [a["activity_type"] for a in r["suggested_activities"]] == ["lower_back_side_stretch"]
    assert "with caution for your health conditions" in r["answer"] and "Stop if your pain gets worse" in r["answer"]       # the row's general caution (no per-exercise one)


def test_a_typed_add_of_a_ranked_modify_pick_uses_its_own_caution(make_user, v2on):
    u = make_user(condition_codes=BOTH)
    _set_program(u, "dead_bug", None)
    v2on["router"]["rr"] = RR(intent="add_exercise", exercise="dead bug", wants_exercises=False)
    r = ask(u, "add dead bug")
    assert "Dead Bug: " in r["answer"] and "neutral spine" in r["answer"]


def test_a_typed_add_of_an_avoid_exercise_is_still_refused_with_the_protocol(make_user, v2on):
    u = make_user(condition_codes=BOTH)
    v2on["router"]["rr"] = RR(intent="add_exercise", exercise="running", wants_exercises=False)
    r = ask(u, "add running")
    assert not r.get("suggested_activities") and "I can't add Running" in r["answer"] and "with caution" not in r["answer"]


def test_the_add_button_tap_accepts_a_modify_exercise_but_never_an_avoid_one(make_user, v2on):
    u = make_user(condition_codes=BOTH)
    _set_program(u, "lower_back_side_stretch", None)
    with session_scope() as s:
        out = rag_service.answer_query(s, u["user_id"], "", rag_db_url="unused", openrouter_api_key="unused",
                                       program_confirmation={"confirmed": True, "activity_types": ["lower_back_side_stretch", "running", "hamstring_stretch"], "mode": "add"})
    assert out["program_updated"] is True
    with session_scope() as s:
        from sqlalchemy import select
        from app.profile.models import UserProgram
        active = {a for (a,) in s.execute(select(UserProgram.activity_type).where(UserProgram.user_id == u["user_id"], UserProgram.status == "active")).all()}
    assert "lower_back_side_stretch" in active and "running" not in active and "hamstring_stretch" not in active


def test_the_caution_text_names_each_exercise_and_falls_back_to_the_row_cautions():
    text = v2.add_caution(["Dead Bug", "Seated Side Stretch"], {"Dead Bug": "Keep the spine neutral.", "Seated Side Stretch": ""}, ["Stop if your pain gets worse.", "Move slowly."])
    assert text.startswith(" Use Dead Bug, Seated Side Stretch with caution") and "Dead Bug: Keep the spine neutral." in text and "Stop if your pain gets worse." in text
    only_own = v2.add_caution(["Dead Bug"], {"Dead Bug": "Keep the spine neutral."}, ["Stop if your pain gets worse."])
    assert "Stop if your pain gets worse." not in only_own
