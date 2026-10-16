"""Integration tests for the Module 15 weekly job (RLM design doc §4, Implementation
plan Phase 2). Real local Postgres, same pattern as the rest of the suite.

glute_bridge is the fixture activity throughout: default 2x5, max 3x15, not
per-side (per data/activity_taxonomy.csv) — default_volume=10, ceiling_volume=45,
floor_volume=5.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.platform.db import session_scope
from app.profile import service as profile_service
from app.profile.models import ActivityLog, UserProgram
from app.profile.service import today_ist
from app.progression.service import MIN_LOGGED_DAYS_FOR_GROWTH
from app.progression.models import ProgressionHistory
from app.progression.service import DEFAULT_CURVE_K, compute_curve_increase

ACTIVITY = "glute_bridge"


def _backdate_entry(session, user_id, weeks, activity_type=ACTIVITY):
    entry = session.get(UserProgram, (user_id, activity_type))
    entry.added_at = datetime.now(timezone.utc) - timedelta(days=weeks * 7 + 1)
    session.flush()


def _seed_days(session, user_id, activity_type, *, sets, amount, unit, today, days):
    """Direct ActivityLog inserts on `days` distinct dates — needed wherever a
    test wants real growth: a single log_activity() call only ever produces
    one day, which MIN_LOGGED_DAYS_FOR_GROWTH now correctly holds rather than
    growing (see test_progression_min_frequency.py)."""
    from datetime import timedelta
    for i in range(days):
        session.add(ActivityLog(
            user_id=user_id, activity_type=activity_type, sets_done=sets,
            amount_per_set=amount, unit=unit, total_volume=sets * amount,
            local_date=today - timedelta(days=i),
        ))


def _row_for(rows, activity_type):
    """Onboarding auto-creates a default template program (14.2) alongside
    whatever this test explicitly adds, so `run_weekly_progression` legitimately
    returns one row per active entry, not just the one under test — every
    assertion below looks at its own activity_type's row, not the whole list."""
    matches = [r for r in rows if r.activity_type == activity_type]
    assert len(matches) == 1, f"expected exactly one row for {activity_type}, got {matches}"
    return matches[0]


def test_high_adherence_increases_target_by_the_curve(make_user):
    """Uses walk (distance, 0.2 km step) rather than the shared glute_bridge
    ACTIVITY constant: a single week's ~10% curve on glute_bridge's coarse
    2-rep step correctly snaps back to the SAME prescribable value (see
    test_curve_increase_can_hold_when_it_cant_cross_a_whole_step below) — that
    is real, intended behavior, not something this test should exercise. A
    finer-grained unit is what actually shows a visible single-week increase."""
    from app.progression.service import run_weekly_progression

    activity = "walk"
    user = make_user()
    with session_scope() as s:
        _backdate_entry(s, user["user_id"], weeks=1, activity_type=activity)
        # MIN_LOGGED_DAYS_FOR_GROWTH real days, not one log_activity() call —
        # a single day's log is now correctly held, not grown (see
        # test_progression_min_frequency.py).
        _seed_days(s, user["user_id"], activity, sets=1, amount=1, unit="km",
                   today=today_ist(), days=MIN_LOGGED_DAYS_FOR_GROWTH)

        rows = run_weekly_progression(s, user["user_id"], today=today_ist())
        row = _row_for(rows, activity)
        assert row.reason == "curve"
        expected_pct = compute_curve_increase(1.0, week=1, k=DEFAULT_CURVE_K)
        assert float(row.curve_pct) == round(expected_pct * 100, 2)
        assert float(row.new_target) > float(row.old_target)

        entry = s.get(UserProgram, (user["user_id"], activity))
        assert float(entry.target_amount) == float(row.new_target)


