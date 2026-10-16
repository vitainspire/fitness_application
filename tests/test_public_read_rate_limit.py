"""Regression test for a found-live bug: /v1/conditions and
/v1/taxonomy/activities are public (no auth) and had NO rate limit at all —
completely unthrottled. Fixed with enforce_public_read_rate_limits()
(app/gateway/middleware.py), deliberately using its OWN Redis key namespace
("ref:") rather than reusing enforce_preauth_rate_limits()'s keys — sharing
those would let a few onboarding-screen loads of this harmless reference data
eat into the same budget that protects signup/login, a self-inflicted
lockout. This test also checks that cross-contamination didn't happen."""
import dataclasses
import uuid

from app.platform.db import session_scope


def test_conditions_endpoint_is_rate_limited(app, client, monkeypatch):
    cfg = app.config["APP_CONFIG"]
    monkeypatch.setitem(
        app.config, "APP_CONFIG",
        dataclasses.replace(cfg, rl_device_per_hour=2, rl_ip_per_hour=200),
    )
    headers = {"X-Device-Id": f"pytest-ratelimit-{uuid.uuid4().hex[:8]}"}
    assert client.get("/v1/conditions", headers=headers).status_code == 200
    assert client.get("/v1/conditions", headers=headers).status_code == 200
    r3 = client.get("/v1/conditions", headers=headers)
    assert r3.status_code == 429, r3.get_json()


def test_taxonomy_activities_endpoint_is_rate_limited(app, client, monkeypatch):
    cfg = app.config["APP_CONFIG"]
    monkeypatch.setitem(
        app.config, "APP_CONFIG",
        dataclasses.replace(cfg, rl_device_per_hour=2, rl_ip_per_hour=200),
    )
    headers = {"X-Device-Id": f"pytest-ratelimit-{uuid.uuid4().hex[:8]}"}
    assert client.get("/v1/taxonomy/activities", headers=headers).status_code == 200
    assert client.get("/v1/taxonomy/activities", headers=headers).status_code == 200
    r3 = client.get("/v1/taxonomy/activities", headers=headers)
    assert r3.status_code == 429, r3.get_json()


def test_public_read_rate_limit_does_not_share_bucket_with_signup(app, client, monkeypatch):
    """The whole point of the separate "ref:" key namespace: exhausting the
    conditions/taxonomy budget for a device must not touch its ability to
    sign up — a shared bucket would be a self-inflicted lockout bug."""
    cfg = app.config["APP_CONFIG"]
    monkeypatch.setitem(
        app.config, "APP_CONFIG",
        dataclasses.replace(cfg, rl_device_per_hour=2, rl_ip_per_hour=200),
    )
    device = f"pytest-ratelimit-{uuid.uuid4().hex[:8]}"
    headers = {"X-Device-Id": device}
    client.get("/v1/conditions", headers=headers)
    client.get("/v1/conditions", headers=headers)
    exhausted = client.get("/v1/conditions", headers=headers)
    assert exhausted.status_code == 429

    r = client.post(
        "/v1/auth/signup",
        json={"email": f"pytest.ratelimit.{uuid.uuid4().hex[:8]}@example.com",
              "password": "A-good-long-passphrase-123"},
        headers=headers,
    )
    assert r.status_code == 201, r.get_json()
