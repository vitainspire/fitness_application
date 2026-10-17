"""HTTP surface for Modules 3, 4, 6, 13, 14."""
from __future__ import annotations

import os
from datetime import date, timedelta

from flask import Blueprint, current_app, g, jsonify, request

from app.gateway.middleware import enforce_public_read_rate_limits, require_auth, require_staff
from app.platform.db import session_scope
from app.platform.errors import ValidationError
from app.profile import service
from app.progression import service as progression_service

bp = Blueprint("core", __name__, url_prefix="/v1")

# Sized against wall-clock, not "what looks like a lot": the client_entry_id
# path does a SELECT + savepointed INSERT + flush per entry, so each entry is
# ~3 DB round trips. On this deployment the app and database are in different
# regions (~350ms per round trip, measured), which makes even 50 entries a
# ~50-second request holding one of only four worker threads. 15 keeps the
# worst case near 15s while staying far above any real logging session (the
# largest genuine case is one custom exercise with all 3 measurement types).
MAX_ENTRIES_PER_REQUEST = 15


def _body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValidationError("Expected a JSON object body.")
    return data


def _str_field(d: dict, key: str, default: str = "") -> str:
    """Found live (code audit): every one of these fields used to go through
    `(d.get(key) or "").strip()` — fine for a missing/empty value, but a
    client sending a JSON number/list/object for a string field made `or`
    pass the truthy non-string value straight through, and `.strip()` on it
    raised an uncaught AttributeError (a 500, misclassified as a server
    crash) instead of a clean 422. Also rejects a JSON `null` explicitly
    sent as e.g. {"goal": null} the same way `or ""` used to."""
    value = d.get(key, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise ValidationError(f"{key!r} must be a string.")
    return value.strip()


def _optional_str_field(d: dict, key: str) -> str | None:
    return _str_field(d, key) or None


def _optional_bool_field(d: dict, key: str) -> bool | None:
    """Absent/None means "leave this field unchanged" — distinct from an
    explicit false, same reasoning as _optional_number_list_field above."""
    if key not in d or d[key] is None:
        return None
    return bool(d[key])


def _float_field(d: dict, *keys: str, default: float = 0.0) -> float:
    """Found live (code audit): `float(e.get("amount") or ... or 0)` crashed
    with an uncaught TypeError if the client sent a list/object instead of a
    number for a logged amount."""
    value = default
    for key in keys:
        if d.get(key) is not None:
            value = d[key]
            break
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{keys[0]!r} must be a number.")
    except OverflowError:
        # Found live (code audit): float() on a huge JSON integer (10**400)
        # raises OverflowError, which isn't in the tuple above — the same bug
        # class already fixed in _int_field just below, missed here. It
        # crashed as an uncaught 500 instead of a clean 422.
        raise ValidationError(f"{keys[0]!r} is out of range.")


def _int_field(d: dict, key: str, default: int) -> int:
    value = d.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{key!r} must be an integer.")
    except OverflowError:
        # Found live (second-pass code audit): int(float('inf')) raises
        # OverflowError, not ValueError/TypeError — {"sets": Infinity} (JSON
        # accepts bare Infinity/NaN by default) crashed uncaught here,
        # the same bug class already fixed for `days` in
        # dashboard_activity_detail but missed on this call site.
        raise ValidationError(f"{key!r} must be a finite integer.")


def _list_field(d: dict, key: str) -> list[str]:
    """Found live (code audit): `d.get(key) or []` let a non-list truthy
    value (e.g. a JSON number) through to be iterated, raising an uncaught
    TypeError. It also didn't reject a bare STRING (e.g. "reps" for
    measurement_types), which is technically iterable — it silently iterated
    as individual characters, producing a confusing validation error instead
    of a clean type error.

    Found live (second-pass code audit): validating the outer container
    alone wasn't enough — every real caller (condition_codes,
    measurement_types) feeds this straight into `dict.fromkeys(...)` for
    dedup, which calls hash() on each element; a non-string element (a list
    or dict) raised an uncaught TypeError there instead of here. Every real
    use of this helper is a list of taxonomy/condition CODES, so requiring
    string elements is correct for all current call sites, not just a
    convenient restriction."""
    value = d.get(key) or []
    if not isinstance(value, list):
        raise ValidationError(f"{key!r} must be a list.")
    if not all(isinstance(item, str) for item in value):
        raise ValidationError(f"{key!r} must be a list of strings.")
    return value


def _optional_number_list_field(d: dict, key: str) -> list[float] | None:
    """set_amounts: absent entirely (None) means "no per-set breakdown" and
    must stay distinguishable from an empty list — log_activity rejects an
    empty list outright but treats None as simply not using this feature, so
    `d.get(key)` (not `or []`) preserves that distinction. Booleans are
    technically `int` in Python and would silently pass an `isinstance(...,
    (int, float))` check — rejected explicitly, same reasoning as the
    existing numeric-field helpers in this file."""
    if key not in d or d[key] is None:
        return None
    value = d[key]
    if not isinstance(value, list):
        raise ValidationError(f"{key!r} must be a list of numbers.")
    if not all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value):
        raise ValidationError(f"{key!r} must be a list of numbers.")
    return [float(v) for v in value]