def test_curve_increase_can_hold_when_it_cant_cross_a_whole_step(make_user):
    """Found live: a default reps-math produced "5.52 reps" for a real
    account — not something a person can do. Fixed by snapping to the
    reviewed progression_step; this test locks in that a single ~10% week
    correctly HOLDS at the same whole value rather than reporting a
    fractional one, and that curve_pct itself still honestly records what
    the underlying math wanted, even though it didn't (yet) cross into the
    next real step.

    Uses calf_stretch (hold-type), not glute_bridge — glute_bridge is
    reps-type, and reps-type activities now use REPS_FAST_TRACK_K (~193% in
    week 1, decaying to a genuine log-shaped curve — an explicit product
    decision so a compliant beginner reaches the standard 30-rep ceiling in
    about 5 weeks instead of practically never), which makes even a single
    week's jump far too big to hold at the same value. Hold-type activities
    are untouched by that mechanism, so this is still the right activity
    shape to prove the underlying snap-hold
    behavior on."""
    from app.progression.service import run_weekly_progression

    activity = "calf_stretch"
    user = make_user()
    with session_scope() as s:
        _backdate_entry(s, user["user_id"], weeks=1, activity_type=activity)
        profile_service.log_activity(s, user["user_id"], activity_type=activity,
                                      amount=10, sets=2)

        rows = run_weekly_progression(s, user["user_id"], today=today_ist())
        row = _row_for(rows, activity)
        assert row.reason == "hold"
        assert float(row.new_target) == float(row.old_target) == 10.0
        assert float(row.curve_pct) > 0  # the math still wanted an increase


def test_curve_increase_eventually_crosses_a_whole_step_via_internal_tracking(make_user):
    """RESOLVED gap (this test used to be named ..._can_permanently_stall_the_
    curve and asserted the opposite of what's below): glute_bridge's step (2
    reps) is 40% of its baseline (5 reps), and even week=1's max single-week
    curve percentage (~10.4%) never crosses a whole step on its own. Fixed by
    UserProgram.internal_target_amount (see its docstring in
    app/profile/models.py) — the weekly job now grows a hidden, continuous
    value every week regardless of whether the DISPLAYED target_amount moved,
    so small gains accumulate instead of resetting. Verified live: this
    account's real glute_bridge crossed 5 -> 7 reps in exactly 2 weeks
    (internal 5.0 -> 5.52 -> 6.09, crossing the 6.0 midpoint on week 2)."""
    from app.progression.service import run_weekly_progression

    user = make_user()
    with session_scope() as s:
        profile_service.add_program_entry(s, user["user_id"], ACTIVITY)
        _backdate_entry(s, user["user_id"], weeks=1)
        # This test's "10 weeks" are simulated via added_at alone — today_ist()
        # never actually advances across iterations, so the adherence window
        # is the same real window every time. Seed 2 extra historical days
        # once, up front, so MIN_LOGGED_DAYS_FOR_GROWTH (3) is satisfied every
        # iteration once the loop's own per-iteration log adds "today" as the
        # 3rd distinct day — without this, every iteration would only ever
        # have one real distinct date logged and permanently hold.
        entry = s.get(UserProgram, (user["user_id"], ACTIVITY))
        _seed_days(s, user["user_id"], ACTIVITY, sets=entry.target_sets,
                   amount=float(entry.target_amount), unit=entry.unit,
                   today=today_ist() - timedelta(days=1), days=2)
        crossed = False
        for _ in range(10):
            entry = s.get(UserProgram, (user["user_id"], ACTIVITY))
            profile_service.log_activity(
                s, user["user_id"], activity_type=ACTIVITY,
                amount=float(entry.target_amount), sets=entry.target_sets,
            )
            run_weekly_progression(s, user["user_id"], today=today_ist())
            entry = s.get(UserProgram, (user["user_id"], ACTIVITY))
            if float(entry.target_amount) > 5.0:
                crossed = True
                break
        assert crossed, "target never crossed its first real step in 10 weeks of 100% adherence"
        assert float(entry.target_amount) % 2 == 1  # still on the real 5,7,9... grid


