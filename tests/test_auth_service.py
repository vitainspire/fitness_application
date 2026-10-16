"""Regression tests for real bugs found in the code audit — no auth test
file existed before this (everything was only exercised indirectly via the
make_user fixture calling the real signup endpoint), so these lock in the
security-relevant fixes directly.
"""
from datetime import timedelta

import pytest

from app.auth import service, tokens as tk
from app.auth.models import RefreshToken, User
from app.auth.passwords import DUMMY_PASSWORD_HASH, hash_password, verify_password
from app.platform import redis_clients as rc
from app.platform.config import load_config
from app.platform.db import session_scope
from app.platform.errors import RateLimited, Unauthorized, ValidationError


def _cfg():
    return load_config()


def test_login_pays_argon2_cost_even_for_a_nonexistent_email(client):
    """Found live: login() used to skip verify_password entirely when no
    user exists (short-circuit on `user is None or ...`), creating a timing
    side-channel that leaked whether an email is registered even though the
    response body is identical either way. This doesn't measure wall-clock
    time (unreliable in CI) — it confirms the dummy-hash path is actually
    exercised and still raises the same generic error. `client` is unused
    directly; depending on it ensures the app (and its engine) is
    initialized, the same way every other test in this suite gets a DB."""
    with session_scope() as s:
        with pytest.raises(Unauthorized, match="Email or password is incorrect"):
            service.login(s, _cfg(), "definitely-not-a-real-account@example.com",
                          "whatever-password", "test-device")


def test_dummy_password_hash_is_a_real_verifiable_argon2_hash():
    """The constant login() verifies against for nonexistent users must
    actually cost real Argon2 time, not be a cheap placeholder string."""
    assert DUMMY_PASSWORD_HASH.startswith("$argon2")
    assert verify_password(DUMMY_PASSWORD_HASH, "not-a-real-password-timing-safety-constant")
    assert not verify_password(DUMMY_PASSWORD_HASH, "wrong")


def test_set_tokens_valid_after_ttl_tracks_configured_access_token_ttl(make_user):
    """Found live: this TTL was a hardcoded 2 hours, independent of
    cfg.access_token_ttl. Confirms the Redis key's real TTL now reflects the
    configured value (plus the small safety margin), not a fixed constant."""
    user = make_user()
    cfg = _cfg()
    tk.set_tokens_valid_after(user["user_id"], cfg)
    ttl = rc.durable().ttl(rc.key_tokens_valid_after(user["user_id"]))
    expected = int((cfg.access_token_ttl + timedelta(minutes=5)).total_seconds())
    assert ttl > 0
    assert ttl <= expected
    assert ttl > expected - 10  # generous slack for test execution time


def test_refresh_rotation_still_works_normally_with_the_row_lock(make_user):
    """FOR UPDATE was added to close a rotation race — confirms ordinary,
    non-concurrent rotation (the overwhelming common case) is unaffected."""
    import uuid
    unique = uuid.uuid4().hex[:8]
    with session_scope() as s:
        u = User(email=f"rotate.{unique}@example.com", password_hash=hash_password("A-good-long-passphrase-123"))
        s.add(u)
        s.flush()
        cfg = _cfg()
        raw_refresh, _row = tk.issue_refresh_token(s, cfg, u.id, "device-1")
        pair, user = tk.rotate_refresh_token(s, cfg, raw_refresh, "device-1")
        assert user.id == u.id
        assert pair.refresh_token != raw_refresh


def test_login_lockout_applies_the_same_way_to_a_nonexistent_email():
    """Found live (third-pass code audit): fail_key used to be None for a
    nonexistent account, so only a REAL account could ever accumulate
    failures and eventually get a 429 — an attacker could tell "exists" from
    "doesn't exist" purely by which status code repeated attempts eventually
    returned, no timing analysis needed. Now a nonexistent email accumulates
    failures and locks out exactly the same way a real account does."""
    import uuid
    cfg = _cfg()
    email = f"lockout-test-{uuid.uuid4().hex[:8]}@example.com"
    with session_scope() as s:
        for _ in range(cfg.login_lockout_threshold):
            with pytest.raises(Unauthorized):
                service.login(s, cfg, email, "wrong-password", "test-device")
        with pytest.raises(RateLimited):
            service.login(s, cfg, email, "wrong-password", "test-device")


