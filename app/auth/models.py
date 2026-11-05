"""Module 1 tables — authentication and session state.

Every field here traces to a numbered requirement. The comments name it, because
several of these columns exist for reasons that are not obvious from the name.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.platform.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)

    # 1.1 — the UNIQUE constraint is what prevents duplicate accounts under
    # concurrent signup. Application-level checks race; this does not.
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # 1.10 — soft delete, then hard purge after the documented grace window.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    # 1.7 — separate identity/role from end users. Deliberately just a flag,
    # not a separate table: the handoff calls for "a separate table/flag",
    # and a flag is the simplest thing that satisfies "staff vs not staff"
    # without inventing a role system nothing here needs yet. Never set via
    # any app endpoint — flipped directly in the database by whoever
    # administers the deployment (this mirrors "existing admin framework
    # behind SSO/VPN, not a service you build" from REQUIREMENTS 1.7).
    is_staff: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                            server_default="false")

    # Google sign-in: `google_sub` is Google's permanent id for the person (never changes, unlike the
    # email). auth_provider "google" means the account was CREATED through Google and has no password
    # the user knows (a random one is stored, as managed accounts do); "password" is every other account,
    # including password accounts that later linked a Google login.
    google_sub: Mapped[str | None] = mapped_column(String(64), unique=True, index=True, nullable=True)
    auth_provider: Mapped[str] = mapped_column(String(16), nullable=False, default="password",
                                               server_default="password")

    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def is_active(self) -> bool:
        return self.deleted_at is None


class RefreshToken(Base):
    """1.4 — rotation, sliding expiry, absolute cap, device-bound grace.

    Read the column comments before changing anything here; three of these fields
    exist to prevent specific, documented failures.
    """
    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    # Stored hashed, never plaintext (1.4).
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    # Deliberately `device_id`, NOT `device_fingerprint`. IP must never be part of
    # this value — mobile IPs change on wifi/cellular handoff, and treating that as
    # theft would revoke every session for a user who did nothing wrong (1.4).
    device_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # Sliding: each rotation grants a fresh window (1.4).
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Carries the ORIGINAL login time through every rotation. Without it the
    # 180-day absolute cap cannot be enforced, because each new token only knows
    # its own expiry (1.4).
    chain_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Set when this token is rotated out. Reuse after `grace_until` — or from a
    # different device at any time — is theft (1.6).
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    grace_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="refresh_tokens")

    __table_args__ = (
        Index("ix_refresh_tokens_user_active", "user_id", "revoked_at"),
    )

    def is_live(self, now: datetime) -> bool:
        return (
            self.revoked_at is None
            and self.rotated_at is None
            and self.expires_at > now
        )

    def in_grace(self, now: datetime) -> bool:
        return self.grace_until is not None and now <= self.grace_until


class PasswordResetToken(Base):
    """1.9 — single use, short expiry, stored hashed."""
    __tablename__ = "password_reset_tokens"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmailVerificationToken(Base):
    """1.8 — single use, ~24h expiry, stored hashed."""
    __tablename__ = "email_verification_tokens"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeletionRequest(Base):
    """1.10 — outlives the data it deleted.

    A backup restore would otherwise silently resurrect deleted accounts. The
    reconciliation job reads this table after any restore and re-applies every
    pending deletion before the system returns to service.

    `user_id_hash` rather than a plain id: this row survives the account, so it
    must not itself be a durable record identifying the person. Purged once
    `purge_after` passes — set just beyond the PITR window.
    """
    __tablename__ = "deletion_requests"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class DevicePushToken(Base):
    """8.3 — per user, per device. Deleted on an unregistered/invalid response
    from FCM/APNs, and revoked on logout and on theft detection (1.6)."""
    __tablename__ = "device_push_tokens"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_id: Mapped[str] = mapped_column(String(128), nullable=False)
    token: Mapped[str] = mapped_column(String(512), nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)  # ios | android
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("user_id", "device_id", name="uq_push_token_user_device"),
    )
