"""A custom (non-taxonomy) exercise from voice must come back in a form the save accepts:
unit count / km / min only, and time or distance as ONE effort (3 x 30 s plank -> 0.5, 0.5, 0.5 min, each set kept)."""
import json

import pytest

from app.platform.db import session_scope
from app.voice import service as voice_service


def _extract(monkeypatch, user, entries):
    monkeypatch.setattr(voice_service, "chat_complete",
                        lambda *a, **k: json.dumps({"reply_text": "ok", "entries": entries}))
    with session_scope() as s:
        return voice_service.extract_activities(s, user["user_id"], "x", openrouter_api_key="unused")["entries"]


def _custom(name, amounts, unit):
    return {"activity_type": None, "activity_name": name, "is_custom": True,
            "set_amounts": amounts, "unit": unit, "duration_minutes": None}


@pytest.mark.parametrize("unit,amounts,expected_unit,expected", [
    ("sec", [30, 30, 30], "min", [0.5, 0.5, 0.5]),  # the plank case: sets kept
    ("seconds", [90], "min", [1.5]),
    ("min", [45], "min", [45]),
    ("minutes", [10, 10], "min", [10, 10]),
    ("hours", [1], "min", [60]),
    ("m", [500], "km", [0.5]),
    ("meters", [250, 250], "km", [0.25, 0.25]),
    ("km", [5], "km", [5]),
    ("miles", [2], "km", [3.22]),
    ("count", [20, 20, 20], "count", [20, 20, 20]),  # reps keep their sets
    ("reps", [15, 15], "count", [15, 15]),
    (None, [10, 10], "count", [10, 10]),
])
def test_custom_units_are_normalised(make_user, monkeypatch, unit, amounts, expected_unit, expected):
    e = _extract(monkeypatch, make_user(), [_custom("thing zz", amounts, unit)])
    assert len(e) == 1 and e[0]["unit"] == expected_unit and e[0]["set_amounts"] == expected


@pytest.mark.parametrize("unit,amounts", [("kg", [60]), ("lbs", [100]), ("furlongs", [2]), ("sec", [0.001]), ("min", [0, -5])])
def test_unconvertible_custom_entries_are_dropped_not_saved_wrongly(make_user, monkeypatch, unit, amounts):
    assert _extract(monkeypatch, make_user(), [_custom("thing zz", amounts, unit)]) == []


def test_built_in_exercises_keep_their_own_units(make_user, monkeypatch):
    e = _extract(monkeypatch, make_user(), [{"activity_type": "neck_isometric", "activity_name": None,
                                             "is_custom": False, "set_amounts": [30, 30, 30], "unit": "sec"}])
    assert e[0]["unit"] == "sec" and e[0]["set_amounts"] == [30, 30, 30]


def test_plank_in_seconds_can_now_be_confirmed(client, make_user, monkeypatch):
    """The exact failure: custom plank 3 x 30 s used to be created as a reps exercise and then rejected
    with "plank is measured in count, not sec." — failing the whole batch."""
    user = make_user()
    e = _extract(monkeypatch, user, [_custom("plank", [30, 30, 30], "sec"),
                                     _custom("push-ups", [40, 40, 40], "count")])
    payload = []
    for item in e:   # the same steps the frontend's confirmVoiceLog performs
        sets = len(item["set_amounts"])
        amount = sum(item["set_amounts"]) / sets
        mt = "distance" if item["unit"] == "km" else "duration" if item["unit"] == "min" else "reps"
        created = client.post("/v1/custom-exercises", headers=user["headers"], json={
            "name": item["activity_name"], "measurement_type": mt, "target_amount": amount, "target_sets": sets})
        assert created.status_code == 201, created.get_json()
        cid = created.get_json()["id"]
        if item["unit"] in ("km", "min"):   # one entry per spoken set for time / distance
            payload += [{"custom_exercise_id": cid, "amount": v, "sets": 1, "unit": item["unit"],
                         "source": "voice"} for v in item["set_amounts"]]
        else:
            payload.append({"custom_exercise_id": cid, "amount": amount, "sets": sets,
                            "unit": item["unit"], "source": "voice"})
    assert [p["amount"] for p in payload[:3]] == [0.5, 0.5, 0.5]              # plank: three separate sets
    r = client.post("/v1/activities", json={"entries": payload}, headers=user["headers"])
    assert r.status_code == 201, r.get_json()
    assert len(r.get_json()["logged"]) == 4                                   # 3 plank sets + 1 push-up entry
