"""Google sign-in: one endpoint for sign-up AND sign-in (POST /v1/auth/google).

Google's token check is faked (monkeypatched), so these tests never contact Google. They cover what our code
decides: new vs returning vs linked users, unverified emails, the feature switch, and deleting a Google account.
"""
import dataclasses
import uuid

import pytest

from app.auth import google as google_mod
from app.platform.errors import Unauthorized

ONBOARD = {"age": 30, "goal": "health", "experience_band": "beginner",
           "condition_codes": ["none"], "unit_preference": "km"}


def _headers():
    u = uuid.uuid4().hex[:12]
    return {"X-Device-Id": f"pytest-google-{u}",
            "X-Forwarded-For": f"10.{int(u[:2], 16)}.{int(u[2:4], 16)}.{int(u[4:6], 16)}"}


def _auth(token, h):
    return {**h, "Authorization": f"Bearer {token}"}


@pytest.fixture
def google_on(app, monkeypatch):
    cfg = app.config["APP_CONFIG"]
    monkeypatch.setitem(app.config, "APP_CONFIG", dataclasses.replace(cfg, google_client_id="test-client-id"))


@pytest.fixture
def fake_google(monkeypatch, google_on):
    """Returns a setter: fake_google(sub=..., email=..., verified=True). The route then 'verifies' to that identity."""
    state = {}

    def verify(credential, client_id):
        assert client_id == "test-client-id"
        if credential == "bad":
            raise Unauthorized("Google sign-in failed. Please try again.")
        return dict(state)

    monkeypatch.setattr(google_mod, "verify_google_id_token", verify)

    def setter(sub=None, email=None, verified=True):
        state.update(sub=sub or uuid.uuid4().hex, email=email or f"pytest.g.{uuid.uuid4().hex[:10]}@example.com",
                     email_verified=verified, name="Test User")
        return state

    return setter


def _post(client, credential="ok", h=None):
    h = h or _headers()
    return client.post("/v1/auth/google", json={"credential": credential}, headers=h), h


def test_new_google_user_is_created_and_sent_to_onboarding(client, fake_google):
    ident = fake_google()
    r, h = _post(client)
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["is_new_user"] is True and body["email"] == ident["email"].lower()
    p = client.get("/v1/profile", headers=_auth(body["access_token"], h)).get_json()
    assert p["onboarded"] is False          # the frontend's route() therefore shows the onboarding screen
    assert p["auth_provider"] == "google"


def test_returning_google_user_goes_straight_in(client, fake_google):
    fake_google()
    r1, h = _post(client)
    a1 = r1.get_json()
    ok = client.post("/v1/profile/onboarding", json=ONBOARD, headers=_auth(a1["access_token"], h))
    assert ok.status_code in (200, 201), ok.get_json()
    r2, h2 = _post(client)                  # same Google identity again (e.g. a new device)
    assert r2.status_code == 200
    a2 = r2.get_json()
    assert a2["is_new_user"] is False and a2["user_id"] == a1["user_id"]
    p = client.get("/v1/profile", headers=_auth(a2["access_token"], h2)).get_json()
    assert p["onboarded"] is True           # route() therefore shows the dashboard directly


def _verify_email_now(email):
    from sqlalchemy import select
    from app.auth import tokens as tk
    from app.auth.models import User
    from app.platform.db import session_scope
    with session_scope() as s:
        s.scalar(select(User).where(User.email == email)).email_verified_at = tk.now_utc()


def test_google_login_links_an_existing_verified_password_account_and_keeps_its_password(client, fake_google):
    email = f"pytest.link.{uuid.uuid4().hex[:10]}@example.com"
    h = _headers()
    s = client.post("/v1/auth/signup", json={"email": email, "password": "A-good-long-passphrase-123"}, headers=h)
    assert s.status_code == 201
    _verify_email_now(email)                                            # the owner already proved the address
    fake_google(email=email)
    r, h2 = _post(client)
    assert r.status_code == 200 and r.get_json()["is_new_user"] is False
    assert r.get_json()["user_id"] == s.get_json()["user_id"]          # same account, not a duplicate
    # the password still works, and the account is still a password account
    lg = client.post("/v1/auth/login", json={"email": email, "password": "A-good-long-passphrase-123"}, headers=_headers())
    assert lg.status_code == 200
    p = client.get("/v1/profile", headers=_auth(r.get_json()["access_token"], h2)).get_json()
    assert p["auth_provider"] == "password"


def test_pre_registered_unverified_account_loses_its_password_and_sessions_when_the_real_owner_signs_in_with_google(client, fake_google):
    """The takeover this blocks: an attacker signs up with the victim's email and the attacker's own password, the
    victim later uses Google. Before the fix the attacker's password and live session kept working in the victim's account."""
    import time
    email = f"pytest.victim.{uuid.uuid4().hex[:10]}@example.com"
    attacker_pw = "Attackers-long-passphrase-123"
    ah = _headers()
    s = client.post("/v1/auth/signup", json={"email": email, "password": attacker_pw}, headers=ah)
    assert s.status_code == 201
    attacker_tok = _auth(s.get_json()["access_token"], ah)
    assert client.get("/v1/profile", headers=attacker_tok).status_code == 200
    time.sleep(1.1)                                                     # tokens use whole-second timestamps

    fake_google(email=email)                                           # the real owner, vouched for by Google
    r, vh = _post(client)
    assert r.status_code == 200 and r.get_json()["user_id"] == s.get_json()["user_id"]
    assert client.get("/v1/profile", headers=_auth(r.get_json()["access_token"], vh)).status_code == 200   # the owner is in

    assert client.get("/v1/profile", headers=attacker_tok).status_code in (401, 403)        # attacker's session ended
    lg = client.post("/v1/auth/login", json={"email": email, "password": attacker_pw}, headers=_headers())
    assert lg.status_code == 401                                        # attacker's password no longer works
    rf = client.post("/v1/auth/refresh", json={"refresh_token": s.get_json()["refresh_token"]}, headers=ah)
    assert rf.status_code in (401, 403)                                 # nor can the attacker refresh


def test_unverified_google_email_is_refused(client, fake_google):
    fake_google(verified=False)
    r, _ = _post(client)
    assert r.status_code == 422


def test_bad_google_token_is_rejected(client, fake_google):
    fake_google()
    r, _ = _post(client, credential="bad")
    assert r.status_code == 401


def test_feature_is_off_without_a_client_id(client):
    r, _ = _post(client)
    assert r.status_code == 404


def test_real_verifier_rejects_garbage_without_any_network(client):
    with pytest.raises(Unauthorized):
        google_mod.verify_google_id_token("not-a-real-token", "test-client-id")
    with pytest.raises(Unauthorized):
        google_mod.verify_google_id_token("", "test-client-id")


def test_google_account_deletes_with_typed_confirmation(client, fake_google):
    fake_google()
    r, h = _post(client)
    tok = _auth(r.get_json()["access_token"], h)
    bad = client.delete("/v1/account", json={"confirm": "nope"}, headers=tok)
    assert bad.status_code == 422
    ok = client.delete("/v1/account", json={"confirm": "delete"}, headers=tok)
    assert ok.status_code == 200 and ok.get_json()["status"] == "deletion_scheduled"
    again, _ = _post(client)              # the Google identity can now start a fresh account
    assert again.status_code in (200, 201)
