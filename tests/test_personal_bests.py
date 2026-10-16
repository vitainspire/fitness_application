"""Tests for the Personal Bests feature: a flagged-implausible log for a
distance/duration activity (walk, run, cycle, swim) that ranks in the user's
own top 5 for that activity is treated as a genuine extraordinary effort, not
a typo/glitch. It still counts toward streak()/suggestions_today()'s
"completed today", unlike an ordinary implausible log — but, same as any
implausible log, must never be the only thing feeding progression's weekly
target math. Reps/sets exercises (e.g. glute_bridge) are entirely unaffected.
"""
from app.platform.db import session_scope
from app.profile import service


def test_implausible_walk_in_top_5_is_extraordinary_and_counts_for_streak(make_user):
    user = make_user(condition_codes=["none"])
    # Separate transactions, like two real requests would be — otherwise the
    # program entry's added_at and the log's logged_at (both DB-side `now()`)
    # can land in either order within one flush, which is a test artifact,
    # not anything a real request sequence could hit.
    with session_scope() as s:
        service.add_program_entry(s, user["user_id"], "walk")
    with session_scope() as s:
        # walk's plausible_max_total is 15 (data/activity_taxonomy.csv).
        result = service.log_activity(s, user["user_id"], activity_type="walk", amount=20, sets=1)
        assert result["implausible"] is True
        assert result["extraordinary"] is True

        suggestions = service.suggestions_today(s, user["user_id"])
        walk_item = next(i for i in suggestions["all_items"] if i["activity_type"] == "walk")
        assert walk_item["completed"] is True

        streak = service.streak(s, user["user_id"])
        assert streak["current"] == 1

        bests = service.list_personal_bests(s, user["user_id"])
        assert bests["walk"][0]["value"] == 20.0


def test_personal_best_list_has_exactly_one_best_per_exercise(make_user):
    user = make_user(condition_codes=["none"])
    with session_scope() as s:
        for amount in [16, 17, 18, 19, 20, 21]:  # all above the 15km plausible ceiling
            service.log_activity(s, user["user_id"], activity_type="walk", amount=amount, sets=1,
                                  client_entry_id=f"walk-{amount}")
        bests = service.list_personal_bests(s, user["user_id"])
        assert [b["value"] for b in bests["walk"]] == [21.0]  # one record, the best one


def test_implausible_rep_based_log_is_never_extraordinary(make_user):
    """Reps/sets exercises (anything that isn't distance/duration) must keep
    today's unchanged behaviour: flagged implausible, excluded from streak,
    never recorded as a personal best."""
    user = make_user(condition_codes=["none"])
    with session_scope() as s:
        service.add_program_entry(s, user["user_id"], "glute_bridge")
        result = service.log_activity(s, user["user_id"], activity_type="glute_bridge",
                                      amount=5000, sets=1)
        assert result["implausible"] is True
        assert result["extraordinary"] is False

        suggestions = service.suggestions_today(s, user["user_id"])
        item = next(i for i in suggestions["all_items"] if i["activity_type"] == "glute_bridge")
        assert item["completed"] is False

        streak = service.streak(s, user["user_id"])
        assert streak["current"] == 0

        bests = service.list_personal_bests(s, user["user_id"])
        assert "glute_bridge" not in bests


def test_extraordinary_log_still_excluded_from_progression_adherence(make_user):
    """An extraordinary effort must not inflate next week's targets — it
    stays excluded from progression math exactly like any other implausible
    log, only streak/completed treat it specially."""
    from app.progression.service import _window_adherence
    from app.profile.service import today_ist

    user = make_user(condition_codes=["none"])
    with session_scope() as s:
        service.add_program_entry(s, user["user_id"], "walk")
        result = service.log_activity(s, user["user_id"], activity_type="walk", amount=20, sets=1)
        assert result["extraordinary"] is True
        # The only log this window is the extraordinary one; adherence must
        # still see it as excluded (0.0), not count the inflated 20.
        adherence = _window_adherence(s, user["user_id"], "walk", target_volume=5.0,
                                      today=today_ist())
        assert adherence == 0.0
