"""HTTP surface for managed plans (teacher/doctor/parent) —
see MANAGED_PLANS_TEACHER_DOCTOR_PARENT_PLAN.md."""
from __future__ import annotations

from flask import Blueprint, g, jsonify, request

from app.gateway.middleware import require_auth
from app.platform.db import session_scope
from app.platform.errors import ValidationError
from app.relationships import service

bp = Blueprint("relationships", __name__, url_prefix="/v1/relationships")


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
    with session_scope() as s:
        return jsonify(service.create_invite(s, g.user_id, relation_type)), 201


@bp.get("")
@require_auth
def list_relationships():
    with session_scope() as s:
        return jsonify({"relationships": service.list_my_relationships(s, g.user_id)})


@bp.post("/claim")
@require_auth
def claim():
    d = _body()
    token = d.get("invite_token")
    if not isinstance(token, str) or not token.strip():
        raise ValidationError("invite_token is required.")
    with session_scope() as s:
        return jsonify(service.claim_invite(s, token.strip(), g.user_id))


@bp.delete("/<relationship_id>")
@require_auth
def revoke(relationship_id: str):
    with session_scope() as s:
        service.revoke_relationship(s, g.user_id, relationship_id)
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
        return jsonify(service.add_pending_entry(
            s, g.user_id, relationship_id, activity_type=activity_type,
            target_sets=_int_field(d, "target_sets", 1),
            target_amount=_float_field(d, "target_amount"),
            auto_progress=bool(d.get("auto_progress", True)),
        )), 201


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
