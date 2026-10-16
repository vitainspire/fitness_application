"""DB-backed tests for the RLM analysis agent's deterministic sub-checks
(design doc §5.1). None of these touch the LLM router (_should_recurse) —
they call the sub-check functions directly, same pattern as the pure-logic
tests but against real ProgressionHistory/safety-grid data.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.platform.db import session_scope
from app.profile import service as profile_service
from app.progression.analysis import (
    _check_adherence_trend, _check_plateau, _check_streak, _check_vs_curve, _condition_gate,
)
from app.progression.models import ProgressionHistory

ACTIVITY = "glute_bridge"


def _seed_history(session, user_id, rows):
    """rows: list of (adherence_pct, curve_pct, old_target, new_target, reason),
    oldest first. Each gets a distinct applied_at so ordering is unambiguous."""
    base = datetime.now(timezone.utc) - timedelta(days=len(rows) * 7)
    for i, (adherence, curve, old, new, reason) in enumerate(rows):
        session.add(ProgressionHistory(
            user_id=user_id, activity_type=ACTIVITY,
            old_target=Decimal(str(old)), new_target=Decimal(str(new)),
            adherence_pct=Decimal(str(adherence)), curve_pct=Decimal(str(curve)),
            reason=reason, applied_at=base + timedelta(days=i * 7),
        ))
    session.flush()


def test_adherence_trend_with_no_history_reports_unavailable(make_user):
    user = make_user()
    with session_scope() as s:
        finding = _check_adherence_trend(s, user["user_id"], ACTIVITY)
        assert finding["available"] is False


def test_adherence_trend_detects_decline(make_user):
    user = make_user()
    with session_scope() as s:
        # oldest-first: 4 strong weeks, then 4 weak weeks (most recent last)
        _seed_history(s, user["user_id"], [
            (95, 10, 10, 11, "curve"), (92, 9, 11, 12, "curve"),
            (90, 8, 12, 13, "curve"), (93, 8, 13, 14, "curve"),
            (55, 0, 14, 14, "hold"), (40, 0, 14, 14, "reduce"),
            (35, 0, 14, 13, "reduce"), (45, 0, 13, 13, "hold"),
        ])
        finding = _check_adherence_trend(s, user["user_id"], ACTIVITY)
        assert finding["trend"] == "declining"
        assert finding["value"] < 50


def test_adherence_trend_detects_improvement(make_user):
    user = make_user()
    with session_scope() as s:
        _seed_history(s, user["user_id"], [
            (40, 0, 10, 10, "reduce"), (45, 0, 10, 10, "hold"),
            (50, 0, 10, 10, "hold"), (55, 0, 10, 10, "hold"),
            (90, 8, 10, 11, "curve"), (92, 8, 11, 12, "curve"),
            (95, 7, 12, 13, "curve"), (93, 7, 13, 14, "curve"),
        ])
        finding = _check_adherence_trend(s, user["user_id"], ACTIVITY)
        assert finding["trend"] == "improving"


def test_plateau_detected_when_flat_despite_reasonable_effort(make_user):
    user = make_user()
    with session_scope() as s:
        _seed_history(s, user["user_id"], [
            (70, 5, 20, 20, "hold"), (65, 5, 20, 20, "hold"), (60, 5, 20, 20, "hold"),
        ])
        finding = _check_plateau(s, user["user_id"], ACTIVITY)
        assert finding["detected"] is True


def test_plateau_not_detected_when_target_is_still_moving(make_user):
    user = make_user()
    with session_scope() as s:
        _seed_history(s, user["user_id"], [
            (90, 8, 10, 11, "curve"), (90, 7, 11, 12, "curve"), (90, 6, 12, 13, "curve"),
        ])
        finding = _check_plateau(s, user["user_id"], ACTIVITY)
        assert finding["detected"] is False


def test_plateau_not_detected_with_fewer_than_three_weeks(make_user):
    user = make_user()
    with session_scope() as s:
        _seed_history(s, user["user_id"], [(70, 5, 20, 20, "hold")])
        finding = _check_plateau(s, user["user_id"], ACTIVITY)
        assert finding["detected"] is False


def test_vs_curve_with_no_history_is_unavailable(make_user):
    user = make_user()
    with session_scope() as s:
        finding = _check_vs_curve(s, user["user_id"], ACTIVITY)
        assert finding["available"] is False


def test_vs_curve_reports_predicted_vs_applied(make_user):
    user = make_user()
    with session_scope() as s:
        _seed_history(s, user["user_id"], [(90, 12.5, 10, 11, "curve")])
        finding = _check_vs_curve(s, user["user_id"], ACTIVITY)
        assert finding["predicted_pct"] == 12.5
        assert finding["applied_pct"] == 10.0  # 11/10 - 1 = 10%
        assert finding["reason"] == "curve"


def test_streak_check_matches_the_existing_module_4_2_streak(make_user):
    user = make_user()
    with session_scope() as s:
        profile_service.log_activity(s, user["user_id"], activity_type=ACTIVITY,
                                      amount=5, sets=2)
        finding = _check_streak(s, user["user_id"])
        assert finding["current"] == 1
        assert finding["active_days"] == 1


def test_condition_gate_safe_when_no_avoid_verdicts(make_user):
    user = make_user(condition_codes=["gout"])
    with session_scope() as s:
        gate = _condition_gate(s, user["user_id"], ACTIVITY)
        assert gate == {"checked": True, "cached": False, "safe": True}


def test_condition_gate_unsafe_when_grid_says_avoid(make_user, force_avoid_cell):
    """herniated_disk x hamstring_stretch is forced to an `avoid` cell for
    this test (same combination used in tests/test_progression_engine.py) —
    see conftest.py's force_avoid_cell for why this isn't the real grid data
    anymore."""
    force_avoid_cell("herniated_disk", "hamstring_stretch")
    user = make_user(condition_codes=["herniated_disk"])
    with session_scope() as s:
        gate = _condition_gate(s, user["user_id"], "hamstring_stretch")
        assert gate["safe"] is False
