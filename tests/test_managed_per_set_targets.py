"""Per-set ("different reps for each set") targets for MANAGED plans — the
manager's Add screen, both for a live managed person and for an entry staged
before the invite link is opened. Mirrors what the self-service Edit/Add screens
have always supported (see test_per_set_progression.py)."""
import pytest

from app.platform.config import load_config
from app.platform.db import session_scope
from app.platform.errors import ValidationError
from app.profile import service as profile_service
from app.relationships import service as rel_service


def _open(session, invite_token):
    _pair, user, _row = rel_service.open_invite(session, load_config(), invite_token, "test-device")
    return user.id


def _live_managed(manager):
    """A claimed relationship with glute_bridge already on the managed plan."""
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student", "Priya")
        managed_id = _open(s, invite["invite_token"])
        profile_service.add_program_entry(s, managed_id, "glute_bridge",
                                          acting_manager_id=manager["user_id"])
    return managed_id


# ── live managed person: PUT /program/entries/<type>/target + managed_user_id ──

def test_manager_can_set_per_set_reps_on_a_live_plan(make_user):
    manager = make_user()
    managed_id = _live_managed(manager)
    with session_scope() as s:
        r = profile_service.set_managed_target(
            s, manager["user_id"], managed_id, "glute_bridge",
            target_sets=1, target_amount=0.0, set_amounts=[12, 10, 8])
        assert r["set_targets"] == [12, 10, 8]
        assert r["target_sets"] == 3
        assert r["target_amount"] == 10  # average, whole reps
        entry = s.get(profile_service.UserProgram, (managed_id, "glute_bridge"))
        assert [float(v) for v in entry.set_targets] == [12, 10, 8]


def test_manager_per_set_reps_are_rounded_to_whole_numbers(make_user):
    manager = make_user()
    managed_id = _live_managed(manager)
    with session_scope() as s:
        r = profile_service.set_managed_target(
            s, manager["user_id"], managed_id, "glute_bridge",
            target_sets=1, target_amount=0.0, set_amounts=[12.4, 9.6])
        assert r["set_targets"] == [12, 10]


@pytest.mark.parametrize("bad", [[], [0], [-3, 5], [float("nan")], [float("inf")], [10 ** 9]])
def test_manager_per_set_rejects_bad_values(make_user, bad):
    manager = make_user()
    managed_id = _live_managed(manager)
    with session_scope() as s:
        with pytest.raises(ValidationError):
            profile_service.set_managed_target(
                s, manager["user_id"], managed_id, "glute_bridge",
                target_sets=1, target_amount=0.0, set_amounts=bad)


def test_manager_per_set_rejects_a_single_effort_activity(make_user):
    manager = make_user()
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student", "Priya")
        managed_id = _open(s, invite["invite_token"])
        profile_service.add_program_entry(s, managed_id, "walk", acting_manager_id=manager["user_id"])
        with pytest.raises(ValidationError):
            profile_service.set_managed_target(
                s, manager["user_id"], managed_id, "walk",
                target_sets=1, target_amount=0.0, set_amounts=[1, 2])


def test_a_later_uniform_manager_edit_clears_the_per_set_breakdown(make_user):
    manager = make_user()
    managed_id = _live_managed(manager)
    with session_scope() as s:
        profile_service.set_managed_target(
            s, manager["user_id"], managed_id, "glute_bridge",
            target_sets=1, target_amount=0.0, set_amounts=[12, 10, 8])
        r = profile_service.set_managed_target(
            s, manager["user_id"], managed_id, "glute_bridge", target_sets=2, target_amount=6)
        assert r["target_sets"] == 2 and r["target_amount"] == 6
        entry = s.get(profile_service.UserProgram, (managed_id, "glute_bridge"))
        assert entry.set_targets is None  # no stale 3-set breakdown next to a 2x6 target


def test_uniform_manager_target_still_works_without_set_amounts(make_user):
    manager = make_user()
    managed_id = _live_managed(manager)
    with session_scope() as s:
        r = profile_service.set_managed_target(
            s, manager["user_id"], managed_id, "glute_bridge", target_sets=3, target_amount=7)
        assert (r["target_sets"], r["target_amount"]) == (3, 7)
        assert "set_targets" not in r


def test_http_put_target_accepts_set_amounts_for_a_manager(client, make_user):
    manager = make_user()
    managed_id = _live_managed(manager)
    r = client.put("/v1/program/entries/glute_bridge/target", headers=manager["headers"],
                   json={"managed_user_id": managed_id, "set_amounts": [15, 12, 10]})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["set_targets"] == [15, 12, 10]
    bad = client.put("/v1/program/entries/glute_bridge/target", headers=manager["headers"],
                     json={"managed_user_id": managed_id, "set_amounts": "12,10"})
    assert bad.status_code == 422