@bp.get("/conditions")
def conditions():
    """Public: the client needs this to render onboarding before a profile exists."""
    enforce_public_read_rate_limits()
    with session_scope() as s:
        return jsonify({"conditions": service.list_conditions(s)})


@bp.get("/taxonomy/activities")
def activities():
    enforce_public_read_rate_limits()
    with session_scope() as s:
        return jsonify({"activities": service.list_activities(s)})


@bp.post("/profile/onboarding")
@require_auth
def onboarding():
    d = _body()
    # Found live (E2E audit): an omitted condition_codes silently became [] ("no
    # conditions"), which for a medical-safety screen is the one field that must
    # be an explicit answer (API_REFERENCE.md: "never omit the field"). The
    # frontend always sends it; an explicit [] still means "none". Checked here,
    # not in the shared _list_field, which other endpoints rely on defaulting.
    if "condition_codes" not in d:
        raise ValidationError("'condition_codes' is required (use [] for none).")
    with session_scope() as s:
        return jsonify(service.complete_onboarding(
            s, g.user_id,
            age=d.get("age"),
            goal=_str_field(d, "goal"),
            experience_band=_str_field(d, "experience_band"),
            condition_codes=_list_field(d, "condition_codes"),
            unit_preference=_str_field(d, "unit_preference", "km"),
            confirm_rescale=d.get("confirm_rescale") is True,
        ))


@bp.get("/profile")
@require_auth
def profile():
    with session_scope() as s:
        return jsonify(service.profile_summary(s, g.user_id))


@bp.put("/profile")
@require_auth
def update_profile():
    """3.4 — edit age / goal / experience_band / unit_preference in any
    combination, without resubmitting conditions or rebuilding the program."""
    d = _body()
    # Found live (second-pass code audit): this route used to pass raw JSON
    # values straight through as `fields`, and service.update_profile does
    # `if goal not in GOALS` etc. directly on them — a non-string value
    # (e.g. {"goal": ["a"]}) makes `in` on a set call hash() on it, raising
    # an uncaught TypeError instead of a clean 422. Only string fields are
    # actually settable here, so route every present key through _str_field.
    fields = {}
    for key in ("goal", "experience_band", "unit_preference"):
        if key in d:
            fields[key] = _str_field(d, key)
    if "age" in d:
        fields["age"] = d["age"]
    with session_scope() as s:
        return jsonify(service.update_profile(
            s, g.user_id, fields=fields, confirm_rescale=d.get("confirm_rescale") is True))


@bp.get("/program")
@require_auth
def program():
    with session_scope() as s:
        return jsonify({"program": service.get_program(s, g.user_id)})


@bp.get("/program/catalogue")
@require_auth
def program_catalogue():
    with session_scope() as s:
        return jsonify({"catalogue": service.list_catalogue(s, g.user_id)})


@bp.post("/program/entries")
@require_auth
def add_program_entry():
    d = _body()
    # Found live (E2E audit): a body with no activity_type fell through as ""
    # and surfaced as 404 "Unknown activity: " — a missing required field is
    # a validation error (422), not a missing resource.
    if not _str_field(d, "activity_type"):
        raise ValidationError("'activity_type' is required.")
    # managed_user_id present = a manager (teacher/doctor/parent) acting on
    # someone else's plan — see MANAGED_PLANS_TEACHER_DOCTOR_PARENT_PLAN.md.
    # Authorization (an active relationship must exist) is checked inside
    # the service call itself, never trusted from the client.
    managed_user_id = d.get("managed_user_id")
    with session_scope() as s:
        if isinstance(managed_user_id, str) and managed_user_id:
            return jsonify(service.add_program_entry(
                s, managed_user_id, _str_field(d, "activity_type"),
                override_confirmed=bool(d.get("override_confirmed")),
                acting_manager_id=g.user_id,
                auto_progress=bool(d.get("auto_progress", True)),
            ))
        return jsonify(service.add_program_entry(
            s, g.user_id, _str_field(d, "activity_type"),
            override_confirmed=bool(d.get("override_confirmed")),
            auto_progress=bool(d.get("auto_progress", True)),
        ))


