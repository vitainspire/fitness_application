"""Regression tests for the systemic validation gap found in the code audit:
almost no free-form JSON field in app/profile/routes.py was type-checked
before use, so a malformed-but-easy-to-send payload crashed with an
uncaught exception (a 500, misclassified as a server crash) instead of a
clean 422. Every case here previously raised AttributeError/TypeError/
OverflowError deep in a route or service function; all must now return 422.
"""
from app.platform.db import session_scope

ACTIVITY = "glute_bridge"


def test_non_string_goal_returns_422_not_500(make_user, client):
    user = make_user()
    r = client.post(
        "/v1/profile/onboarding",
        json={"age": 25, "goal": 123, "experience_band": "beginner",
              "condition_codes": ["none"], "unit_preference": "km"},
        headers=user["headers"],
    )
    assert r.status_code == 422


def test_non_list_condition_codes_returns_422_not_500(make_user, client):
    user = make_user()
    r = client.post(
        "/v1/profile/onboarding",
        json={"age": 25, "goal": "health", "experience_band": "beginner",
              "condition_codes": 123, "unit_preference": "km"},
        headers=user["headers"],
    )
    assert r.status_code == 422


def test_non_string_measurement_type_returns_422_not_500(make_user, client):
    user = make_user()
    r = client.post(
        "/v1/custom-exercises",
        json={"name": "My Exercise", "measurement_type": ["reps"], "target_amount": 10},
        headers=user["headers"],
    )
    assert r.status_code == 422


def test_invalid_unit_preference_on_onboarding_returns_422(make_user, client):
    user = make_user()
    r = client.post(
        "/v1/profile/onboarding",
        json={"age": 25, "goal": "health", "experience_band": "beginner",
              "condition_codes": ["none"], "unit_preference": "lightyears"},
        headers=user["headers"],
    )
    assert r.status_code == 422


def test_duplicate_condition_codes_do_not_crash(make_user, client):
    user = make_user()
    r = client.post(
        "/v1/profile/onboarding",
        json={"age": 25, "goal": "health", "experience_band": "beginner",
              "condition_codes": ["gout", "gout"], "unit_preference": "km"},
        headers=user["headers"],
    )
    assert r.status_code == 200


def test_huge_days_on_activity_detail_returns_422_not_500(make_user, client):
    """Found live: ?days=1000000 overflowed Python's date range with an
    OverflowError, which isn't a ValueError, so it wasn't caught either."""
    user = make_user()
    r = client.get("/v1/dashboard/activity-detail?days=1000000", headers=user["headers"])
    assert r.status_code == 422


def test_huge_explicit_date_range_on_activity_detail_returns_422(make_user, client):
    user = make_user()
    r = client.get(
        "/v1/dashboard/activity-detail?start=0001-01-01&end=9999-12-31",
        headers=user["headers"],
    )
    assert r.status_code == 422


def test_non_numeric_amount_on_log_returns_422_not_500(make_user, client):
    user = make_user()
    r = client.post(
        "/v1/activities",
        json={"activity_type": ACTIVITY, "amount": [1, 2, 3], "sets": 1},
        headers=user["headers"],
    )
    assert r.status_code == 422


def test_extreme_amount_on_log_returns_422_not_a_db_overflow(make_user, client):
    user = make_user()
    r = client.post(
        "/v1/activities",
        json={"activity_type": ACTIVITY, "amount": 999999999999, "sets": 1},
        headers=user["headers"],
    )
    assert r.status_code == 422


def test_extreme_sets_on_log_returns_422_not_a_db_overflow(make_user, client):
    user = make_user()
    r = client.post(
        "/v1/activities",
        json={"activity_type": ACTIVITY, "amount": 5,
              "sets": 99999999999999999999},
        headers=user["headers"],
    )
    assert r.status_code == 422


def test_overlong_client_entry_id_returns_422_not_500(make_user, client):
    user = make_user()
    r = client.post(
        "/v1/activities",
        json={"activity_type": ACTIVITY, "amount": 5, "sets": 1,
              "client_entry_id": "x" * 100},
        headers=user["headers"],
    )
    assert r.status_code == 422


def test_non_hashable_goal_on_profile_update_returns_422_not_500(make_user, client):
    """Found live (second-pass audit): PUT /v1/profile never routed through
    the validation helpers at all — it passed d.items() straight through,
    and service.update_profile's `goal not in GOALS` calls hash() on the raw
    value, raising an uncaught TypeError for a non-hashable JSON type."""
    user = make_user()
    r = client.put("/v1/profile", json={"goal": ["a"]}, headers=user["headers"])
    assert r.status_code == 422


def test_non_hashable_experience_band_on_profile_update_returns_422(make_user, client):
    user = make_user()
    r = client.put("/v1/profile", json={"experience_band": {}}, headers=user["headers"])
    assert r.status_code == 422