def test_mid_adherence_holds(make_user):
    from app.progression.service import run_weekly_progression

    user = make_user()
    with session_scope() as s:
        profile_service.add_program_entry(s, user["user_id"], ACTIVITY)
        _backdate_entry(s, user["user_id"], weeks=1)
        # 3 out of 5 reps per set = 60% of target volume -> the 50-79% hold bucket.
        profile_service.log_activity(s, user["user_id"], activity_type=ACTIVITY,
                                      amount=3, sets=2)

        rows = run_weekly_progression(s, user["user_id"], today=today_ist())
        row = _row_for(rows, ACTIVITY)
        assert row.reason == "hold"
        assert float(row.new_target) == float(row.old_target)


def test_no_logs_in_window_holds_when_the_reduce_cant_cross_a_whole_step(make_user):
    """A single -10% reduce is smaller than glute_bridge's 2-rep/5-rep-baseline
    (40%) step, so ONE week correctly holds at the same displayed value —
    still true even with internal_target_amount's multi-week compounding
    (see test_curve_increase_eventually_crosses_a_whole_step_via_internal_
    tracking), since that fix is about accumulation ACROSS weeks, not about
    any single week suddenly crossing a step it mathematically can't."""
    from app.progression.service import run_weekly_progression

    user = make_user()
    with session_scope() as s:
        profile_service.add_program_entry(s, user["user_id"], ACTIVITY)
        _backdate_entry(s, user["user_id"], weeks=1)
        # No activity_logs at all -> 0% adherence -> the <50% reduce bucket.
        rows = run_weekly_progression(s, user["user_id"], today=today_ist())
        row = _row_for(rows, ACTIVITY)
        assert row.reason == "hold"
        assert float(row.new_target) == float(row.old_target) == 5.0


def test_reduction_never_crosses_the_floor(make_user):
    """15.4 — absolute floor at 0.5x the default prescription volume (=5 here),
    regardless of how many consecutive low-adherence weeks run."""
    from app.progression.service import run_weekly_progression

    user = make_user()
    with session_scope() as s:
        profile_service.add_program_entry(s, user["user_id"], ACTIVITY)
        _backdate_entry(s, user["user_id"], weeks=1)
        for _ in range(20):  # far more than enough to floor from a single reduce step
            run_weekly_progression(s, user["user_id"], today=today_ist())
        entry = s.get(UserProgram, (user["user_id"], ACTIVITY))
        floor_amount = 5.0 / (entry.target_sets * 1)  # sides=1, not per_side
        assert float(entry.target_amount) >= floor_amount - 1e-6


def test_suspended_entry_is_never_progressed(make_user):
    from app.progression.service import run_weekly_progression

    user = make_user()
    with session_scope() as s:
        profile_service.add_program_entry(s, user["user_id"], ACTIVITY)
        _backdate_entry(s, user["user_id"], weeks=1)
        profile_service.remove_program_entry(s, user["user_id"], ACTIVITY)  # -> suspended
        rows = run_weekly_progression(s, user["user_id"], today=today_ist())
        assert all(r.activity_type != ACTIVITY for r in rows)


def test_user_override_entry_is_never_progressed(make_user, force_avoid_cell):
    """14.7 / 15.2 — the user chose to keep tracking something the grid flagged;
    the app must not increase its intensity.

    herniated_disk x hamstring_stretch is forced to an `avoid` cell for this
    test (a `supported`-bucket condition, unlike a refer-bucket condition,
    which would block add_program_entry entirely before source ever matters
    here) — see conftest.py's force_avoid_cell for why this isn't the real
    grid data anymore."""
    from app.progression.service import run_weekly_progression

    force_avoid_cell("herniated_disk", "hamstring_stretch")
    override_activity = "hamstring_stretch"
    user = make_user(condition_codes=["herniated_disk"])
    with session_scope() as s:
        profile_service.add_program_entry(
            s, user["user_id"], override_activity, override_confirmed=True
        )
        entry = s.get(UserProgram, (user["user_id"], override_activity))
        assert entry.source == "user_override"
        _backdate_entry(s, user["user_id"], weeks=1, activity_type=override_activity)
        profile_service.log_activity(s, user["user_id"], activity_type=override_activity,
                                      amount=15, sets=2)
        s.flush()
        rows = run_weekly_progression(s, user["user_id"], today=today_ist())
        assert all(r.activity_type != override_activity for r in rows)


