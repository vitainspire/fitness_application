"""(1) The first plan is scaled by the experience level chosen at onboarding, the
same way the add-exercise catalogue already was. (2) The chat's activity recall
shows the real per-set numbers, not an average."""
from datetime import date

import pytest

from app.platform.db import session_scope
from app.profile import service
from app.rag.service import _activity_recall_answer, _recall_amount_text
from app.relationships.service import _EXPERIENCE_MULTIPLIER


def _program(client, user):
    r = client.get("/v1/program", headers=user["headers"])
    assert r.status_code == 200
    return {p["activity_type"]: p for p in r.get_json()["program"]}


def test_beginner_first_plan_is_exactly_the_baseline(client, make_user):
    user = make_user(experience_band="beginner")
    gb = _program(client, user)["glute_bridge"]
    assert (gb["sets"], gb["amount"]) == (2, 5.0)


@pytest.mark.parametrize("band", ["1m", "2m", "3m", "experienced"])
def test_experienced_users_start_higher_than_beginners(client, make_user, band):
    beginner = _program(client, make_user(experience_band="beginner"))
    scaled = _program(client, make_user(experience_band=band))
    assert scaled["glute_bridge"]["amount"] > beginner["glute_bridge"]["amount"]
    # sets are never scaled, only the amount
    assert scaled["glute_bridge"]["sets"] == beginner["glute_bridge"]["sets"]


def test_experienced_first_plan_matches_the_catalogue_scaling(client, make_user):
    from app.progression.service import _round_reps
    user = make_user(experience_band="experienced")
    program = _program(client, user)
    mult = _EXPERIENCE_MULTIPLIER["experienced"]
    with session_scope() as s:
        from app.catalog.models import ActivityTaxonomy
        from sqlalchemy import select
        for t in s.scalars(select(ActivityTaxonomy)).all():
            if t.activity_type not in program:
                continue  # blocked/hidden for this user
            expected = (_round_reps(float(t.default_amount) * mult)
                        if t.prescription_type in ("reps", "hold")
                        else round(float(t.default_amount) * mult, 1))
            assert program[t.activity_type]["amount"] == expected, t.activity_type


def test_existing_progress_is_not_rescaled_when_onboarding_runs_again(client, make_user):
    """build_program also re-runs when conditions change; entries that already exist
    (earned progress) must stay exactly as they are."""
    user = make_user(experience_band="experienced")
    with session_scope() as s:
        service.set_own_target(s, user["user_id"], "glute_bridge", target_sets=2, target_amount=6)
    body = {"age": 30, "goal": "health", "experience_band": "experienced",
            "condition_codes": ["none"], "unit_preference": "km"}
    assert client.post("/v1/profile/onboarding", json=body, headers=user["headers"]).status_code == 200
    assert _program(client, user)["glute_bridge"]["amount"] == 6.0


# ── recall wording ─────────────────────────────────────────────────────────

def _e(**kw):
    base = {"name": "Dead Bug", "sets": 3, "amount": 10.33, "unit": "count",
            "total_volume": 31.0, "set_amounts": None}
    return {**base, **kw}


def test_recall_shows_the_exact_reps_per_set():
    assert (_recall_amount_text(_e(set_amounts=[10, 10, 11]))
            == "3 sets of 10, 10 and 11 reps (31 in total)")


def test_recall_equal_sets_are_not_repeated():
    assert _recall_amount_text(_e(sets=2, set_amounts=[11, 11])) == "2 sets of 11 reps (22 in total)"


def test_recall_uniform_entry_without_a_breakdown():
    assert _recall_amount_text(_e(sets=2, amount=11.0)) == "2 sets of 11 reps (22 in total)"


def test_recall_single_set_distance_hold_and_duration():
    assert _recall_amount_text(_e(sets=1, amount=2.5, unit="km")) == "2.5 km"
    assert _recall_amount_text(_e(sets=3, amount=10.0, unit="sec", set_amounts=[10, 12, 15])) \
        == "3 sets of 10, 12 and 15 sec (37 in total)"
    assert _recall_amount_text(_e(sets=1, amount=30.0, unit="min")) == "30 min"
    assert _recall_amount_text(_e(sets=1, amount=15.0, set_amounts=[15])) == "15 reps"


def test_recall_answer_never_prints_an_average(make_user):
    user = make_user()
    with session_scope() as s:
        service.log_activity(s, user["user_id"], activity_type="dead_bug", set_amounts=[10, 10, 11])
        today = service.today_ist()
        answer = _activity_recall_answer(s, user["user_id"], today, today)
        assert "3 sets of 10, 10 and 11 reps (31 in total)" in answer
        assert "10.33" not in answer