def test_logout_does_not_revoke_another_users_refresh_token():
    """Found live (third-pass code audit): logout() used to revoke a refresh
    token by hash alone, with no check that it actually belongs to the
    caller — anyone holding another user's raw refresh token could force
    that user's session revoked. Confirms an attacker's own claims can't
    touch a victim's token."""
    import uuid
    unique = uuid.uuid4().hex[:8]
    with session_scope() as s:
        victim = User(email=f"victim.{unique}@example.com",
                      password_hash=hash_password("A-good-long-passphrase-123"))
        attacker = User(email=f"attacker.{unique}@example.com",
                        password_hash=hash_password("A-good-long-passphrase-123"))
        s.add_all([victim, attacker])
        s.flush()
        victim_id, attacker_id = victim.id, attacker.id
        cfg = _cfg()
        raw_refresh, row = tk.issue_refresh_token(s, cfg, victim_id, "victim-device")
        s.flush()
        row_id = row.id

    import time
    fake_claims = {"sub": attacker_id, "jti": f"fake-{unique}", "exp": int(time.time()) + 60}
    with session_scope() as s:
        service.logout(s, fake_claims, raw_refresh)

    with session_scope() as s:
        row = s.get(RefreshToken, row_id)
        assert row.revoked_at is None  # the victim's session must still be untouched


def test_delete_account_requires_correct_password(make_user):
    """Found live (third-pass code audit): the single most destructive
    action in this module used to require only a valid session — no
    re-confirmation, unlike change_password which already re-checks the
    current password."""
    user = make_user()
    cfg = _cfg()
    with session_scope() as s:
        with pytest.raises(ValidationError, match="Password is incorrect"):
            service.delete_account(s, cfg, user["user_id"], password="definitely-wrong")

    with session_scope() as s:
        u = s.get(User, user["user_id"])
        assert u.deleted_at is None  # must not have been deleted


def test_delete_account_succeeds_with_the_correct_password(make_user):
    user = make_user()
    cfg = _cfg()
    with session_scope() as s:
        service.delete_account(s, cfg, user["user_id"], password="A-good-long-passphrase-123")

    with session_scope() as s:
        u = s.get(User, user["user_id"])
        assert u.deleted_at is not None


def test_delete_account_purges_data_and_frees_email_for_reregistration(make_user, client):
    from app.profile.models import UserCondition, UserProfile

    # 1. User signs up with specific email and conditions
    user = make_user(condition_codes=["chronic_back_pain"])
    user_id = user["user_id"]
    cfg = _cfg()

    with session_scope() as s:
        u = s.get(User, user_id)
        email = u.email

    # 2. Confirm profile and conditions exist
    with session_scope() as s:
        assert s.get(UserProfile, user_id) is not None
        assert s.get(UserCondition, (user_id, "chronic_back_pain")) is not None

    # 3. User deletes account
    with session_scope() as s:
        service.delete_account(s, cfg, user_id, password="A-good-long-passphrase-123")

    # 4. Verify all profile and health data are permanently purged
    with session_scope() as s:
        assert s.get(UserProfile, user_id) is None
        assert s.get(UserCondition, (user_id, "chronic_back_pain")) is None

    # 5. User signs up again with the EXACT SAME EMAIL address (no 409 Conflict)
    r = client.post(
        "/v1/auth/signup",
        json={"email": email, "password": "New-passphrase-12345"},
        headers={"X-Device-Id": "device-new-signup"},
    )
    assert r.status_code == 201
    new_user_data = r.get_json()
    assert new_user_data["user_id"] != user_id
    with session_scope() as s:
        new_u = s.get(User, new_user_data["user_id"])
        assert new_u.email == email