def test_weekly_job_holds_instead_of_increasing_when_the_safety_grid_says_avoid(make_user):
    """Found live (code audit): run_weekly_progression never checked the
    safety grid before increasing a target — only the on-demand chat path
    did. Simulates the real gap this closes: the activity was already active
    from a BEFORE-condition-existed program (not a user_override entry,
    which is already excluded from progression for a different reason — see
    test_user_override_entry_is_never_progressed above), and the condition
    was added afterward. herniated_disk x hamstring_stretch is the same real
    `avoid` cell used by that test and by _condition_gate's own tests."""
    from app.profile.models import UserCondition
    from app.progression.service import run_weekly_progression

    activity = "hamstring_stretch"
    user = make_user()  # no conditions yet — hamstring_stretch adds normally
    with session_scope() as s:
        profile_service.add_program_entry(s, user["user_id"], activity)
        entry = s.get(UserProgram, (user["user_id"], activity))
        assert entry.source != "user_override"
        _backdate_entry(s, user["user_id"], weeks=1, activity_type=activity)
        profile_service.log_activity(s, user["user_id"], activity_type=activity,
                                      amount=15, sets=2)
        # The condition is added AFTER the activity is already active —
        # exactly the real-world scenario the missing check let slip through.
        s.add(UserCondition(user_id=user["user_id"], condition_code="herniated_disk"))
        s.flush()

        rows = run_weekly_progression(s, user["user_id"], today=today_ist())
        row = next(r for r in rows if r.activity_type == activity)
        assert row.reason == "hold"


def test_progression_history_row_is_queryable(make_user):
    from app.progression.service import run_weekly_progression

    user = make_user()
    with session_scope() as s:
        profile_service.add_program_entry(s, user["user_id"], ACTIVITY)
        _backdate_entry(s, user["user_id"], weeks=1)
        profile_service.log_activity(s, user["user_id"], activity_type=ACTIVITY,
                                      amount=5, sets=2)
        run_weekly_progression(s, user["user_id"], today=today_ist())

        history = s.scalars(
            select(ProgressionHistory).where(
                ProgressionHistory.user_id == user["user_id"],
                ProgressionHistory.activity_type == ACTIVITY,
            )
        ).all()
        assert len(history) == 1


def test_running_the_job_twice_the_same_day_does_not_stack_a_second_change(make_user):
    """Found live (code audit): nothing previously stopped a same-day re-run
    (a manual re-run, a retried scheduler trigger, an overlapping run — more
    plausible now the scheduler fires daily) from applying a second week's
    change on top of the first. Uses calf_stretch (hold-type, the generic
    guard-chain path) specifically — the reps fast-track path was already
    incidentally protected by its own never-regress floor."""
    from app.progression.service import run_weekly_progression

    activity = "calf_stretch"
    user = make_user()
    with session_scope() as s:
        _backdate_entry(s, user["user_id"], weeks=1, activity_type=activity)
        profile_service.log_activity(s, user["user_id"], activity_type=activity,
                                      amount=10, sets=2)
        today = today_ist()
        run_weekly_progression(s, user["user_id"], today=today)
        entry_after_first = s.get(UserProgram, (user["user_id"], activity))
        target_after_first = float(entry_after_first.target_amount)

        second_rows = run_weekly_progression(s, user["user_id"], today=today)
        entry_after_second = s.get(UserProgram, (user["user_id"], activity))

        # The second call must skip this already-progressed entry specifically
        # (other never-before-touched entries in the default program are
        # untouched by this test either way, since it only asserts about
        # `activity`).
        assert not any(r.activity_type == activity for r in second_rows)
        assert float(entry_after_second.target_amount) == target_after_first

        history = s.scalars(
            select(ProgressionHistory).where(
                ProgressionHistory.user_id == user["user_id"],
                ProgressionHistory.activity_type == activity,
            )
        ).all()
        assert len(history) == 1  # not two
