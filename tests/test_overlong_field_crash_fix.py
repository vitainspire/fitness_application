"""Regression tests for a found-live bug: email (signup) and X-Device-Id both
only had a minimum-length/non-empty check, not a maximum — User.email is
String(320) and the device-id columns are String(128), so a value longer
than that reached the INSERT and surfaced as an uncaught "value too long"
database error (500) instead of a clean 422. Fixed in app/auth/service.py
(signup) and app/auth/routes.py (_device_id)."""
import pytest

from app.platform.errors import ValidationError


def test_overlong_email_raises_clean_validation_error(client):
    overlong_email = "a" * 310 + "@example.com"  # > 320 chars total
    r = client.post(
        "/v1/auth/signup",
        json={"email": overlong_email, "password": "A-good-long-passphrase-123"},
        headers={"X-Device-Id": "pytest-device-overlong-email"},
    )
    assert r.status_code == 422, r.get_json()
    assert "too long" in r.get_json()["error"]["message"].lower()


def test_overlong_device_id_raises_clean_validation_error(client):
    overlong_device_id = "d" * 200  # > 128 chars
    r = client.post(
        "/v1/auth/signup",
        json={"email": "pytest.overlong.device@example.com", "password": "A-good-long-passphrase-123"},
        headers={"X-Device-Id": overlong_device_id},
    )
    assert r.status_code == 422, r.get_json()
    assert "too long" in r.get_json()["error"]["message"].lower()
