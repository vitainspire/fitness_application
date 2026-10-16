"""Regression tests for the third-pass code audit's M6 finding: several auth
routes crashed with an uncaught 500 when a JSON field was explicitly `null`
rather than merely absent — `d.get(key, default)` only applies the default
when the key is ABSENT, not when it's present with a null value. Every case
here previously raised an uncaught AttributeError deep in a route or service
function; all must now return a clean 401/422, never a 500.
"""
PASSWORD = "A-good-long-passphrase-123"


def test_login_with_null_email_returns_401_not_500(client):
    r = client.post(
        "/v1/auth/login", json={"email": None, "password": "whatever"},
        headers={"X-Device-Id": "d-null-email"},
    )
    assert r.status_code == 401


def test_login_with_null_password_returns_401_not_500(client):
    r = client.post(
        "/v1/auth/login", json={"email": "someone@example.com", "password": None},
        headers={"X-Device-Id": "d-null-pw"},
    )
    assert r.status_code == 401


def test_signup_with_null_password_returns_422_not_500(client):
    r = client.post(
        "/v1/auth/signup", json={"email": "nullpw@example.com", "password": None},
        headers={"X-Device-Id": "d-signup-null"},
    )
    assert r.status_code == 422


def test_signup_with_non_string_email_returns_422_not_500(client):
    r = client.post(
        "/v1/auth/signup", json={"email": 12345, "password": PASSWORD},
        headers={"X-Device-Id": "d-signup-nonstr"},
    )
    assert r.status_code == 422


def test_password_reset_request_with_null_email_returns_ok_not_500(client):
    """Deliberately always 200/RESET_ACK either way (1.9 enumeration
    protection) — the bug was a 500 crash, not a wrong status code."""
    r = client.post("/v1/auth/password-reset/request", json={"email": None})
    assert r.status_code == 200


def test_password_reset_confirm_with_null_token_returns_422_not_500(client):
    r = client.post(
        "/v1/auth/password-reset/confirm", json={"token": None, "password": PASSWORD},
    )
    assert r.status_code == 422


def test_password_reset_confirm_with_null_password_returns_422_not_500(client):
    r = client.post(
        "/v1/auth/password-reset/confirm", json={"token": "some-token", "password": None},
    )
    assert r.status_code == 422


def test_change_password_with_null_current_password_returns_422_not_500(make_user, client):
    user = make_user()
    r = client.post(
        "/v1/auth/change-password",
        json={"current_password": None, "new_password": "A-new-good-passphrase-456"},
        headers=user["headers"],
    )
    assert r.status_code == 422


def test_change_password_with_null_new_password_returns_422_not_500(make_user, client):
    user = make_user()
    r = client.post(
        "/v1/auth/change-password",
        json={"current_password": PASSWORD, "new_password": None},
        headers=user["headers"],
    )
    assert r.status_code == 422


def test_logout_with_non_string_refresh_token_returns_422_not_500(make_user, client):
    user = make_user()
    r = client.post(
        "/v1/auth/logout", json={"refresh_token": 12345}, headers=user["headers"],
    )
    assert r.status_code == 422


def test_logout_with_null_refresh_token_signs_out_cleanly(make_user, client):
    """None is the correct value for 'no refresh token supplied' — must not
    be rejected, unlike a genuinely wrong type."""
    user = make_user()
    r = client.post(
        "/v1/auth/logout", json={"refresh_token": None}, headers=user["headers"],
    )
    assert r.status_code == 200


def test_delete_account_with_null_password_returns_422_not_500(make_user, client):
    user = make_user()
    r = client.delete("/v1/account", json={"password": None}, headers=user["headers"])
    assert r.status_code == 422


def test_delete_account_with_wrong_password_returns_422_and_does_not_delete(make_user, client):
    user = make_user()
    r = client.delete("/v1/account", json={"password": "wrong-password"}, headers=user["headers"])
    assert r.status_code == 422
    profile = client.get("/v1/profile", headers=user["headers"])
    assert profile.status_code == 200  # account must still exist and be usable


def test_delete_account_with_correct_password_succeeds(make_user, client):
    user = make_user()
    r = client.delete("/v1/account", json={"password": PASSWORD}, headers=user["headers"])
    assert r.status_code == 200


def test_oversized_password_on_signup_returns_422_not_a_slow_argon2_hash(client):
    """Found live (pre-launch production readiness check): nothing capped
    password length — an unauthenticated caller could send an enormous
    password string to force expensive Argon2 work per request."""
    import uuid
    r = client.post(
        "/v1/auth/signup",
        json={"email": f"oversize-{uuid.uuid4().hex[:8]}@example.com", "password": "a" * 500},
        headers={"X-Device-Id": "d-oversize-signup"},
    )
    assert r.status_code == 422


def test_oversized_password_on_login_returns_401_without_hitting_argon2(client):
    r = client.post(
        "/v1/auth/login", json={"email": "someone@example.com", "password": "a" * 500},
        headers={"X-Device-Id": "d-oversize-login"},
    )
    assert r.status_code == 401


def test_oversized_request_body_returns_413(client):
    """Found live (pre-launch production readiness check): no
    MAX_CONTENT_LENGTH meant Flask would buffer a request body of any size
    before a route handler (or even JSON parsing) ever ran."""
    import json
    huge_body = json.dumps({"email": "x@example.com", "password": "a" * (2 * 1024 * 1024)})
    r = client.post(
        "/v1/auth/login", data=huge_body, content_type="application/json",
        headers={"X-Device-Id": "d-oversize-body"},
    )
    assert r.status_code == 413
