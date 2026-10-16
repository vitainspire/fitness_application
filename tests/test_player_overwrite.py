"""Workout Player corrections: the player keeps ONE entry per exercise per day.
Saving again (replace_today) replaces it — 20 reps then a corrected 15 reads 15,
never 35 — while voice / GPS / home-card entries keep appending exactly as before."""
import pytest
from sqlalchemy import select

from app.platform.db import session_scope
from app.platform.errors import ValidationError
from app.profile import service
from app.profile.models import ActivityLog, PersonalBest, WorkoutSession


def _session(s, user, types=("glute_bridge",)):
    return service.start_workout_session(s, user["user_id"], list(types))["id"]


def _today_rows(s, user, activity_type):
    return s.scalars(select(ActivityLog).where(
        ActivityLog.user_id == user["user_id"], ActivityLog.activity_type == activity_type,
        ActivityLog.local_date == service.today_ist())).all()


def _save(s, user, sid, set_amounts=None, amount=None, sets=1, activity="glute_bridge", replace=True):
    kw = dict(activity_type=activity, workout_session_id=sid, replace_today=replace)
    if set_amounts is not None:
        kw["set_amounts"] = set_amounts
    else:
        kw.update(amount=amount, sets=sets)
    return service.log_activity(s, user["user_id"], **kw)


def test_saving_again_replaces_instead_of_adding(make_user):
    user = make_user()
    with session_scope() as s:
        sid = _session(s, user)
        _save(s, user, sid, set_amounts=[10, 10])           # first (mistaken) entry
        _save(s, user, sid, set_amounts=[15, 13])           # correction
        rows = _today_rows(s, user, "glute_bridge")
        assert len(rows) == 1
        assert [float(v) for v in rows[0].set_amounts] == [15, 13]
        assert float(rows[0].total_volume) == 28            # 28, not 20 + 28


def test_without_the_flag_entries_still_append(make_user):
    user = make_user()
    with session_scope() as s:
        sid = _session(s, user)
        _save(s, user, sid, set_amounts=[10, 10], replace=False)
        _save(s, user, sid, set_amounts=[15, 13], replace=False)
        assert len(_today_rows(s, user, "glute_bridge")) == 2


def test_replace_works_for_a_single_effort_exercise(make_user):
    user = make_user()
    with session_scope() as s:
        sid = _session(s, user, ("walk",))
        _save(s, user, sid, amount=2.0, activity="walk")
        _save(s, user, sid, amount=3.5, activity="walk")
        rows = _today_rows(s, user, "walk")
        assert len(rows) == 1 and float(rows[0].amount_per_set) == 3.5


def test_replace_across_two_player_sessions_and_session_counts_stay_correct(make_user):
    user = make_user()
    with session_scope() as s:
        a = _session(s, user)
        _save(s, user, a, set_amounts=[10, 10])
        s.get(WorkoutSession, a).status = "completed"       # first visit ended
        b = _session(s, user)                                # reopening the player
        _save(s, user, b, set_amounts=[15, 13])
        rows = _today_rows(s, user, "glute_bridge")
        assert len(rows) == 1 and rows[0].workout_session_id == b
        assert s.get(WorkoutSession, a).completed_activity_count == 0
        assert s.get(WorkoutSession, b).completed_activity_count == 1


def test_replace_does_not_touch_voice_gps_or_home_card_entries(make_user):
    user = make_user()
    with session_scope() as s:
        sid = _session(s, user, ("walk",))
        service.log_activity(s, user["user_id"], activity_type="walk", amount=1.0, source="voice")
        service.log_activity(s, user["user_id"], activity_type="walk", amount=1.5, source="gps",
                             workout_session_id=sid)
        service.log_activity(s, user["user_id"], activity_type="walk", amount=0.5)   # home card
        _save(s, user, sid, amount=2.0, activity="walk")
        _save(s, user, sid, amount=3.0, activity="walk")
        rows = _today_rows(s, user, "walk")
        assert sorted(float(r.amount_per_set) for r in rows) == [0.5, 1.0, 1.5, 3.0]


def test_replace_never_touches_another_users_entries(make_user):
    a, b = make_user(), make_user()
    with session_scope() as s:
        sa, sb = _session(s, a), _session(s, b)
        _save(s, a, sa, set_amounts=[10, 10])
        _save(s, b, sb, set_amounts=[9, 9])
        _save(s, b, sb, set_amounts=[8, 8])
        assert len(_today_rows(s, a, "glute_bridge")) == 1
        assert len(_today_rows(s, b, "glute_bridge")) == 1


