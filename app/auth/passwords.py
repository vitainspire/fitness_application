"""Password hashing and policy (1.1).

Policy: minimum length, reject known-breached passwords. Length beats composition
rules — no mandated symbol classes.
"""
from __future__ import annotations

import hashlib

import requests
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.platform.errors import ValidationError

_hasher = PasswordHasher()

_PWNED_RANGE_URL = "https://api.pwnedpasswords.com/range/{prefix}"

# Found live (pre-launch production readiness check): nothing capped how long
# a submitted password could be, on either the set-a-new-password path
# (validate_password) or — more importantly — the VERIFY path (login,
# change-password's current_password check), which every unauthenticated
# request to /v1/auth/login reaches. Argon2's hashing/verification cost
# scales with input length, so an attacker could send an enormous password
# string to cheaply burn real CPU time per request — an unauthenticated
# resource-exhaustion angle distinct from (and in addition to) the request-
# body-size cap in app/factory.py's MAX_CONTENT_LENGTH. 256 is far more than
# any real password needs while stopping a multi-KB/MB string from ever
# reaching Argon2 at all.
MAX_PASSWORD_LENGTH = 256

# Found live (code audit): login() used to skip verify_password entirely when
# no matching account exists, since `user is None or ...` short-circuits —
# meaning a nonexistent email returned in ~0ms while a real one always paid
# the ~50-200ms Argon2 cost. The response BODY was identical either way, but
# the reliable latency difference is a practical account-enumeration oracle,
# exactly what the identical error message was meant to prevent. login()
# verifies against this fixed dummy hash when no such user exists, so the
# Argon2 cost is always paid regardless of which branch it's really in.
DUMMY_PASSWORD_HASH = _hasher.hash("not-a-real-password-timing-safety-constant")


def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(stored_hash: str, plain: str) -> bool:
    if len(plain) > MAX_PASSWORD_LENGTH:
        # Cheap, fast rejection before Argon2 ever sees it — see
        # MAX_PASSWORD_LENGTH's own comment above.
        return False
    try:
        return _hasher.verify(stored_hash, plain)
    except (VerificationError, InvalidHashError):
        # VerificationError is the base class (VerifyMismatchError is one
        # subclass of it) — found live, code audit: the narrower except only
        # caught VerifyMismatchError, so any other verification failure (e.g.
        # a corrupted/unexpectedly-encoded stored hash not covered by
        # InvalidHashError either) raised uncaught, turning a clean "wrong
        # password" into an unhandled 500 on a security-critical path.
        return False


def is_breached(plain: str, *, timeout: float = 2.0) -> bool:
    """k-anonymity breach check: only the first 5 hex characters of the SHA-1
    hash leave this process, so the password itself is never transmitted.

    Fails OPEN on any network problem. A breach lookup outage must not stop
    people signing up — the length floor still applies.
    """
    # SHA-1 here is not a security choice — it's the fixed algorithm the
    # HaveIBeenPwned k-anonymity API itself requires for the range query.
    # usedforsecurity=False documents that (and quiets a security scanner
    # that would otherwise correctly flag bare SHA-1 as weak for anything
    # actually protecting data, which this isn't).
    digest = hashlib.sha1(plain.encode(), usedforsecurity=False).hexdigest().upper()
    prefix, suffix = digest[:5], digest[5:]
    try:
        resp = requests.get(_PWNED_RANGE_URL.format(prefix=prefix), timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException:
        return False
    return any(line.split(":")[0] == suffix for line in resp.text.splitlines())


def check_password_complexity(plain: str, min_length: int = 10) -> dict[str, bool]:
    """Inspects password composition and returns compliance breakdown."""
    return {
        "min_length": len(plain) >= min_length,
        "has_upper": any(c.isupper() for c in plain),
        "has_lower": any(c.islower() for c in plain),
        "has_digit": any(c.isdigit() for c in plain),
        "has_special": any(not c.isalnum() for c in plain),
        "not_overlong": len(plain) <= MAX_PASSWORD_LENGTH,
    }


def validate_password(
    plain: str,
    *,
    min_length: int = 10,
    breach_check: bool = True,
    enforce_complexity: bool = True,
) -> None:
    if len(plain) > MAX_PASSWORD_LENGTH:
        raise ValidationError(
            f"Password must be no more than {MAX_PASSWORD_LENGTH} characters."
        )
    if len(plain) < min_length:
        raise ValidationError(
            f"Password must be at least {min_length} characters."
        )
    if enforce_complexity:
        if not any(c.isupper() for c in plain):
            raise ValidationError(
                "Password must contain at least one uppercase letter (A-Z)."
            )
        if not any(c.islower() for c in plain):
            raise ValidationError(
                "Password must contain at least one lowercase letter (a-z)."
            )
        if not any(c.isdigit() for c in plain):
            raise ValidationError(
                "Password must contain at least one number (0-9)."
            )
        if not any(not c.isalnum() for c in plain):
            raise ValidationError(
                "Password must contain at least one special character (!@#$%^&*...)."
            )
    if breach_check and is_breached(plain):
        raise ValidationError(
            "This password has appeared in a known data breach. Please choose another."
        )
