"""Module 1 HTTP surface. Paths match REQUIREMENTS → API Contract."""
from __future__ import annotations

from flask import Blueprint, current_app, g, jsonify, request

from app.auth import service
from app.gateway.middleware import enforce_preauth_rate_limits, require_auth
from app.platform.db import session_scope
from app.platform.errors import ValidationError
from app.platform.mailer import build_password_reset_html, send_email

bp = Blueprint("auth", __name__, url_prefix="/v1/auth")


def _body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValidationError("Expected a JSON object body.")
    return data


def _str_field(d: dict, key: str, default: str = "") -> str:
    """Found live (third-pass code audit): `d.get(key, default)` only applies
    the default when the key is ABSENT — an explicit JSON null for the key
    (e.g. {"email": null}) sails straight through as None, and every caller
    here immediately calls .strip()/.lower() on the result, raising an
    uncaught AttributeError (a 500) instead of a clean validation error. Same
    bug class already fixed in app/profile/routes.py's own _str_field; this
    is that fix applied to auth's fields, which never went through it."""
    value = d.get(key, default)
    return value if isinstance(value, str) else default


def _device_id() -> str:
    device = request.headers.get("X-Device-Id", "").strip()
    if not device:
        # 1.4 depends on a stable device id; refusing early is clearer than
        # silently binding the session to "unknown-device".
        raise ValidationError("X-Device-Id header is required.")
    # Found live: no maximum length was checked — this value is stored in a
    # String(128) column (app/auth/models.py), so a header longer than that
    # reached the INSERT and the database's own "value too long" error
    # surfaced as an uncaught 500 instead of a clean 422. Same class of bug
    # already fixed once for client_entry_id (app/profile/routes.py) and for
    # email (app/auth/service.py.signup) — applying the same proactive
    # length check here instead of letting the database reject it uncaught.
    if len(device) > 128:
        raise ValidationError("X-Device-Id header is too long.")
    return device


def _pair_response(pair, *, status: int = 200, **extra):
    """Build the body first, then serialise.

    Do NOT jsonify and then mutate `response.json` — that property returns a
    parsed *copy*, so the mutation is silently discarded and the field never
    reaches the client. Any extra fields belong in this dict.
    """
    body = {
        "access_token": pair.access_token,
        "refresh_token": pair.refresh_token,
        "access_expires_at": pair.access_expires_at.isoformat(),
        "refresh_expires_at": pair.refresh_expires_at.isoformat(),
    }
    body.update(extra)
    return jsonify(body), status


@bp.post("/signup")
def signup():
    enforce_preauth_rate_limits()
    data = _body()
    cfg = current_app.config["APP_CONFIG"]
    with session_scope() as session:
        user, pair, verify_token = service.signup(
            session, cfg,
            email=_str_field(data, "email"),
            password=_str_field(data, "password"),
            device_id=_device_id(),
        )
        send_email(
            to=user.email,
            subject="Verify your ViTAinspire email",
            body=f"Your verification token: {verify_token}",
            from_email=cfg.mail_from,
            from_name=cfg.mail_sender_name,
            is_production=cfg.is_production,
            smtp_host=cfg.smtp_host,
            smtp_port=cfg.smtp_port,
            smtp_user=cfg.smtp_user,
            smtp_password=cfg.smtp_password,
            smtp_use_tls=cfg.smtp_use_tls,
            smtp_use_ssl=cfg.smtp_use_ssl,
        )
        return _pair_response(pair, status=201, user_id=user.id)


@bp.post("/login")
def login():
    enforce_preauth_rate_limits()
    data = _body()
    cfg = current_app.config["APP_CONFIG"]
    with session_scope() as session:
        _, pair = service.login(
            session, cfg,
            email=_str_field(data, "email"),
            password=_str_field(data, "password"),
            device_id=_device_id(),
        )
        return _pair_response(pair)


@bp.post("/refresh")
def refresh():
    """Silent renewal (11.1). Sliding expiry means an active user never sees a
    login screen again after signup (1.4).

    enforce_preauth_rate_limits() added (found live, code audit): every
    other pre-auth endpoint calls it; this one didn't, leaving a full DB
    round trip per call completely unthrottled — plus, since
    rotate_refresh_token now takes a row lock (see tokens.py), an
    unthrottled flood here is also a plausible DB lock-contention vector.
    """
    enforce_preauth_rate_limits()
    data = _body()
    raw = data.get("refresh_token")
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError("refresh_token is required.")
    raw = raw.strip()
    cfg = current_app.config["APP_CONFIG"]
    with session_scope() as session:
        _, pair = service.refresh(session, cfg, raw, _device_id())
        return _pair_response(pair)


