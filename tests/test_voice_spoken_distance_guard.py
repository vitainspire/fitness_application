"""A distance the user SAID is never replaced by a time-based estimate. The model sometimes
leaves the amount empty when a time is also spoken ("walked 3 km in 40 minutes"); the
service then reads the written distance itself, and only estimates from the pace when no
distance was spoken at all."""
import json

import pytest

from app.platform.db import session_scope
from app.voice import service as voice_service


def _extract(monkeypatch, user, transcript, entries):
    monkeypatch.setattr(voice_service, "chat_complete",
                        lambda *a, **k: json.dumps({"reply_text": "ok", "entries": entries}))
    with session_scope() as s:
        return voice_service.extract_activities(s, user["user_id"], transcript, openrouter_api_key="unused")["entries"]


def _e(code, sets, minutes=None):
    return {"activity_type": code, "activity_name": None, "is_custom": False,
            "set_amounts": sets, "unit": "km", "duration_minutes": minutes}


# ── the bug: model left the amount empty although a distance was spoken ─────

@pytest.mark.parametrize("transcript,expected", [
    ("I walked 3 km in 40 minutes", 3.0),
    ("walked 2 km in 15 minutes", 2.0),
    ("I walked 2.5 kilometers in 30 minutes", 2.5),
    ("I walked 3 kilometres in 40 minutes", 3.0),
    ("I ran 5k in 25 minutes", 5.0),
    ("I ran 3 miles in 30 minutes", 4.83),
    ("I walked 800 meters in 10 minutes", 0.8),
    ("I walked 800 metres in 10 minutes", 0.8),
])
def test_spoken_distance_wins_when_the_model_left_the_amount_empty(make_user, monkeypatch, transcript, expected):
    code = "running" if "ran" in transcript else "walk"
    entries = _extract(monkeypatch, make_user(), transcript, [_e(code, [], 40)])
    assert len(entries) == 1
    assert entries[0]["set_amounts"] == [expected] and entries[0]["unit"] == "km"
    assert "estimated_from_minutes" not in entries[0]          # not an estimate: the user said it


def test_model_that_got_it_right_is_untouched(make_user, monkeypatch):
    entries = _extract(monkeypatch, make_user(), "walked 2 km in 15 minutes", [_e("walk", [2], 15)])
    assert entries[0]["set_amounts"] == [2] and "estimated_from_minutes" not in entries[0]


# ── time only: unchanged behaviour ──────────────────────────────────────────

@pytest.mark.parametrize("transcript,code,expected", [
    ("I walked for 30 minutes", "walk", 2.5),
    ("I ran for 30 minutes", "running", 4.29),
])
def test_time_only_is_still_estimated_from_the_pace(make_user, monkeypatch, transcript, code, expected):
    e = _extract(monkeypatch, make_user(), transcript, [_e(code, [], 30)])[0]
    assert e["set_amounts"] == [expected] and e["estimated_from_minutes"] == 30


def test_minutes_are_not_mistaken_for_a_distance(make_user, monkeypatch):
    # "30 min", "30 minutes", "30m" must never count as meters/miles
    for phrase in ("I walked for 30 min", "I walked for 30 minutes", "I walked for 30m"):
        e = _extract(monkeypatch, make_user(), phrase, [_e("walk", [], 30)])
        assert e and e[0]["set_amounts"] == [2.5], phrase


# ── several entries ─────────────────────────────────────────────────────────

def test_a_distance_already_used_by_one_entry_is_not_given_to_another(make_user, monkeypatch):
    # walk got its 2 km from the model; running was time-only -> estimate, not "2 km"
    out = _extract(monkeypatch, make_user(), "I walked 2 km and ran for 30 minutes",
                   [_e("walk", [2]), _e("running", [], 30)])
    by = {e["activity_type"]: e for e in out}
    assert by["walk"]["set_amounts"] == [2]
    assert by["running"]["set_amounts"] == [4.29] and by["running"]["estimated_from_minutes"] == 30


def test_two_entries_waiting_for_one_distance_are_not_guessed(make_user, monkeypatch):
    out = _extract(monkeypatch, make_user(), "I walked 2 km in 15 minutes and ran for 20 minutes",
                   [_e("walk", [], 15), _e("running", [], 20)])
    assert out == []                                           # ambiguous: nothing invented


def test_a_distance_written_in_words_is_never_replaced_by_an_estimate(make_user, monkeypatch):
    out = _extract(monkeypatch, make_user(), "I walked two kilometers in 15 minutes", [_e("walk", [], 15)])
    assert out == []                                           # can't read it safely, so no invented number


def test_non_distance_exercises_are_unaffected(make_user, monkeypatch):
    out = _extract(monkeypatch, make_user(), "I did 3 sets of 10 glute bridges in 5 minutes",
                   [{"activity_type": "glute_bridge", "activity_name": None, "is_custom": False,
                     "set_amounts": [10, 10, 10], "unit": "count", "duration_minutes": 5}])
    assert out[0]["set_amounts"] == [10, 10, 10] and out[0]["unit"] == "count"


# ── helpers and prompt ──────────────────────────────────────────────────────

def test_spoken_distance_parser():
    f = voice_service._spoken_distances_km
    assert f("walked 3 km and 800 meters") == [3.0, 0.8]
    assert f("ran 1.5 miles") == [2.41]
    assert f("a 10k race") == [10.0]
    assert f("walked for 30 minutes 20 min 15m") == []
    assert f("") == [] and f(None) == []


def test_the_prompt_tells_the_model_to_keep_the_spoken_distance(make_user, monkeypatch):
    seen = {}
    monkeypatch.setattr(voice_service, "chat_complete",
                        lambda messages, *a, **k: seen.update(p=messages[0]["content"]) or '{"entries": []}')
    with session_scope() as s:
        voice_service.extract_activities(s, make_user()["user_id"], "x", openrouter_api_key="unused")
    assert "Never leave set_amounts empty when a distance was spoken" in seen["p"]