@bp.delete("/program/entries/<activity_type>")
@require_auth
def remove_program_entry(activity_type: str):
    with session_scope() as s:
        return jsonify(service.remove_program_entry(s, g.user_id, activity_type))


@bp.put("/program/entries/<activity_type>/target")
@require_auth
def set_managed_target(activity_type: str):
    """Directly set an entry's target, overriding whatever the app would
    otherwise propose. With managed_user_id: a manager setting a managed
    person's target (MANAGED_PLANS_TEACHER_DOCTOR_PARENT_PLAN.md §4). Without
    it: a user correcting their own — e.g. undoing a mistake or just wanting
    a different number than the default, same as a manager can already do."""
    d = _body()
    managed_user_id = d.get("managed_user_id")
    with session_scope() as s:
        if isinstance(managed_user_id, str) and managed_user_id:
            result = service.set_managed_target(
                s, g.user_id, managed_user_id, activity_type,
                target_sets=_int_field(d, "target_sets", 1),
                target_amount=_float_field(d, "target_amount"),
                set_amounts=_optional_number_list_field(d, "set_amounts"),
            )
        else:
            result = service.set_own_target(
                s, g.user_id, activity_type,
                target_sets=_int_field(d, "target_sets", 1),
                target_amount=_float_field(d, "target_amount"),
                set_amounts=_optional_number_list_field(d, "set_amounts"),
                auto_progress=_optional_bool_field(d, "auto_progress"),
            )
        return jsonify(result)


@bp.get("/suggestions")
@require_auth
def suggestions():
    with session_scope() as s:
        return jsonify(service.suggestions_today(s, g.user_id))


@bp.post("/activities")
@require_auth
def log():
    d = _body()
    entries = d.get("entries") or [d]
    # Found live (second-pass code audit): a truthy non-list "entries"
    # (e.g. {"entries": 5}) reached `for e in entries:` unchecked and raised
    # an uncaught TypeError ("int object is not iterable") instead of a
    # clean 422.
    if not isinstance(entries, list):
        raise ValidationError("'entries' must be a list.")
    # Found live (code audit): unbounded — the only real ceiling was the 1MiB
    # body limit (~20,000 tiny entries), each doing its own SELECT+INSERT+
    # flush inside one transaction. No legitimate single logging session logs
    # more than a handful of exercises at once (a custom exercise with all 3
    # measurement types is the largest real case).
    if len(entries) > MAX_ENTRIES_PER_REQUEST:
        raise ValidationError(f"Cannot log more than {MAX_ENTRIES_PER_REQUEST} entries at once.")
    out = []
    with session_scope() as s:
        for e in entries:
            if not isinstance(e, dict):
                raise ValidationError("Each activity entry must be a JSON object.")
            activity_type = _optional_str_field(e, "activity_type")
            set_amounts = _optional_number_list_field(e, "set_amounts")
            replace_today = e.get("replace_today", False)
            if not isinstance(replace_today, bool):
                raise ValidationError("'replace_today' must be true or false.")
            out.append(service.log_activity(
                s, g.user_id,
                activity_type=activity_type,
                custom_exercise_id=_optional_str_field(e, "custom_exercise_id"),
                amount=_float_field(e, "amount", "quantity"),
                sets=_int_field(e, "sets", 1),
                unit=_optional_str_field(e, "unit"),
                source=_str_field(e, "source", "manual"),
                client_entry_id=_optional_str_field(e, "client_entry_id"),
                workout_session_id=_optional_str_field(e, "workout_session_id"),
                set_amounts=set_amounts,
                replace_today=replace_today,
            ))
            # First real per-set log for this activity switches it from the
            # single-blended-target system to per-set independent tracking
            # (PER_SET_PROGRESSION_PLAN.md) — no-op if already started, or if
            # this isn't a reps-type activity. Deliberately after a
            # successful log_activity call, never before — a validation
            # failure on the log itself must not silently start tracking.
            if activity_type and set_amounts is not None:
                progression_service.start_per_set_tracking_if_needed(s, g.user_id, activity_type)
        return jsonify({"logged": out, "streak": service.streak(s, g.user_id)}), 201


