"""Module 1 business logic.

The pieces here that carry real weight:
  1.2   per-account lockout, because IP and device keys are attacker-controlled
  1.6   theft response performs all four actions, never a subset
  1.9   reset responses are identical whether or not the email exists
  1.10  the deletion record outlives the data it removed
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import tokens as tk
from app.auth.models import (
    DeletionRequest, DevicePushToken, EmailVerificationToken,
    PasswordResetToken, RefreshToken, User,
)
from app.auth.passwords import (
    DUMMY_PASSWORD_HASH, hash_password, validate_password, verify_password,
)
from app.platform import redis_clients as rc
from app.platform.config import Config
from app.platform.errors import Conflict, Forbidden, RateLimited, Unauthorized, ValidationError
from app.profile.models import OverrideConsent

# Deliberately vague and identical for both branches (1.9).
RESET_ACK = "If an account exists for that address, a reset link has been sent."


def _normalise_email(email: str) -> str:
    return email.strip().lower()


def _hash_user_id(user_id: str) -> str:
    return hashlib.sha256(user_id.encode()).hexdigest()


# --- Signup ----------------------------------------------------------------

def signup(session: Session, cfg: Config, email: str, password: str,
           device_id: str) -> tuple[User, tk.TokenPair, str]:
    email = _normalise_email(email)
    if "@" not in email or len(email) < 3:
        raise ValidationError("Enter a valid email address.")
    # Found live: only a MINIMUM length was checked — User.email is
    # String(320) (app/auth/models.py), so an email longer than that passed
    # this check, reached the INSERT, and the database's own "value too long
    # for type character varying(320)" error surfaced as an uncaught 500
    # instead of a clean 422. Same class of bug already fixed once for
    # client_entry_id (app/profile/routes.py) — applying the same proactive
    # length check here instead of letting the database reject it uncaught.
    if len(email) > 320:
        raise ValidationError("Email address is too long.")
    validate_password(
        password,
        min_length=cfg.password_min_length,
        breach_check=cfg.breach_check_enabled,
    )

    user = User(email=email, password_hash=hash_password(password))
    session.add(user)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        # The DB constraint is the real guard — two concurrent signups for the
        # same address both reach here and exactly one wins (1.1).
        raise Conflict("An account with that email already exists.") from exc

    verify_raw = _issue_email_verification(session, user.id)
    access, access_exp, _ = tk.issue_access_token(cfg, user.id)
    raw_refresh, refresh_row = tk.issue_refresh_token(session, cfg, user.id, device_id)
    pair = tk.TokenPair(access, raw_refresh, access_exp, refresh_row.expires_at)
    return user, pair, verify_raw


# --- Login -----------------------------------------------------------------

def _lockout_delay(cfg: Config, failures: int) -> int:
    """Exponential backoff, capped. Temporary and self-clearing — a permanent
    lock would be a denial of service against the real account owner (1.2)."""
    over = failures - cfg.login_lockout_threshold
    if over < 0:
        return 0
    return min(cfg.login_lockout_base_seconds * (2 ** over), 900)


def login(session: Session, cfg: Config, email: str, password: str,
          device_id: str) -> tuple[User, tk.TokenPair]:
    email = _normalise_email(email)
    user = session.scalar(select(User).where(User.email == email))

    # Always pay the Argon2 cost, even when no such user exists — see
    # DUMMY_PASSWORD_HASH's docstring for why this closes a real timing
    # side-channel (found live, code audit). Deliberately runs BEFORE the
    # lockout check below: found live in a second-pass audit that when this
    # ran after, a locked-out EXISTING account returned RateLimited
    # immediately (skipping verify_password entirely) while a nonexistent
    # account always paid the slow dummy-hash cost — an attacker who first
    # drove >=login_lockout_threshold failed attempts against a target email
    # could then distinguish "exists" from "doesn't exist" by timing alone,
    # reopening the exact enumeration oracle this fix exists to close.
    password_ok = verify_password(
        user.password_hash if user is not None else DUMMY_PASSWORD_HASH, password
    )

    # Per-account counter (1.2). Keyed on the account, because per-IP and
    # per-device limits are trivially bypassed by rotating both.
    #
    # Found live (third-pass code audit): this used to be `None` for a
    # nonexistent account, which skipped the lockout check/increment below
    # entirely — only a REAL account could ever accumulate failures and
    # eventually get a 429 "too many attempts." An attacker could tell
    # "exists" from "doesn't exist" purely by which status code repeated
    # attempts eventually returned, regardless of timing (the exact class of
    # oracle the dummy-hash fix above already closed for timing specifically).
    # Now every email gets a counter either way — a real account's by user_id
    # (stable across an email change), a nonexistent one's by the email
    # itself — so the lockout behavior is identical for both.
    fail_key = (
        rc.key_login_failures(user.id) if user
        else rc.key_login_failures_by_email(email)
    )
    failures = int(rc.durable().get(fail_key) or 0)
    delay = _lockout_delay(cfg, failures)
    if delay:
        # Report the REAL remaining lockout, not the formula's nominal delay.
        # The counter cannot advance past the threshold (this raise happens
        # before the increment below), so `_lockout_delay` returns a constant
        # base value — while the account actually stays locked until the
        # failure key's own TTL expires. Sending that constant as Retry-After
        # told a locked-out user "30 seconds" when the truth was up to an
        # hour, so they retry, fail, and learn nothing.
        ttl = rc.durable().ttl(fail_key)
        raise RateLimited(
            "Too many failed attempts. Please wait before trying again.",
            retry_after=ttl if isinstance(ttl, int) and ttl > 0 else delay,
        )

    if user is None or not user.is_active or not password_ok:
        # The expiry is deliberately refreshed on EVERY failure that reaches
        # here, and that is not the bug it looks like: the RateLimited raise
        # above returns before this block once the threshold is hit, so a
        # locked-out account can never renew its own window — the renewals
        # only ever happen on failures 1..threshold. The window therefore
        # ends one hour after the last sub-threshold failure, which is the
        # intended self-clearing behaviour.
        #
        # (An earlier pass changed this to set the TTL only once, on the
        # first failure, on the mistaken theory that a sustained attacker
        # could renew it forever. That made the lockout strictly weaker: an
        # attacker pacing one guess every ~12 minutes let the key expire
        # mid-sequence and never tripped the threshold at all. Reverted.)
        pipe = rc.durable().pipeline()
        pipe.incr(fail_key)
        pipe.expire(fail_key, timedelta(hours=1))
        pipe.execute()
        # Same message either way — do not reveal whether the address exists.
        raise Unauthorized("Email or password is incorrect.")

    rc.durable().delete(fail_key)  # resets on success (1.2)

    access, access_exp, _ = tk.issue_access_token(cfg, user.id)
    raw_refresh, refresh_row = tk.issue_refresh_token(session, cfg, user.id, device_id)
    return user, tk.TokenPair(access, raw_refresh, access_exp, refresh_row.expires_at)


# --- Refresh ---------------------------------------------------------------

def refresh(session: Session, cfg: Config, raw_refresh: str,
            device_id: str) -> tuple[User, tk.TokenPair]:
    try:
        pair, user = tk.rotate_refresh_token(session, cfg, raw_refresh, device_id)
    except tk.TheftDetected as theft:
        handle_theft(session, cfg, theft.user_id, theft.reason)
        raise Unauthorized(
            "Your session was ended for security reasons. Please sign in again."
        ) from theft
    return user, pair


def handle_theft(session: Session, cfg: Config, user_id: str, reason: str) -> None:
    """1.6 — all four actions, made durable before the caller's 401 unwinds.

    The requirement this has to satisfy: the refresh request that triggered
    detection must fail with 401, and that exception unwinds the caller's
    `session_scope`, which rolls back — so the revocations must not be left
    sitting unflushed in that session, or they are silently discarded at the
    moment they matter most, leaving the attacker holding a token that still
    rotates. The explicit `commit()` below is what guarantees that; a later
    rollback has nothing left to undo.

    Found live (code audit) — this used to open its OWN second session to get
    that durability, which self-deadlocked against its own caller:
    rotate_refresh_token holds `SELECT ... FOR UPDATE` on the presented row
    (tokens.py) for the life of the caller's transaction, and
    revoke_all_refresh_tokens' `UPDATE ... WHERE revoked_at IS NULL` targets
    that very row. The second session blocked on the first session's lock
    while the first session sat blocked in Python waiting for the second to
    return. Postgres saw only one waiter, so its deadlock detector never
    fired and nothing timed out — the request hung indefinitely, holding two
    pooled connections. With pool_size=5 + max_overflow=5, five such events
    (a changed X-Device-Id is enough to trigger one) would wedge every
    connection and take the API down until a restart.

    Using the caller's own session removes the second connection, and with it
    the lock conflict.
    """
    tk.revoke_all_refresh_tokens(session, user_id)                       # 1
    session.query(DevicePushToken).filter(                               # 3
        DevicePushToken.user_id == user_id
    ).delete(synchronize_session=False)
    # Durable now, before the Unauthorized above unwinds the request. This
    # also releases the FOR UPDATE lock held since rotate_refresh_token.
    session.commit()

    tk.set_tokens_valid_after(user_id, cfg)                              # 2 (Redis)

    # 4 — must not happen silently. Wired to the alerting channel (Module 10):
    # a durable AppIssue row, not just a console log line that vanishes.
    import logging
    logging.getLogger("security").critical(
        "refresh_token_theft_detected user_id=%s reason=%s", user_id, reason
    )
    try:
        from flask import g
        from app.platform.db import session_scope
        from app.platform.models import record_issue
        with session_scope() as issue_session:
            record_issue(
                issue_session, issue_type="session_theft",
                message=f"Refresh token theft detected: {reason}",
                user_id=user_id, request_id=getattr(g, "request_id", None),
            )
    except Exception:
        logging.getLogger("security").exception(
            "Failed to record theft detection as an issue (user_id=%s)", user_id
        )


# --- Logout ----------------------------------------------------------------

def logout(session: Session, claims: dict, raw_refresh: str | None) -> None:
    tk.denylist_access_token(claims)                         # 1.5
    if raw_refresh:
        row = session.scalar(
            select(RefreshToken).where(
                RefreshToken.token_hash == tk.hash_token(raw_refresh)
            )
        )
        # Found live (third-pass code audit): this revoked by hash alone,
        # with no check that the token actually belongs to the caller —
        # anyone who obtained another user's raw refresh token could use it
        # to force-revoke that user's session. Same as a not-found token,
        # not an error: this call is "sign out this device," and a refresh
        # token that isn't this user's own was never a session of theirs to
        # begin with.
        if row is not None and row.user_id == claims["sub"] and row.revoked_at is None:
            row.revoked_at = tk.now_utc()


# --- Email verification ----------------------------------------------------

def _issue_email_verification(session: Session, user_id: str) -> str:
    raw = secrets.token_urlsafe(32)
    session.add(EmailVerificationToken(
        user_id=user_id,
        token_hash=tk.hash_token(raw),
        expires_at=tk.now_utc() + timedelta(hours=24),
    ))
    return raw


def verify_email(session: Session, raw_token: str) -> None:
    row = session.scalar(
        select(EmailVerificationToken).where(
            EmailVerificationToken.token_hash == tk.hash_token(raw_token)
        )
    )
    now = tk.now_utc()
    if row is None or row.used_at is not None or row.expires_at <= now:
        raise ValidationError("This verification link is invalid or has expired.")
    row.used_at = now
    user = session.get(User, row.user_id)
    if user is not None:
        user.email_verified_at = now


# --- Password reset --------------------------------------------------------

def request_password_reset(session: Session, email: str) -> str:
    """Returns the raw token for the mailer. The HTTP layer must return the same
    acknowledgement whether or not an account exists (1.9)."""
    user = session.scalar(select(User).where(User.email == _normalise_email(email)))
    if user is None or not user.is_active:
        return ""  # caller still returns RESET_ACK
    raw = secrets.token_urlsafe(32)
    session.add(PasswordResetToken(
        user_id=user.id,
        token_hash=tk.hash_token(raw),
        expires_at=tk.now_utc() + timedelta(hours=1),
    ))
    return raw


def confirm_password_reset(session: Session, cfg: Config,
                           raw_token: str, new_password: str) -> None:
    row = session.scalar(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == tk.hash_token(raw_token)
        )
    )
    now = tk.now_utc()
    if row is None or row.used_at is not None or row.expires_at <= now:
        raise ValidationError("This reset link is invalid or has expired.")

    validate_password(
        new_password,
        min_length=cfg.password_min_length,
        breach_check=cfg.breach_check_enabled,
    )

    user = session.get(User, row.user_id)
    if user is None or not user.is_active:
        raise ValidationError("This reset link is invalid or has expired.")

    user.password_hash = hash_password(new_password)
    row.used_at = now

    # Someone resetting because they suspect compromise must not leave the
    # attacker's session alive (1.9).
    tk.revoke_all_refresh_tokens(session, user.id)
    tk.set_tokens_valid_after(user.id, cfg)
    rc.durable().delete(rc.key_login_failures(user.id))


def change_password(session: Session, cfg: Config, user_id: str, *,
                    current_password: str, new_password: str) -> None:
    """Logged-in password change from Settings. Requires the current password
    rather than an emailed token — the user is already proven to hold a valid
    session, so this is the direct equivalent of confirm_password_reset above,
    minus the token.
    """
    user = session.get(User, user_id)
    if user is None or not user.is_active:
        raise ValidationError("Account not found.")
    if not verify_password(user.password_hash, current_password):
        raise ValidationError("Current password is incorrect.")

    validate_password(
        new_password,
        min_length=cfg.password_min_length,
        breach_check=cfg.breach_check_enabled,
    )

    user.password_hash = hash_password(new_password)

    # Same treatment as a reset (1.9): changing a password is exactly the
    # moment to kill any other live session, in case the old password had
    # already leaked. This device's refresh token is revoked too — signing
    # in again with the new password is the expected next step.
    tk.revoke_all_refresh_tokens(session, user.id)
    tk.set_tokens_valid_after(user.id, cfg)
    rc.durable().delete(rc.key_login_failures(user.id))


# --- Account deletion ------------------------------------------------------

def _purge_user_data(session: Session, user_id: str) -> None:
    """Permanently deletes all health data, logs, chat history, programs, and
    tokens for the user across all modules (GDPR / Right to be Forgotten)."""
    # 1. Managed relationships and pending plans
    from app.relationships.models import (
        ManagedRelationship, PendingCustomExercise, PendingManagedEntry,
    )
    rel_ids = session.scalars(
        select(ManagedRelationship.id).where(
            or_(ManagedRelationship.manager_user_id == user_id,
                ManagedRelationship.managed_user_id == user_id)
        )
    ).all()
    if rel_ids:
        session.execute(
            delete(PendingManagedEntry).where(PendingManagedEntry.relationship_id.in_(rel_ids))
        )
        session.execute(
            delete(PendingCustomExercise).where(PendingCustomExercise.relationship_id.in_(rel_ids))
        )
        session.execute(
            delete(ManagedRelationship).where(ManagedRelationship.id.in_(rel_ids))
        )

    # 2. RAG health assistant conversations, memories, and quotas
    from app.rag.models import RehabChatLog, RehabMemory, RehabQuota
    session.execute(delete(RehabChatLog).where(RehabChatLog.user_id == user_id))
    session.execute(delete(RehabMemory).where(RehabMemory.user_id == user_id))
    session.execute(delete(RehabQuota).where(RehabQuota.user_id == user_id))

    # 3. Progression history
    from app.progression.models import ProgressionHistory
    session.execute(delete(ProgressionHistory).where(ProgressionHistory.user_id == user_id))

    # 4. Profile, conditions, exercise programs, activity logs, personal bests, and workout sessions
    from app.profile.models import (
        ActivityLog, CustomExercise, PersonalBest, SorenessReport,
        UserCondition, UserProfile, UserProgram, WaterIntakeLog, WorkoutSession,
    )
    session.execute(delete(ActivityLog).where(ActivityLog.user_id == user_id))
    session.execute(delete(WorkoutSession).where(WorkoutSession.user_id == user_id))
    session.execute(delete(SorenessReport).where(SorenessReport.user_id == user_id))
    session.execute(delete(WaterIntakeLog).where(WaterIntakeLog.user_id == user_id))
    session.execute(delete(PersonalBest).where(PersonalBest.user_id == user_id))
    session.execute(delete(UserProgram).where(UserProgram.user_id == user_id))
    session.execute(delete(CustomExercise).where(CustomExercise.user_id == user_id))
    session.execute(delete(UserCondition).where(UserCondition.user_id == user_id))
    session.execute(delete(UserProfile).where(UserProfile.user_id == user_id))

    # 5. Auth tokens
    session.execute(delete(RefreshToken).where(RefreshToken.user_id == user_id))
    session.execute(delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id))
    session.execute(delete(EmailVerificationToken).where(EmailVerificationToken.user_id == user_id))
    session.execute(delete(DevicePushToken).where(DevicePushToken.user_id == user_id))


def delete_account(session: Session, cfg: Config, user_id: str, *, password: str,
                   pitr_days: int = 35) -> None:
    """1.10 — permanently purge user health data and release the email address.

    Found live (third-pass code audit): the single most destructive action in
    this whole module used to require only a valid session (@require_auth) —
    change_password already re-checks the current password before proceeding;
    this did not. Now mirrors that same re-authentication step, so a
    hijacked/leaked access token alone isn't enough to permanently delete the
    account — the caller must prove they still know the password too.

    Explicit managed-account block (MANAGED_PLANS_TEACHER_DOCTOR_PARENT_PLAN.md):
    confirmed directly — only whoever created the link should be able to
    delete it, never the managed person themselves. In practice the password
    check above already stops this today (a managed account's password is a
    random, never-revealed secret — see open_invite), but that's an
    accident of implementation, not a guarantee; this makes the real rule
    explicit so it keeps holding even if a managed account's password can
    ever legitimately be set later (e.g. a future "claim this account for
    real" reset flow).
    """
    from app.relationships.service import is_managed_account  # local: avoids
    # a module-level import cycle risk — app.relationships.service imports
    # app.auth.tokens/.models/.passwords (not .service), so this direction is
    # safe, but keeping it local matches this codebase's existing convention
    # for every other cross-module import of this shape.

    user = session.get(User, user_id)
    if user is None or not user.is_active:
        return
    if is_managed_account(session, user_id):
        raise Forbidden(
            "This account is managed by someone else — only they can delete it."
        )
    if not verify_password(user.password_hash, password):
        raise ValidationError("Password is incorrect.")

    now = tk.now_utc()
    user.deleted_at = now
    # Free the unique email constraint so the user can re-register anytime with their original email
    user.email = f"deleted_{user.id}_{int(now.timestamp())}@deleted.invalid"

    tk.revoke_all_refresh_tokens(session, user_id)
    tk.set_tokens_valid_after(user_id, cfg)

    # 14.7 — keep the override-consent evidence, strip the identity attached
    # to it. Proof the warning system worked; no health data tied to a person
    # once they're gone.
    session.query(OverrideConsent).filter(
        OverrideConsent.user_id == user_id
    ).update({OverrideConsent.user_id: None}, synchronize_session=False)

    # Permanently delete all health conditions, conversation logs, workout sessions,
    # activity logs, personal bests, programs, and tokens right now.
    _purge_user_data(session, user_id)

    session.add(DeletionRequest(
        user_id_hash=_hash_user_id(user_id),
        completed_at=now,
        purge_after=now,
    ))
