"""Per-set rep logging (set_amounts): backward-compatible, additive field on
POST /v1/activities. See PER_SET_PROGRESSION_PLAN.md.
"""
from app.platform.db import session_scope
from app.profile.models import ActivityLog


def test_set_amounts_derives_sets_and_average(make_user, client):
    user = make_user()
    r = client.post(
        "/v1/activities",
        json={"activity_type": "glute_bridge", "set_amounts": [10, 8, 6]},
        headers=user["headers"],
    )
    assert r.status_code == 201
    body = r.get_json()["logged"][0]
    assert body["duplicate"] is False
    # total_volume = real sum (24), not an assumed-uniform sets*amount.
    assert body["total_volume"] == 24.0

    with session_scope() as s:
        log = s.get(ActivityLog, body["id"])
        assert log.sets_done == 3
        assert float(log.amount_per_set) == 8.0  # average of 10, 8, 6
        assert [float(v) for v in log.set_amounts] == [10.0, 8.0, 6.0]


def test_uniform_sets_produce_the_same_total_as_before(make_user, client):
    """sets * amount == sum(set_amounts) when every set happens to be equal —
    set_amounts must never change the result for the uniform case."""
    user = make_user()
    uniform = client.post(
        "/v1/activities",
        json={"activity_type": "glute_bridge", "amount": 8, "sets": 3},
        headers=user["headers"],
    ).get_json()["logged"][0]
    per_set = client.post(
        "/v1/activities",
        json={"activity_type": "glute_bridge", "set_amounts": [8, 8, 8]},
        headers=user["headers"],
    ).get_json()["logged"][0]
    assert uniform["total_volume"] == per_set["total_volume"] == 24.0


def test_set_amounts_omitted_is_fully_backward_compatible(make_user, client):
    user = make_user()
    r = client.post(
        "/v1/activities",
        json={"activity_type": "glute_bridge", "amount": 5, "sets": 2},
        headers=user["headers"],
    )
    assert r.status_code == 201
    with session_scope() as s:
        log = s.get(ActivityLog, r.get_json()["logged"][0]["id"])
        assert log.set_amounts is None


def test_set_amounts_rejects_custom_exercise(make_user, client):
    user = make_user()
    custom = client.post(
        "/v1/custom-exercises",
        json={"name": "Band Pull Apart", "measurement_type": "reps", "target_amount": 10},
        headers=user["headers"],
    ).get_json()
    r = client.post(
        "/v1/activities",
        json={"custom_exercise_id": custom["id"], "set_amounts": [10, 8]},
        headers=user["headers"],
    )
    assert r.status_code == 422


def test_set_amounts_rejects_empty_list(make_user, client):
    user = make_user()
    r = client.post(
        "/v1/activities",
        json={"activity_type": "glute_bridge", "set_amounts": []},
        headers=user["headers"],
    )
    assert r.status_code == 422


def test_set_amounts_rejects_non_positive_values(make_user, client):
    user = make_user()
    r = client.post(
        "/v1/activities",
        json={"activity_type": "glute_bridge", "set_amounts": [10, 0, 6]},
        headers=user["headers"],
    )
    assert r.status_code == 422


def test_set_amounts_rejects_non_numeric_elements(make_user, client):
    user = make_user()
    r = client.post(
        "/v1/activities",
        json={"activity_type": "glute_bridge", "set_amounts": [10, "eight", 6]},
        headers=user["headers"],
    )
    assert r.status_code == 422


def test_set_amounts_multi_element_rejected_for_distance_activity(make_user, client):
    """walk is a single-effort distance activity — already locked to exactly
    1 set by the existing prescription-type check, which set_amounts must
    not bypass."""
    user = make_user()
    r = client.post(
        "/v1/activities",
        json={"activity_type": "walk", "set_amounts": [1.0, 1.5]},
        headers=user["headers"],
    )
    assert r.status_code == 422


def test_set_amounts_respects_total_volume_cap(make_user, client):
    """MAX_TOTAL_VOLUME (~1e8) must bound the real SUM of set_amounts, not
    just each individual value against MAX_LOGGABLE_AMOUNT (1e5) — 1000 sets
    at the per-value max sums past the total cap even though every single
    value is individually legal."""
    user = make_user()
    r = client.post(
        "/v1/activities",
        json={"activity_type": "glute_bridge", "set_amounts": [100000] * 1000},
        headers=user["headers"],
    )
    assert r.status_code == 422


def test_set_amounts_prescription_shows_the_real_breakdown(make_user, client):
    user = make_user()
    r = client.post(
        "/v1/activities",
        json={"activity_type": "glute_bridge", "set_amounts": [10, 8, 6]},
        headers=user["headers"],
    )
    assert r.get_json()["logged"][0]["prescription"] == "10, 8, 6 count"


def test_set_amounts_still_tags_a_workout_session(make_user, client):
    """The two new logging features (set_amounts, workout sessions) must
    compose cleanly — set_amounts was built after sessions, on the same
    entry point."""
    user = make_user()
    started = client.post(
        "/v1/workout-sessions", json={"activity_types": ["glute_bridge"]},
        headers=user["headers"],
    ).get_json()
    r = client.post(
        "/v1/activities",
        json={"activity_type": "glute_bridge", "set_amounts": [10, 8],
              "workout_session_id": started["id"]},
        headers=user["headers"],
    )
    assert r.status_code == 201
    finished = client.post(
        f"/v1/workout-sessions/{started['id']}/finish", headers=user["headers"],
    ).get_json()
    assert finished["completed_activity_count"] == 1
