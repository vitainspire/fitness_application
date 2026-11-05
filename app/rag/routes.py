"""HTTP surface for Module 5 (5.1, 5.4) — /v1/rehab/query, /v1/rehab/quota."""
from __future__ import annotations

from flask import Blueprint, current_app, g, jsonify, request

from app.gateway.middleware import require_auth
from app.platform.db import session_scope
from app.platform.errors import ValidationError
from app.rag import service

bp = Blueprint("rehab", __name__, url_prefix="/v1/rehab")


def _body() -> dict:
    try:
        data = request.get_json(silent=True)
    except RecursionError:
        # A deeply nested JSON body (for example 5,000 "[") used to crash the JSON decoder with a RecursionError = HTTP 500 and a Slack alert.
        raise ValidationError("Expected a JSON object body.")
    if not isinstance(data, dict):
        raise ValidationError("Expected a JSON object body.")
    return data


def _rag_config():
    cfg = current_app.config["APP_CONFIG"]
    if not cfg.rag_database_url or not cfg.openrouter_api_key:
        # A real deploy decision (production hard-requiring this) hasn't been made
        # yet — see PART2 chat log — so this fails loudly per-request rather than
        # refusing to boot the whole app over a feature still being wired up. The
        # generic exception handler logs the real reason server-side and returns a
        # safe message to the client.
        raise RuntimeError(
            "RAG_DATABASE_URL / OPENROUTER_API_KEY are not configured; "
            "Module 5 cannot answer queries yet."
        )
    return cfg


@bp.get("/quota")
@require_auth
def quota():
    with session_scope() as s:
        return jsonify(service.remaining_quota(s, g.user_id))


@bp.get("/history")
@require_auth
def history():
    """5.7 — the actual transcript, so the chat screen has something to redraw
    on login instead of the conversation silently resetting to blank."""
    with session_scope() as s:
        return jsonify({"history": service.chat_history(s, g.user_id)})


@bp.post("/query")
@require_auth
def query():
    d = _body()
    cfg = _rag_config()
    confirmation = d.get("condition_confirmation")
    if confirmation is not None:
        if not isinstance(confirmation, dict):
            raise ValidationError("condition_confirmation must be an object.")
        # Found live (third-pass code audit): the outer object was checked but
        # not its "code" field — a non-string, unhashable value (a list/dict)
        # reached `code not in valid_codes` in service.py and raised an
        # uncaught TypeError instead of a clean 422.
        code = confirmation.get("code")
        if code is not None and not isinstance(code, str):
            raise ValidationError("condition_confirmation.code must be a string.")
    escalation_confirmation = d.get("escalation_confirmation")
    if escalation_confirmation is not None:
        if not isinstance(escalation_confirmation, dict):
            raise ValidationError("escalation_confirmation must be an object.")
        # Same bug class: a non-string activity_type (e.g. a list) survived
        # `(... or "").strip()` in service.py and raised an uncaught
        # AttributeError instead of a clean 422.
        activity_type = escalation_confirmation.get("activity_type")
        if activity_type is not None and not isinstance(activity_type, str):
            raise ValidationError("escalation_confirmation.activity_type must be a string.")
    program_confirmation = d.get("program_confirmation")
    if program_confirmation is not None:
        if not isinstance(program_confirmation, dict):
            raise ValidationError("program_confirmation must be an object.")
        # Same bug class as the two checks above: validate the inner field's
        # type here too, rather than letting a malformed value reach service.py.
        mode = program_confirmation.get("mode")
        if mode is not None and mode not in ("add", "switch"):
            raise ValidationError("program_confirmation.mode must be 'add' or 'switch'.")
        activity_types = program_confirmation.get("activity_types")
        if activity_types is not None and (
            not isinstance(activity_types, list)
            or not all(isinstance(a, str) for a in activity_types)
        ):
            raise ValidationError("program_confirmation.activity_types must be a list of strings.")
    revert_confirmation = d.get("revert_confirmation")
    if revert_confirmation is not None:
        if not isinstance(revert_confirmation, dict):
            raise ValidationError("revert_confirmation must be an object.")
        activity_types = revert_confirmation.get("activity_types")
        if activity_types is not None and (
            not isinstance(activity_types, list)
            or not all(isinstance(a, str) for a in activity_types)
        ):
            raise ValidationError("revert_confirmation.activity_types must be a list of strings.")
    question = d.get("question")
    if question is None:
        question = ""
    if not isinstance(question, str):
        # Found live (code audit): `d.get("question") or ""` passes a truthy
        # NON-string straight through (a JSON number, list or object), and
        # answer_query's `(question or "").strip()` then raises
        # AttributeError — an uncaught 500. This file already guards the
        # confirmation sub-fields this way; the main field was missed.
        raise ValidationError("'question' must be a string.")
    # A NUL byte (PostgreSQL text cannot hold it) or a lone invalid surrogate (cannot be encoded as UTF-8) used to reach the database / logger and crash with HTTP 500.
    try:
        question.encode("utf-8")
    except UnicodeEncodeError:
        raise ValidationError("Your message contains characters that can't be sent.")
    if "\x00" in question:
        raise ValidationError("Your message contains characters that can't be sent.")
    with session_scope() as s:
        return jsonify(service.answer_query(
            s, g.user_id, question,
            rag_db_url=cfg.rag_database_url,
            openrouter_api_key=cfg.openrouter_api_key,
            condition_confirmation=confirmation,
            escalation_confirmation=escalation_confirmation,
            program_confirmation=program_confirmation,
            revert_confirmation=revert_confirmation,
            rlm_enabled=cfg.rlm_chat_analysis_enabled,
        ))
