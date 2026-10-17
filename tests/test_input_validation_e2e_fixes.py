"""Fixes from the E2E audit: malformed ids and missing required fields must be
clean 4xx errors (never a 500), and valid requests must behave exactly as before."""
import pytest

BAD_ID = "malformed-id"
UNKNOWN_UUID = "00000000-0000-0000-0000-000000000000"


# ── BUG-01: malformed ids ───────────────────────────────────────────────

@pytest.mark.parametrize("method,path,body", [
    ("put", f"/v1/custom-exercises/{BAD_ID}", {"name": "x"}),
    ("delete", f"/v1/custom-exercises/{BAD_ID}", None),
    ("delete", f"/v1/relationships/{BAD_ID}", None),
    ("get", f"/v1/relationships/{BAD_ID}/activity", None),
    ("get", f"/v1/relationships/{BAD_ID}/pending-entries", None),
    ("post", f"/v1/relationships/{BAD_ID}/pending-entries", {"activity_type": "walk"}),
    ("delete", f"/v1/relationships/{BAD_ID}/pending-custom-exercises/{BAD_ID}", None),
    ("post", f"/v1/workout-sessions/{BAD_ID}/finish", None),
])
def test_malformed_path_id_is_404_not_500(client, make_user, method, path, body):
    user = make_user()
    r = getattr(client, method)(path, headers=user["headers"], **({"json": body} if body is not None else {}))
    assert r.status_code == 404, (path, r.status_code, r.get_data(as_text=True))
    assert r.get_json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("body", [
    {"custom_exercise_id": BAD_ID, "amount": 5},
    {"activity_type": "walk", "amount": 1, "workout_session_id": BAD_ID},
])
def test_malformed_body_id_on_activity_log_is_404_not_500(client, make_user, body):
    user = make_user()
    r = client.post("/v1/activities", json=body, headers=user["headers"])
    assert r.status_code == 404, r.get_data(as_text=True)


def test_well_formed_but_unknown_ids_still_get_their_specific_404(client, make_user):
    user = make_user()
    r = client.delete(f"/v1/relationships/{UNKNOWN_UUID}", headers=user["headers"])
    assert r.status_code == 404
    assert r.get_json()["error"]["message"] == "No such managed relationship."
    r = client.delete(f"/v1/custom-exercises/{UNKNOWN_UUID}", headers=user["headers"])
    assert r.status_code == 404


def test_valid_custom_exercise_and_relationship_ids_still_work(client, make_user):
    user = make_user()
    created = client.post("/v1/custom-exercises", headers=user["headers"],
                          json={"name": "Rowing zz", "measurement_type": "duration", "target_amount": 10})
    assert created.status_code == 201, created.get_json()
    cid = created.get_json()["id"]
    assert client.put(f"/v1/custom-exercises/{cid}", json={"name": "Rowing zz2"},
                      headers=user["headers"]).status_code == 200
    assert client.delete(f"/v1/custom-exercises/{cid}", headers=user["headers"]).status_code == 200

    inv = client.post("/v1/relationships", json={"relation_type": "student", "name": "Kid"},
                      headers=user["headers"])
    assert inv.status_code == 201
    rid = inv.get_json()["id"]
    assert client.get(f"/v1/relationships/{rid}/pending-entries", headers=user["headers"]).status_code == 200
    assert client.delete(f"/v1/relationships/{rid}", headers=user["headers"]).status_code == 200


def test_uppercase_uuid_path_id_is_still_accepted(client, make_user):
    user = make_user()
    r = client.delete(f"/v1/relationships/{UNKNOWN_UUID.upper()}", headers=user["headers"])
    assert r.status_code == 404
    assert r.get_json()["error"]["message"] == "No such managed relationship."


# ── BUG-02 / 03 / 05 ────────────────────────────────────────────────────

def test_add_program_entry_without_activity_type_is_422(client, make_user):
    user = make_user()
    for body in ({}, {"activity_type": ""}, {"activity_type": "   "}):
        r = client.post("/v1/program/entries", json=body, headers=user["headers"])
        assert r.status_code == 422, (body, r.get_json())


def test_add_program_entry_unknown_activity_is_still_404(client, make_user):
    user = make_user()
    r = client.post("/v1/program/entries", json={"activity_type": "not_a_real_activity"},
                    headers=user["headers"])
    assert r.status_code == 404


def test_onboarding_requires_condition_codes_but_accepts_explicit_empty(client, make_user):
    # make_user already onboards once; a second POST is the cheapest way to
    # exercise the route's validation on the same account.
    user = make_user()
    base = {"age": 25, "goal": "fitness", "experience_band": "beginner", "unit_preference": "km"}
    r = client.post("/v1/profile/onboarding", json=base, headers=user["headers"])
    assert r.status_code == 422
    assert "condition_codes" in r.get_json()["error"]["message"]
    r = client.post("/v1/profile/onboarding", json={**base, "condition_codes": []}, headers=user["headers"])
    assert r.status_code == 200, r.get_json()


def test_workout_session_requires_at_least_one_activity(client, make_user):
    user = make_user()
    r = client.post("/v1/workout-sessions", json={"activity_types": []}, headers=user["headers"])
    assert r.status_code == 422
    r = client.post("/v1/workout-sessions", json={}, headers=user["headers"])
    assert r.status_code == 422
    r = client.post("/v1/workout-sessions", json={"activity_types": ["walk"]}, headers=user["headers"])
    assert r.status_code == 201
