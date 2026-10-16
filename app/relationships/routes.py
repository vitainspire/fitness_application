"""HTTP surface for managed plans (teacher/doctor/parent) —
see MANAGED_PLANS_TEACHER_DOCTOR_PARENT_PLAN.md."""
from __future__ import annotations

from flask import Blueprint, current_app, g, jsonify, request

from app.gateway.middleware import enforce_preauth_rate_limits, require_auth
from app.platform.db import session_scope
from app.platform.errors import ValidationError
from app.relationships import service

bp = Blueprint("relationships", __name__, url_prefix="/v1/relationships")


def _device_id() -> str:
    """Mirrors app/auth/routes.py's own _device_id exactly — duplicated
    rather than imported, matching this codebase's convention of each
    routes module keeping its own small body/field helpers."""
    device = request.headers.get("X-Device-Id", "").strip()
    if not device:
        raise ValidationError("X-Device-Id header is required.")
    if len(device) > 128:
        raise ValidationError("X-Device-Id header is too long.")
    return device


def _body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValidationError("Expected a JSON object body.")
    return data


def _float_field(d: dict, key: str, default: float = 0.0) -> float:
    value = d.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{key!r} must be a number.")
    except OverflowError:
        raise ValidationError(f"{key!r} is out of range.")


def _int_field(d: dict, key: str, default: int) -> int:
    value = d.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{key!r} must be an integer.")
    except OverflowError:
        raise ValidationError(f"{key!r} is out of range.")


@bp.post("")
@require_auth
def create_invite():
    d = _body()
    relation_type = d.get("relation_type")
    if not isinstance(relation_type, str):
        raise ValidationError("relation_type is required.")
    name = d.get("name")
    if not isinstance(name, str):
        raise ValidationError("name is required.")
    with session_scope() as s:
        return jsonify(service.create_invite(s, g.user_id, relation_type, name)), 201


@bp.get("")
@require_auth
def list_relationships():
    with session_scope() as s:
        return jsonify({"relationships": service.list_my_relationships(s, g.user_id)})


@bp.delete("")
@require_auth
def clear_revoked():
    with session_scope() as s:
        count = service.clear_revoked_relationships(s, g.user_id)
    return jsonify({"status": "cleared", "count": count})


@bp.post("/open")
def open_invite():
    """No @require_auth — the invite token itself is the credential. This is
    the whole mechanism: the managed person never signs up or logs in, this
    endpoint silently creates (or re-opens) their real account and hands
    back a normal token pair, exactly like signup/login would. Rate-limited
    the same as every other pre-auth endpoint for the same reason."""
    enforce_preauth_rate_limits()
    d = _body()
    token = d.get("invite_token")
    if not isinstance(token, str) or not token.strip():
        raise ValidationError("invite_token is required.")
    cfg = current_app.config["APP_CONFIG"]
    with session_scope() as s:
        pair, user, row = service.open_invite(s, cfg, token.strip(), _device_id())
        return jsonify({
            "access_token": pair.access_token, "refresh_token": pair.refresh_token,
            "access_expires_at": pair.access_expires_at.isoformat(),
            "refresh_expires_at": pair.refresh_expires_at.isoformat(),
            "user_id": user.id, "relation_type": row.relation_type,
        })


@bp.delete("/<relationship_id>")
@require_auth
def revoke(relationship_id: str):
    hard_delete = request.args.get("delete", "").lower() in ("true", "1")
    with session_scope() as s:
        service.revoke_relationship(s, g.user_id, relationship_id, hard_delete=hard_delete)
    return jsonify({"status": "revoked"})


@bp.get("/<relationship_id>/activity")
@require_auth
def activity(relationship_id: str):
    """"What are they actually doing" — plan doc §6."""
    with session_scope() as s:
        return jsonify(service.get_managed_activity(s, g.user_id, relationship_id))