@bp.post("/logout")
@require_auth
def logout():
    data = request.get_json(silent=True) or {}
    # Found live while fixing the same bug class elsewhere in this file:
    # refresh_token is optional here, but a present-and-non-string value
    # (e.g. {"refresh_token": 123}) reached tk.hash_token's raw.encode() call
    # uncaught — same "silent-default doesn't cover an explicit wrong type"
    # pattern as _str_field above, just for an Optional field.
    raw_refresh = data.get("refresh_token")
    if raw_refresh is not None and not isinstance(raw_refresh, str):
        raise ValidationError("refresh_token must be a string.")
    with session_scope() as session:
        service.logout(session, g.access_claims, raw_refresh)
    return jsonify({"status": "signed_out"})


@bp.post("/verify-email")
def verify_email():
    """enforce_preauth_rate_limits() added (found live, code audit): the
    only pre-auth endpoint that skipped it besides /refresh (now also
    fixed) — an inconsistent, unthrottled gap in the pre-auth surface."""
    enforce_preauth_rate_limits()
    data = _body()
    token = data.get("token")
    if not isinstance(token, str) or not token.strip():
        raise ValidationError("token is required.")
    with session_scope() as session:
        service.verify_email(session, token.strip())
    return jsonify({"status": "verified"})


@bp.post("/password-reset/request")
def password_reset_request():
    enforce_preauth_rate_limits()
    data = _body()
    cfg = current_app.config["APP_CONFIG"]
    email = _str_field(data, "email")
    with session_scope() as session:
        raw = service.request_password_reset(session, email)
        if raw:
            reset_url = f"{cfg.frontend_url}/?reset_token={raw}#reset-password"
            body = (
                f"Hello,\n\n"
                f"We received a request to reset your password for your ViTAinspire account.\n\n"
                f"Click the link below to set your new password:\n"
                f"{reset_url}\n\n"
                f"Or use your one-time reset code: {raw}\n\n"
                f"Password Requirements:\n"
                f"- At least 1 capital letter (A-Z)\n"
                f"- At least 1 small letter (a-z)\n"
                f"- At least 1 number (0-9)\n"
                f"- At least 1 special character (!@#$%^&*...)\n"
                f"- Minimum {cfg.password_min_length} characters\n\n"
                f"This link expires in 1 hour. If you didn't request a password reset, you can safely ignore this email.\n\n"
                f"— ViTAinspire Team\n"
                f"{cfg.mail_from}"
            )
            html_body = build_password_reset_html(
                reset_url=reset_url,
                token=raw,
                sender_email=cfg.mail_from,
                min_length=cfg.password_min_length,
            )
            send_email(
                to=email,
                subject="Reset your ViTAinspire password",
                body=body,
                html_body=html_body,
                from_email=cfg.mail_from,
                from_name=cfg.mail_sender_name,
                is_production=cfg.is_production,
                smtp_host=cfg.smtp_host,
                smtp_port=cfg.smtp_port,
                smtp_user=cfg.smtp_user,
                smtp_password=cfg.smtp_password,
                smtp_use_tls=cfg.smtp_use_tls,
                smtp_use_ssl=cfg.smtp_use_ssl,
            )
    # Identical response either way — otherwise this is an enumeration oracle (1.9).
    return jsonify({"status": "ok", "message": service.RESET_ACK})


@bp.post("/password-reset/confirm")
def password_reset_confirm():
    enforce_preauth_rate_limits()
    data = _body()
    cfg = current_app.config["APP_CONFIG"]
    with session_scope() as session:
        service.confirm_password_reset(
            session, cfg,
            raw_token=_str_field(data, "token").strip(),
            new_password=_str_field(data, "password"),
        )
    return jsonify({"status": "password_updated"})


@bp.post("/change-password")
@require_auth
def change_password():
    """Settings > change password. Requires the current password, not a token."""
    data = _body()
    cfg = current_app.config["APP_CONFIG"]
    with session_scope() as session:
        service.change_password(
            session, cfg, g.user_id,
            current_password=_str_field(data, "current_password"),
            new_password=_str_field(data, "new_password"),
        )
    return jsonify({"status": "password_updated"})


account_bp = Blueprint("account", __name__, url_prefix="/v1")


@account_bp.delete("/account")
@require_auth
def delete_account():
    # Found live (third-pass code audit): a valid session used to be enough
    # on its own for the most destructive action in the app — now requires
    # re-confirming the current password too, the same way change_password
    # already does.
    data = _body()
    cfg = current_app.config["APP_CONFIG"]
    with session_scope() as session:
        service.delete_account(session, cfg, g.user_id, password=_str_field(data, "password"))
    return jsonify({"status": "deletion_scheduled"})