def test_non_hashable_unit_preference_on_profile_update_returns_422(make_user, client):
    user = make_user()
    r = client.put("/v1/profile", json={"unit_preference": [1, 2]}, headers=user["headers"])
    assert r.status_code == 422


def test_age_above_max_on_profile_update_returns_422_not_a_db_overflow(make_user, client):
    """Found live (second-pass audit): only a floor (>=18) was ever checked,
    no ceiling — an extreme age passed the app check and only failed at
    flush with an uncaught DB error against the 4-byte Integer column."""
    user = make_user()
    r = client.put("/v1/profile", json={"age": 10**9}, headers=user["headers"])
    assert r.status_code == 422


def test_non_list_entries_on_log_returns_422_not_500(make_user, client):
    """Found live (second-pass audit): `entries = d.get("entries") or [d]`
    never checked entries was actually a list before iterating it — a
    truthy non-list (e.g. an int) raised an uncaught TypeError."""
    user = make_user()
    r = client.post("/v1/activities", json={"entries": 5}, headers=user["headers"])
    assert r.status_code == 422


def test_non_string_element_in_condition_codes_returns_422(make_user, client):
    """Found live (second-pass audit): _list_field only validated the outer
    container was a list, not its elements — a non-string element survived
    to complete_onboarding's dict.fromkeys(...) dedup, which calls hash() on
    each item and crashed uncaught on an unhashable one."""
    user = make_user()
    r = client.post(
        "/v1/profile/onboarding",
        json={"age": 25, "goal": "health", "experience_band": "beginner",
              "condition_codes": ["none", {"x": 1}], "unit_preference": "km"},
        headers=user["headers"],
    )
    assert r.status_code == 422


def test_hashable_non_string_element_in_condition_codes_returns_422(make_user, client):
    """A hashable-but-non-string element (an int) survives dict.fromkeys
    dedup and used to crash later at ', '.join(unknown) instead."""
    user = make_user()
    r = client.post(
        "/v1/profile/onboarding",
        json={"age": 25, "goal": "health", "experience_band": "beginner",
              "condition_codes": ["none", 5], "unit_preference": "km"},
        headers=user["headers"],
    )
    assert r.status_code == 422


def test_total_volume_overflow_returns_422_not_a_db_overflow(make_user, client):
    """Found live (second-pass code audit): amount and sets were each
    bounded individually (MAX_LOGGABLE_AMOUNT/MAX_LOGGABLE_SETS), but their
    PRODUCT can still overflow Numeric(10,2) even when both factors are
    individually under their own caps — amount=100000, sets=1000 (both
    under 100_000/10_000) already gives total=1e8, over the ~99,999,999.99
    ceiling."""
    user = make_user()
    r = client.post(
        "/v1/activities",
        json={"activity_type": "glute_bridge", "amount": 100000, "sets": 1000},
        headers=user["headers"],
    )
    assert r.status_code == 422


def test_nan_amount_returns_422_not_500(make_user, client):
    """Found live (second-pass code audit): Python's json module accepts
    bare NaN by default. NaN comparisons are always False, so the amount
    bound checks silently passed, and execution crashed later with an
    uncaught decimal.InvalidOperation comparing NaN against the
    plausible_max_total threshold."""
    import json
    user = make_user()
    r = client.post(
        "/v1/activities",
        data=json.dumps({"activity_type": "glute_bridge", "amount": float("nan"), "sets": 2}),
        content_type="application/json",
        headers=user["headers"],
    )
    assert r.status_code == 422


def test_infinite_sets_returns_422_not_500(make_user, client):
    """Found live (second-pass code audit): int(float('inf')) raises
    OverflowError, not ValueError/TypeError, so _int_field's except clause
    missed it — {"sets": Infinity} crashed uncaught."""
    import json
    user = make_user()
    r = client.post(
        "/v1/activities",
        data=json.dumps({"activity_type": "glute_bridge", "amount": 5, "sets": float("inf")}),
        content_type="application/json",
        headers=user["headers"],
    )
    assert r.status_code == 422


def test_streak_excludes_implausible_logs(make_user):
    """Found live: streak() didn't filter implausible=True, unlike every
    other date-aggregation function in this file."""
    from datetime import date

    from app.profile import service as profile_service
    from app.profile.models import ActivityLog

    user = make_user()
    with session_scope() as s:
        s.add(ActivityLog(
            user_id=user["user_id"], activity_type=ACTIVITY, sets_done=2,
            amount_per_set=5, unit="count", total_volume=10,
            local_date=date(2026, 8, 25), implausible=True,
        ))
        s.flush()
        result = profile_service.streak(s, user["user_id"])
        assert result["current"] == 0
        assert result["active_days"] == 0