@bp.post("/soreness/report")
@require_auth
def report_soreness_route():
    d = _body()
    activity_type = _str_field(d, "activity_type")
    with session_scope() as s:
        return jsonify(service.report_soreness(s, g.user_id, activity_type))


@bp.post("/soreness/clear")
@require_auth
def clear_soreness_route():
    d = _body()
    activity_type = _str_field(d, "activity_type")
    with session_scope() as s:
        return jsonify(service.clear_soreness(s, g.user_id, activity_type))


@bp.get("/soreness/status")
@require_auth
def soreness_status_route():
    with session_scope() as s:
        return jsonify({"items": service.list_soreness_status(s, g.user_id)})


@bp.post("/water-intake/add")
@require_auth
def add_water_glass_route():
    with session_scope() as s:
        return jsonify(service.add_water_glass(s, g.user_id))


@bp.get("/water-intake/today")
@require_auth
def water_today_route():
    with session_scope() as s:
        return jsonify(service.get_water_today(s, g.user_id))


@bp.post("/workout-sessions")
@require_auth
def start_workout_session():
    """Starts a guided Workout Player session. `activity_types` should be
    exactly what the player is about to show (from a prior /v1/suggestions
    call) — see service.start_workout_session for why that matters."""
    d = _body()
    activity_types = _list_field(d, "activity_types")
    # Found live (E2E audit): an empty list created a pointless 0-exercise
    # session. The app never sends one (the player returns early when there is
    # nothing to play), so this only rejects junk.
    if not activity_types:
        raise ValidationError("'activity_types' must contain at least one activity.")
    with session_scope() as s:
        result = service.start_workout_session(s, g.user_id, activity_types)
        return jsonify(result), 201


@bp.post("/workout-sessions/<uid:workout_session_id>/finish")
@require_auth
def finish_workout_session(workout_session_id: str):
    with session_scope() as s:
        return jsonify(service.finish_workout_session(s, g.user_id, workout_session_id))


@bp.get("/custom-exercises")
@require_auth
def custom_exercises():
    """4.4 — a user's own tracking-only exercises. Never suggested, never programmed."""
    with session_scope() as s:
        return jsonify({"custom_exercises": service.list_custom_exercises(s, g.user_id)})


@bp.post("/custom-exercises")
@require_auth
def create_custom_exercise():
    d = _body()
    managed_user_id = d.get("managed_user_id")
    kwargs = dict(
        name=_str_field(d, "name"),
        measurement_type=_str_field(d, "measurement_type"),
        target_amount=_float_field(d, "target_amount"),
        target_sets=_int_field(d, "target_sets", 1),
    )
    with session_scope() as s:
        if isinstance(managed_user_id, str) and managed_user_id:
            result = service.create_custom_exercise(
                s, managed_user_id, acting_manager_id=g.user_id, **kwargs)
        else:
            result = service.create_custom_exercise(s, g.user_id, **kwargs)
        return jsonify(result), 201


@bp.put("/custom-exercises/<uid:exercise_id>")
@require_auth
def rename_custom_exercise(exercise_id: str):
    d = _body()
    with session_scope() as s:
        return jsonify(service.rename_custom_exercise(
            s, g.user_id, exercise_id, _str_field(d, "name")))


@bp.delete("/custom-exercises/<uid:exercise_id>")
@require_auth
def delete_custom_exercise(exercise_id: str):
    with session_scope() as s:
        return jsonify(service.delete_custom_exercise(s, g.user_id, exercise_id))


@bp.get("/dashboard/trend")
@require_auth
def dashboard_trend():
    """4.3 — flexible trend window: ?days=15 or ?start=YYYY-MM-DD&end=YYYY-MM-DD."""
    start_str = request.args.get("start")
    end_str = request.args.get("end")
    days_str = request.args.get("days")
    try:
        start_date = date.fromisoformat(start_str) if start_str else None
        end_date = date.fromisoformat(end_str) if end_str else None
        days = int(days_str) if days_str else None
    except ValueError:
        raise ValidationError("start/end must be YYYY-MM-DD; days must be an integer.")
    with session_scope() as s:
        return jsonify({"trend": service.trend(
            s, g.user_id, days=days, start_date=start_date, end_date=end_date)})


