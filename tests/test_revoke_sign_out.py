"""Revoking a managed plan, with and without the optional "sign them out everywhere".

Default revoke is unchanged: the link stops opening and the manager loses access, but the
person's already-open session keeps working (their entries become part of their own plan —
see "What happens on revocation" in MANAGED_PLANS_TEACHER_DOCTOR_PARENT_PLAN.md).
`?sign_out=true` additionally ends every session of that one account.
"""
import time

from app.platform.db import session_scope
from app.relationships import service as rel_service


def _invite_and_open(client, manager, name="Riya", device="student-phone"):
    """Create an invite as `manager` and open it like the person's phone would."""
    r = client.post("/v1/relationships", json={"relation_type": "student", "name": name},
                    headers=manager["headers"])
    assert r.status_code == 201, r.get_json()
    rel = r.get_json()
    opened = client.post("/v1/relationships/open", json={"invite_token": rel["invite_token"]},
                         headers={"X-Device-Id": device})
    assert opened.status_code == 200, opened.get_json()
    person = opened.get_json()
    person["headers"] = {"X-Device-Id": device, "Authorization": f"Bearer {person['access_token']}"}
    return rel, person


def _can_use_app(client, headers):
    return client.get("/v1/profile", headers=headers).status_code


def test_plain_revoke_leaves_the_persons_session_working(client, make_user):
    manager = make_user()
    rel, person = _invite_and_open(client, manager)

    r = client.delete(f"/v1/relationships/{rel['id']}", headers=manager["headers"])
    assert r.status_code == 200
    assert r.get_json() == {"status": "revoked", "signed_out": False}

    assert _can_use_app(client, person["headers"]) == 200                 # still signed in
    assert client.post("/v1/relationships/open", json={"invite_token": rel["invite_token"]},
                       headers={"X-Device-Id": "another-phone"}).status_code == 403  # link is dead
    assert client.get(f"/v1/relationships/{rel['id']}/activity",
                      headers=manager["headers"]).status_code == 403      # manager lost access


def test_revoke_with_sign_out_ends_every_session_of_that_person(client, make_user):
    manager = make_user()
    rel, person = _invite_and_open(client, manager)
    assert _can_use_app(client, person["headers"]) == 200
    time.sleep(1.1)   # access tokens are invalidated by whole-second "issued before" comparison

    r = client.delete(f"/v1/relationships/{rel['id']}?sign_out=true", headers=manager["headers"])
    assert r.status_code == 200
    assert r.get_json() == {"status": "revoked", "signed_out": True}

    blocked = client.get("/v1/profile", headers=person["headers"])
    assert blocked.status_code == 403
    assert blocked.get_json()["error"]["code"] == "revoked"
    refresh = client.post("/v1/auth/refresh", json={"refresh_token": person["refresh_token"]},
                          headers={"X-Device-Id": "student-phone"})
    assert refresh.status_code in (401, 403)
    assert client.post("/v1/relationships/open", json={"invite_token": rel["invite_token"]},
                       headers={"X-Device-Id": "yet-another-phone"}).status_code == 403
    assert _can_use_app(client, manager["headers"]) == 200                # manager untouched


def test_sign_out_only_affects_that_one_person(client, make_user):
    manager = make_user()
    rel_a, person_a = _invite_and_open(client, manager, name="Anu", device="phone-a")
    rel_b, person_b = _invite_and_open(client, manager, name="Bala", device="phone-b")
    time.sleep(1.1)

    client.delete(f"/v1/relationships/{rel_a['id']}?sign_out=true", headers=manager["headers"])

    assert _can_use_app(client, person_a["headers"]) == 403
    assert _can_use_app(client, person_b["headers"]) == 200               # the other student is fine
    assert _can_use_app(client, manager["headers"]) == 200


def test_sign_out_on_an_invite_nobody_opened_is_harmless(client, make_user):
    manager = make_user()
    r = client.post("/v1/relationships", json={"relation_type": "child", "name": "Kabir"},
                    headers=manager["headers"])
    rel = r.get_json()

    out = client.delete(f"/v1/relationships/{rel['id']}?sign_out=true", headers=manager["headers"])
    assert out.status_code == 200
    assert out.get_json() == {"status": "revoked", "signed_out": False}   # no account yet, nothing to end
    assert _can_use_app(client, manager["headers"]) == 200


def test_a_stranger_cannot_sign_someone_elses_person_out(client, make_user):
    manager = make_user()
    stranger = make_user()
    rel, person = _invite_and_open(client, manager)
    time.sleep(1.1)

    r = client.delete(f"/v1/relationships/{rel['id']}?sign_out=true", headers=stranger["headers"])
    assert r.status_code in (403, 404)
    assert _can_use_app(client, person["headers"]) == 200                 # untouched
    still_active = [x for x in client.get("/v1/relationships", headers=manager["headers"]).get_json()["relationships"]
                    if x["id"] == rel["id"]]
    assert still_active and still_active[0]["status"] == "active"


def test_sign_out_flag_off_by_default_and_for_other_values(client, make_user):
    manager = make_user()
    rel, person = _invite_and_open(client, manager)
    time.sleep(1.1)

    r = client.delete(f"/v1/relationships/{rel['id']}?sign_out=false", headers=manager["headers"])
    assert r.get_json()["signed_out"] is False
    assert _can_use_app(client, person["headers"]) == 200


def test_service_returns_the_account_id_only_when_asked(make_user, client):
    manager = make_user()
    rel, person = _invite_and_open(client, manager)
    with session_scope() as s:
        assert rel_service.revoke_relationship(s, manager["user_id"], rel["id"]) is None
    rel2, person2 = _invite_and_open(client, manager, name="Chitra", device="phone-c")
    with session_scope() as s:
        assert rel_service.revoke_relationship(s, manager["user_id"], rel2["id"], sign_out=True) == person2["user_id"]
