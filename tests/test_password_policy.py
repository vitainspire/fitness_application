"""Tests for password complexity policy and password reset flow.

Requirements:
- Password must contain at least 1 capital (uppercase) letter
- Password must contain at least 1 small (lowercase) letter
- Password must contain at least 1 number
- Password must contain at least 1 special character
- Password reset email sent from ayush.kumar@vitainspire.com with a reset link
- Visiting the reset link allows adding a new password and confirming it
"""
import uuid
import pytest

from app.auth.passwords import check_password_complexity, validate_password
from app.auth import service
from app.auth.models import User
from app.platform.config import load_config
from app.platform.db import session_scope
from app.platform.errors import ValidationError
from app.platform.mailer import build_password_reset_html, send_email


def test_password_complexity_breakdown():
    # Missing uppercase
    assert not check_password_complexity("password123!")["has_upper"]
    # Missing lowercase
    assert not check_password_complexity("PASSWORD123!")["has_lower"]
    # Missing number
    assert not check_password_complexity("Password!!!")["has_digit"]
    # Missing special character
    assert not check_password_complexity("Password123")["has_special"]
    # Valid password
    valid = check_password_complexity("VitaInspire@2026")
    assert valid["has_upper"]
    assert valid["has_lower"]
    assert valid["has_digit"]
    assert valid["has_special"]
    assert valid["min_length"]


def test_validate_password_requires_uppercase():
    with pytest.raises(ValidationError, match="uppercase"):
        validate_password("lowercase123!", min_length=8, breach_check=False)


def test_validate_password_requires_lowercase():
    with pytest.raises(ValidationError, match="lowercase"):
        validate_password("UPPERCASE123!", min_length=8, breach_check=False)


def test_validate_password_requires_number():
    with pytest.raises(ValidationError, match="number"):
        validate_password("PasswordSpecial!", min_length=8, breach_check=False)


def test_validate_password_requires_special_character():
    with pytest.raises(ValidationError, match="special character"):
        validate_password("Password12345", min_length=8, breach_check=False)


def test_validate_password_succeeds_with_all_criteria():
    validate_password("VitaInspire@2026", min_length=8, breach_check=False)


def test_build_password_reset_html_contains_sender_and_link():
    reset_url = "http://localhost:5173/?reset_token=abc-123#reset-password"
    token = "abc-123"
    html = build_password_reset_html(
        reset_url=reset_url,
        token=token,
        sender_email="ayush.kumar@vitainspire.com",
        min_length=10,
    )
    assert "ayush.kumar@vitainspire.com" in html
    assert reset_url in html
    assert token in html
    assert "1 capital" in html or "uppercase" in html
    assert "1 small" in html or "lowercase" in html
    assert "1 number" in html
    assert "1 special character" in html


def test_password_reset_request_and_confirm_flow(client):
    cfg = load_config()
    unique = uuid.uuid4().hex[:8]
    email = f"reset.flow.{unique}@example.com"
    initial_pw = "InitialPass#2026"
    new_pw = "BrandNewPass@2027"

    # 1. Sign up user
    headers = {"X-Device-Id": f"device-{unique}"}
    r = client.post(
        "/v1/auth/signup",
        json={"email": email, "password": initial_pw},
        headers=headers,
    )
    assert r.status_code == 201

    # 2. Request password reset
    r = client.post(
        "/v1/auth/password-reset/request",
        json={"email": email},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.get_json()["status"] == "ok"

    # 3. Retrieve raw token from DB
    with session_scope() as s:
        raw_token = service.request_password_reset(s, email)
    assert raw_token

    # 4. Attempt confirm with weak password (missing special char) -> 422
    r = client.post(
        "/v1/auth/password-reset/confirm",
        json={"token": raw_token, "password": "NoSpecialChar123"},
        headers=headers,
    )
    assert r.status_code == 422
    assert "special character" in r.get_json()["error"]["message"].lower()

    # 5. Confirm with compliant password -> 200
    r = client.post(
        "/v1/auth/password-reset/confirm",
        json={"token": raw_token, "password": new_pw},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.get_json()["status"] == "password_updated"

    # 6. Verify login with old password fails
    r = client.post(
        "/v1/auth/login",
        json={"email": email, "password": initial_pw},
        headers=headers,
    )
    assert r.status_code == 401

    # 7. Verify login with new password succeeds
    r = client.post(
        "/v1/auth/login",
        json={"email": email, "password": new_pw},
        headers=headers,
    )
    assert r.status_code == 200
    assert "access_token" in r.get_json()