@bp.get("/dashboard/activity-detail")
@require_auth
def dashboard_activity_detail():
    """Itemized log entries (which exercise, how much) over a date range —
    what /dashboard/trend deliberately doesn't give (a per-day count only).
    Defaults to the last 7 days; ?days=1 for just today, or an explicit
    ?start=&end= range, same convention as /dashboard/trend."""
    start_str = request.args.get("start")
    end_str = request.args.get("end")
    days_str = request.args.get("days")
    try:
        if start_str or end_str:
            if not (start_str and end_str):
                raise ValidationError("start and end must both be provided together.")
            start_date, end_date = date.fromisoformat(start_str), date.fromisoformat(end_str)
        else:
            days = int(days_str) if days_str else 7
            # Found live (code audit): an unbounded `days` value here (e.g.
            # ?days=1000000) overflowed Python's date range BEFORE this ever
            # reached service.activity_log_detail's own cap below — date
            # only supports years 1-9999, and OverflowError isn't a
            # ValueError, so the existing except clause didn't catch it
            # either. A trivial, reliable, single-request crash.
            if days < 1 or days > service.MAX_TREND_DAYS:
                raise ValidationError(f"days must be between 1 and {service.MAX_TREND_DAYS}.")
            end_date = service.today_ist()
            start_date = end_date - timedelta(days=days - 1)
    except ValueError:
        raise ValidationError("start/end must be YYYY-MM-DD; days must be an integer.")
    with session_scope() as s:
        return jsonify({"entries": service.activity_log_detail(
            s, g.user_id, start_date=start_date, end_date=end_date)})


@bp.get("/dashboard/summary")
@require_auth
def dashboard():
    with session_scope() as s:
        service.auto_restore_stale_soreness(s, g.user_id)
        return jsonify({
            "streak": service.streak(s, g.user_id),
            "week": service.weekly_volume(s, g.user_id),
            "suggestions": service.suggestions_today(s, g.user_id),
            # RLM design doc §7 / Implementation plan Phase 6 — additive field,
            # empty list until the weekly job (or a confirmed escalation) has
            # actually run at least once for this user.
            "progression": progression_service.progression_series(s, g.user_id),
            "signup_date": service.signup_date(s, g.user_id).isoformat(),
        })


@bp.get("/personal-bests")
@require_auth
def personal_bests():
    """Shown after the Workout Player when a just-logged distance/duration
    effort (walk, run, cycle, swim) earns a spot in the user's own top 5 for
    that activity (see log_activity's `extraordinary` flag) — purely a
    celebratory display list, never consulted by streak/progression math."""
    with session_scope() as s:
        return jsonify({"activities": service.list_personal_bests(s, g.user_id)})


@bp.get("/videos")
@require_auth
def videos():
    with session_scope() as s:
        return jsonify(service.video_library(s, g.user_id))


@bp.get("/exercise-library")
@require_auth
def exercise_library():
    """Browsable reference library pulled from ExerciseDB -- separate from
    video_library() above, which only covers the 12 activities our plans
    actually prescribe. This is just "what does X look like" reference
    content, not tied to any user's program."""
    with session_scope() as s:
        return jsonify(service.exercise_library(s, g.user_id))


@bp.put("/admin/videos/<activity_type>")
@require_auth
@require_staff
def curate_video(activity_type: str):
    """Admin curation (7.1) — paste one watched, approved YouTube URL.

    Staff-only (1.7): require_staff replaces the old is_production block
    now that a real staff role exists. Works the same in every
    environment - gated on who you are, not which environment is running.
    """
    d = _body()
    with session_scope() as s_:
        return jsonify(service.set_video(
            s_, activity_type,
            url_or_id=_str_field(d, "url"),
            title=_optional_str_field(d, "title"),
        ))


@bp.post("/admin/progression/run-daily")
def trigger_daily_progression():
    """Trigger the daily progression batch job.
    Accessible with a valid CRON_SECRET header (X-Cron-Secret) or by authenticated staff."""
    cron_secret = os.environ.get("CRON_SECRET", "").strip()
    provided_secret = request.headers.get("X-Cron-Secret", "").strip()

    is_authorized = False
    if cron_secret and provided_secret and cron_secret == provided_secret:
        is_authorized = True
    else:
        auth_header = request.headers.get("Authorization", "")
        if auth_header:
            try:
                require_auth(lambda: None)()
                require_staff(lambda: None)()
                is_authorized = True
            except Exception:
                is_authorized = False

    if not is_authorized:
        return jsonify({"error": {"code": "unauthorized", "message": "Missing or invalid authorization."}}), 401

    result = progression_service.run_daily_progression_batch()
    return jsonify(result), 200