def test_a_personal_best_from_the_replaced_entry_is_removed(make_user):
    user = make_user()
    with session_scope() as s:
        sid = _session(s, user)
        first = _save(s, user, sid, set_amounts=[25, 25])
        s.add(PersonalBest(user_id=user["user_id"], activity_type="glute_bridge",
                           value=50, unit="count", source_log_id=first["id"]))
        s.flush()
        _save(s, user, sid, set_amounts=[15, 15])
        assert s.scalars(select(PersonalBest).where(
            PersonalBest.user_id == user["user_id"])).all() == []


def test_an_unrelated_personal_best_is_kept(make_user):
    user = make_user()
    with session_scope() as s:
        sid = _session(s, user)
        first = _save(s, user, sid, set_amounts=[10, 10])
        other = service.log_activity(s, user["user_id"], activity_type="walk", amount=1.0)
        s.add(PersonalBest(user_id=user["user_id"], activity_type="walk", value=1,
                           unit="km", source_log_id=other["id"]))
        s.flush()
        _save(s, user, sid, set_amounts=[15, 15])
        assert len(s.scalars(select(PersonalBest).where(
            PersonalBest.user_id == user["user_id"])).all()) == 1


def test_a_retried_request_with_the_same_client_entry_id_deletes_nothing(make_user):
    user = make_user()
    with session_scope() as s:
        sid = _session(s, user)
        kw = dict(activity_type="glute_bridge", workout_session_id=sid, replace_today=True,
                  set_amounts=[10, 10], client_entry_id="retry-me-1")
        first = service.log_activity(s, user["user_id"], **kw)
        again = service.log_activity(s, user["user_id"], **kw)
        assert again["duplicate"] is True and again["id"] == first["id"]
        assert len(_today_rows(s, user, "glute_bridge")) == 1


@pytest.mark.parametrize("kwargs", [
    dict(activity_type="glute_bridge", amount=5, source="voice"),                 # not a player save
    dict(activity_type="glute_bridge", amount=5),                                  # no session
])
def test_replace_today_is_rejected_outside_a_player_save(make_user, kwargs):
    user = make_user()
    with session_scope() as s:
        sid = _session(s, user)
        if "source" in kwargs:
            kwargs = {**kwargs, "workout_session_id": sid}
        with pytest.raises(ValidationError):
            service.log_activity(s, user["user_id"], replace_today=True, **kwargs)


def test_replace_today_is_rejected_for_a_custom_exercise(make_user):
    user = make_user()
    with session_scope() as s:
        sid = _session(s, user)
        c = service.create_custom_exercise(s, user["user_id"], name="Zumba zz", measurement_type="reps",
                                           target_amount=10, target_sets=1)
        with pytest.raises(ValidationError):
            service.log_activity(s, user["user_id"], custom_exercise_id=c["id"], amount=5,
                                 workout_session_id=sid, replace_today=True)


def test_http_replace_today_flag(client, make_user):
    user = make_user()
    sid = client.post("/v1/workout-sessions", json={"activity_types": ["glute_bridge"]},
                      headers=user["headers"]).get_json()["id"]
    for amounts in ([10, 10], [15, 13]):
        r = client.post("/v1/activities", headers=user["headers"], json={
            "activity_type": "glute_bridge", "set_amounts": amounts,
            "workout_session_id": sid, "replace_today": True})
        assert r.status_code == 201, r.get_json()
    items = client.get("/v1/suggestions", headers=user["headers"]).get_json()["all_items"]
    gb = next(i for i in items if i["activity_type"] == "glute_bridge")
    assert gb["player_log"]["set_amounts"] == [15, 13] and gb["player_log"]["sets"] == 2
    bad = client.post("/v1/activities", headers=user["headers"], json={
        "activity_type": "glute_bridge", "amount": 5, "workout_session_id": sid,
        "replace_today": "yes"})
    assert bad.status_code == 422


def test_suggestions_player_log_is_none_until_the_player_saves_and_ignores_other_sources(client, make_user):
    user = make_user()

    def player_log(activity):
        items = client.get("/v1/suggestions", headers=user["headers"]).get_json()["all_items"]
        return next(i for i in items if i["activity_type"] == activity)["player_log"]

    assert player_log("glute_bridge") is None
    client.post("/v1/activities", headers=user["headers"],
                json={"activity_type": "glute_bridge", "amount": 5, "sets": 2})          # home card
    client.post("/v1/activities", headers=user["headers"],
                json={"activity_type": "glute_bridge", "amount": 5, "sets": 2, "source": "voice"})
    assert player_log("glute_bridge") is None                                              # never locks the player
    sid = client.post("/v1/workout-sessions", json={"activity_types": ["glute_bridge"]},
                      headers=user["headers"]).get_json()["id"]
    client.post("/v1/activities", headers=user["headers"], json={
        "activity_type": "glute_bridge", "amount": 7, "sets": 3, "workout_session_id": sid})
    assert player_log("glute_bridge") == {"sets": 3, "amount": 7.0, "set_amounts": None}
