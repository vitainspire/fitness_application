"""One personal best per exercise the user has logged, ranked in the exercise's
own terms: reps -> the most reps in a SINGLE set, hold -> longest single hold,
distance / duration -> the amount. Derived from the saved entries."""
from app.platform.db import session_scope
from app.profile import service


def _log(s, user, **kw):
    return service.log_activity(s, user["user_id"], **kw)


def test_reps_best_is_the_most_reps_in_a_single_set(make_user):
    user = make_user()
    with session_scope() as s:
        _log(s, user, activity_type="glute_bridge", set_amounts=[10, 10])      # best set 10
        _log(s, user, activity_type="glute_bridge", set_amounts=[24, 20])      # best set 24  <- record
        _log(s, user, activity_type="glute_bridge", set_amounts=[5])           # best set 5
        best = service.list_personal_bests(s, user["user_id"])["glute_bridge"]
        assert len(best) == 1
        assert best[0]["value"] == 24.0 and best[0]["unit"] == "count"   # not 44, the two sets added up


def test_reps_best_for_an_entry_without_a_breakdown_is_its_per_set_amount(make_user):
    user = make_user()
    with session_scope() as s:
        _log(s, user, activity_type="glute_bridge", amount=15, sets=2)         # 2 x 15 -> 15, not 30
        assert service.list_personal_bests(s, user["user_id"])["glute_bridge"][0]["value"] == 15.0


def test_custom_reps_exercise_best_is_its_per_set_amount(make_user):
    user = make_user()
    with session_scope() as s:
        c = service.create_custom_exercise(s, user["user_id"], name="Skipping zz", measurement_type="reps",
                                           target_amount=10, target_sets=2)
        _log(s, user, custom_exercise_id=c["id"], amount=30, sets=2)
        _log(s, user, custom_exercise_id=c["id"], amount=45, sets=1)
        assert service.list_personal_bests(s, user["user_id"])[c["id"]][0]["value"] == 45.0


def test_hold_best_is_the_longest_single_hold(make_user):
    user = make_user()
    with session_scope() as s:
        _log(s, user, activity_type="neck_isometric", set_amounts=[10, 10, 10])   # total 30, longest 10
        _log(s, user, activity_type="neck_isometric", set_amounts=[25])            # longest 25
        best = service.list_personal_bests(s, user["user_id"])["neck_isometric"]
        assert len(best) == 1 and best[0]["value"] == 25.0 and best[0]["unit"] == "sec"


def test_distance_best_is_the_longest_effort(make_user):
    user = make_user()
    with session_scope() as s:
        for amt in (1.0, 3.5, 2.0):
            _log(s, user, activity_type="walk", amount=amt, sets=1)
        best = service.list_personal_bests(s, user["user_id"])["walk"]
        assert len(best) == 1 and best[0]["value"] == 3.5 and best[0]["unit"] == "km"


def test_custom_exercises_get_one_best_too(make_user):
    user = make_user()
    with session_scope() as s:
        c = service.create_custom_exercise(s, user["user_id"], name="Rowing zz", measurement_type="duration",
                                           target_amount=10, target_sets=1)
        for amt in (10, 25, 15):
            _log(s, user, custom_exercise_id=c["id"], amount=amt, sets=1)
        best = service.list_personal_bests(s, user["user_id"])[c["id"]]
        assert len(best) == 1 and best[0]["value"] == 25.0 and best[0]["display_name"] == "Rowing zz"


def test_eight_exercises_done_means_exactly_eight_records(make_user):
    user = make_user()
    types = ["glute_bridge", "dead_bug", "shoulder_circles", "wrist_circles",
             "ankle_circles", "neck_isometric", "walk", "running"]
    with session_scope() as s:
        for t in types:
            for _ in range(3):  # several entries each must still be one record
                if t in ("walk", "running"):
                    _log(s, user, activity_type=t, amount=1.0, sets=1)
                else:
                    _log(s, user, activity_type=t, amount=5, sets=2)
        bests = service.list_personal_bests(s, user["user_id"])
        assert sorted(bests) == sorted(types)
        assert all(len(v) == 1 for v in bests.values())


def test_an_exercise_never_done_has_no_record(make_user):
    user = make_user()
    with session_scope() as s:
        _log(s, user, activity_type="glute_bridge", amount=5, sets=2)
        assert list(service.list_personal_bests(s, user["user_id"])) == ["glute_bridge"]


def test_a_plain_implausible_entry_is_ignored(make_user):
    user = make_user()
    with session_scope() as s:
        _log(s, user, activity_type="glute_bridge", amount=5, sets=2)
        _log(s, user, activity_type="glute_bridge", amount=100000, sets=1)   # absurd, flagged
        assert service.list_personal_bests(s, user["user_id"])["glute_bridge"][0]["value"] == 5.0


def test_correcting_an_entry_in_the_player_corrects_the_record(make_user):
    user = make_user()
    with session_scope() as s:
        sid = service.start_workout_session(s, user["user_id"], ["glute_bridge"])["id"]
        service.log_activity(s, user["user_id"], activity_type="glute_bridge", set_amounts=[25, 25],
                             workout_session_id=sid, replace_today=True)
        assert service.list_personal_bests(s, user["user_id"])["glute_bridge"][0]["value"] == 25.0
        service.log_activity(s, user["user_id"], activity_type="glute_bridge", set_amounts=[15, 15],
                             workout_session_id=sid, replace_today=True)
        assert service.list_personal_bests(s, user["user_id"])["glute_bridge"][0]["value"] == 15.0