@bp.post("/<relationship_id>/pending-entries")
@require_auth
def add_pending_entry(relationship_id: str):
    """Build the plan BEFORE the invite link is shared — see
    app/relationships/service.py's add_pending_entry docstring."""
    d = _body()
    activity_type = d.get("activity_type")
    if not isinstance(activity_type, str) or not activity_type:
        raise ValidationError("activity_type is required.")
    with session_scope() as s:
        result = service.add_pending_entry(
            s, g.user_id, relationship_id, activity_type=activity_type,
            target_sets=_int_field(d, "target_sets", 1),
            target_amount=_float_field(d, "target_amount"),
            auto_progress=bool(d.get("auto_progress", True)),
            override_confirmed=bool(d.get("override_confirmed")),
        )
        return jsonify(result), 201 if result.get("added") else 200


@bp.get("/<relationship_id>/pending-entries")
@require_auth
def list_pending_entries(relationship_id: str):
    with session_scope() as s:
        return jsonify({"entries": service.list_pending_entries(s, g.user_id, relationship_id)})


@bp.delete("/<relationship_id>/pending-entries/<activity_type>")
@require_auth
def remove_pending_entry(relationship_id: str, activity_type: str):
    with session_scope() as s:
        service.remove_pending_entry(s, g.user_id, relationship_id, activity_type)
    return jsonify({"status": "removed"})


@bp.put("/<relationship_id>/pending-profile")
@require_auth
def set_pending_profile(relationship_id: str):
    """The manager answers the onboarding questions on the managed person's
    behalf while building the plan — see
    app/relationships/service.py's set_pending_profile docstring."""
    d = _body()
    codes = d.get("condition_codes")
    band = d.get("experience_band")
    with session_scope() as s:
        return jsonify(service.set_pending_profile(
            s, g.user_id, relationship_id,
            age=_int_field(d, "age", 18),
            condition_codes=codes if isinstance(codes, list) else [],
            experience_band=band if isinstance(band, str) and band else None,
        ))


@bp.get("/<relationship_id>/suggested-exercises")
@require_auth
def suggested_exercises(relationship_id: str):
    """One-click-add suggestions for the pending-build screen — see
    service.suggested_exercises docstring."""
    with session_scope() as s:
        return jsonify({"suggestions": service.suggested_exercises(s, g.user_id, relationship_id)})


@bp.get("/<relationship_id>/blocked-activities")
@require_auth
def blocked_activities(relationship_id: str):
    """What the "Add to their plan" picker should grey out, for whatever
    conditions are on record for this relationship (staged, if still pending;
    real, if already claimed) — see service.blocked_activities docstring."""
    with session_scope() as s:
        return jsonify({"blocked": service.blocked_activities(s, g.user_id, relationship_id)})


@bp.post("/<relationship_id>/pending-custom-exercises")
@require_auth
def add_pending_custom_exercise(relationship_id: str):
    d = _body()
    name = d.get("name")
    if not isinstance(name, str):
        raise ValidationError("name is required.")
    measurement_type = d.get("measurement_type")
    if not isinstance(measurement_type, str):
        raise ValidationError("measurement_type is required.")
    with session_scope() as s:
        result = service.add_pending_custom_exercise(
            s, g.user_id, relationship_id, name=name, measurement_type=measurement_type,
            target_amount=_float_field(d, "target_amount"),
            target_sets=_int_field(d, "target_sets", 1),
        )
        return jsonify(result), 201


@bp.get("/<relationship_id>/pending-custom-exercises")
@require_auth
def list_pending_custom_exercises(relationship_id: str):
    with session_scope() as s:
        return jsonify({
            "exercises": service.list_pending_custom_exercises(s, g.user_id, relationship_id),
        })


@bp.delete("/<relationship_id>/pending-custom-exercises/<custom_exercise_id>")
@require_auth
def remove_pending_custom_exercise(relationship_id: str, custom_exercise_id: str):
    with session_scope() as s:
        service.remove_pending_custom_exercise(s, g.user_id, relationship_id, custom_exercise_id)
    return jsonify({"status": "removed"})