def test_non_manager_cannot_set_per_set_on_someone_elses_plan(client, make_user):
    manager = make_user()
    managed_id = _live_managed(manager)
    stranger = make_user()
    r = client.put("/v1/program/entries/glute_bridge/target", headers=stranger["headers"],
                   json={"managed_user_id": managed_id, "set_amounts": [15, 12]})
    assert r.status_code in (403, 404)


def test_managed_activity_view_exposes_set_targets(make_user):
    manager = make_user()
    managed_id = _live_managed(manager)
    with session_scope() as s:
        profile_service.set_managed_target(
            s, manager["user_id"], managed_id, "glute_bridge",
            target_sets=1, target_amount=0.0, set_amounts=[12, 10, 8])
        rels = rel_service.list_my_relationships(s, manager["user_id"])
        view = rel_service.get_managed_activity(s, manager["user_id"], rels[0]["id"])
        entry = next(e for e in view["managed_entries"] if e["activity_type"] == "glute_bridge")
        assert entry["set_targets"] == [12, 10, 8]


# ── staged before the invite is opened: POST /relationships/<id>/pending-entries ──

def test_pending_per_set_entry_is_staged_listed_and_materialized(make_user):
    manager = make_user()
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student", "Priya")
        staged = rel_service.add_pending_entry(
            s, manager["user_id"], invite["id"], activity_type="glute_bridge",
            target_sets=1, target_amount=0.0, auto_progress=True, set_amounts=[12, 10, 8])
        assert staged["set_amounts"] == [12, 10, 8]
        assert staged["target_sets"] == 3 and staged["target_amount"] == 10
        listed = rel_service.list_pending_entries(s, manager["user_id"], invite["id"])
        assert listed[0]["set_amounts"] == [12, 10, 8]

        managed_id = _open(s, invite["invite_token"])
        entry = s.get(profile_service.UserProgram, (managed_id, "glute_bridge"))
        assert [float(v) for v in entry.set_targets] == [12, 10, 8]
        assert entry.target_sets == 3


def test_pending_uniform_entry_is_unchanged_and_has_no_set_amounts(make_user):
    manager = make_user()
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student", "Priya")
        staged = rel_service.add_pending_entry(
            s, manager["user_id"], invite["id"], activity_type="glute_bridge",
            target_sets=3, target_amount=7, auto_progress=True)
        assert staged["set_amounts"] is None
        managed_id = _open(s, invite["invite_token"])
        entry = s.get(profile_service.UserProgram, (managed_id, "glute_bridge"))
        assert (entry.target_sets, float(entry.target_amount)) == (3, 7)
        assert entry.set_targets is None


def test_restaging_an_entry_switches_it_between_per_set_and_uniform(make_user):
    manager = make_user()
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student", "Priya")
        rel_service.add_pending_entry(
            s, manager["user_id"], invite["id"], activity_type="glute_bridge",
            target_sets=1, target_amount=0.0, auto_progress=True, set_amounts=[12, 10])
        rel_service.add_pending_entry(
            s, manager["user_id"], invite["id"], activity_type="glute_bridge",
            target_sets=2, target_amount=6, auto_progress=True)
        listed = rel_service.list_pending_entries(s, manager["user_id"], invite["id"])
        assert len(listed) == 1
        assert listed[0]["set_amounts"] is None and listed[0]["target_amount"] == 6


def test_pending_per_set_rejects_single_effort_and_bad_values(make_user):
    manager = make_user()
    with session_scope() as s:
        invite = rel_service.create_invite(s, manager["user_id"], "student", "Priya")
        for activity, bad in (("walk", [1, 2]), ("glute_bridge", []), ("glute_bridge", [0, 5])):
            with pytest.raises(ValidationError):
                rel_service.add_pending_entry(
                    s, manager["user_id"], invite["id"], activity_type=activity,
                    target_sets=1, target_amount=0.0, auto_progress=True, set_amounts=bad)


def test_http_pending_entries_accepts_set_amounts(client, make_user):
    manager = make_user()
    inv = client.post("/v1/relationships", json={"relation_type": "student", "name": "Kid"},
                      headers=manager["headers"]).get_json()
    r = client.post(f"/v1/relationships/{inv['id']}/pending-entries", headers=manager["headers"],
                    json={"activity_type": "glute_bridge", "set_amounts": [14, 12], "auto_progress": True})
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["set_amounts"] == [14, 12]
    got = client.get(f"/v1/relationships/{inv['id']}/pending-entries", headers=manager["headers"])
    assert got.get_json()["entries"][0]["set_amounts"] == [14, 12]
    bad = client.post(f"/v1/relationships/{inv['id']}/pending-entries", headers=manager["headers"],
                      json={"activity_type": "glute_bridge", "set_amounts": ["a"]})
    assert bad.status_code == 422
